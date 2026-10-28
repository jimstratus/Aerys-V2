"""Opt-in SDK-backed A2A 1.0 JSON-RPC door, separate from owner HTTP.

A2A task completion means this restricted exchange completed, NOT approval of a
ledger work item. Unsupported streaming/push/continuations fail explicitly.
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from uuid import uuid4

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import RequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import ServerCallContextBuilder
from a2a.types import AgentCard, ListTasksResponse, TaskState
from a2a.utils.errors import (
    InvalidParamsError,
    TaskNotCancelableError,
    TaskNotFoundError,
    UnsupportedOperationError,
)
from fastapi import FastAPI
from google.protobuf.json_format import ParseDict
from starlette.responses import JSONResponse

from aerys_v2.a2a.contracts import PeerRequest
from aerys_v2.a2a.propagation import carrier, propagator, scrub, tracer


class PeerGuard:
    """Authenticate before SDK parsing, bound chunked bodies, suppress secret errors."""

    def __init__(self, app, registry, max_body):
        self.app, self.registry, self.max_body = app, registry, max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        try:
            peer = self.registry.authenticate(headers.get("authorization", ""))
        except PermissionError:
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(
                scope, receive, send
            )
        body = bytearray()
        while True:
            part = await receive()
            if part["type"] == "http.disconnect":
                return
            body.extend(part.get("body", b""))
            if len(body) > self.max_body:
                return await JSONResponse(
                    {"error": "request too large"}, status_code=413
                )(scope, receive, send)
            if not part.get("more_body"):
                break
        scope.setdefault("state", {})["peer"] = peer.id

        async def replay():
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        return await self.app(scope, replay, send)


class PeerContext(ServerCallContextBuilder):
    def build(self, request):
        return ServerCallContext(
            state={
                "peer": request.state.peer,
                "trace": carrier(request.headers),
                "headers": {"a2a-version": request.headers.get("a2a-version", "")},
            }
        )


class PeerHandler(RequestHandler):
    def __init__(self, registry, store, execute, tenant, capacity):
        self.registry, self.store, self.execute = registry, store, execute
        self.tenant, self.capacity = tenant, capacity
        self.running = {}

    def authorize(self, context, params, operation):
        peer = context.state["peer"]
        try:
            self.registry.authorize(peer, operation)
        except PermissionError:
            raise TaskNotFoundError("Operation not granted") from None
        if params.tenant != self.tenant:
            raise InvalidParamsError("Incorrect tenant")
        return peer

    async def on_message_send(self, params, context):
        peer = self.authorize(context, params, "send")
        msg = params.message
        if (
            not msg.message_id
            or len(msg.message_id) > 128
            or len(msg.context_id) > 128
            or msg.task_id
            or msg.role != 1
            or not msg.parts
            or any(p.WhichOneof("content") != "text" for p in msg.parts)
        ):
            raise InvalidParamsError(
                "Only new text tasks with user role and bounded IDs are supported"
            )
        if params.configuration.HasField("task_push_notification_config"):
            raise UnsupportedOperationError("Push not supported")
        context_id = (
            msg.context_id
            or hashlib.sha256(f"{peer}\0{msg.message_id}".encode()).hexdigest()
        )
        digest = hashlib.sha256(msg.SerializeToString(deterministic=True)).hexdigest()
        # Duplicate lookups are allowed even at capacity; reserve and capacity checks
        # have no awaits between them, so one loop cannot over-admit executions.
        existing = self.store.has_message(peer, msg.message_id)
        if not existing and len(self.running) >= self.capacity:
            raise UnsupportedOperationError("Execution capacity exhausted")
        try:
            task, fresh = self.store.reserve(
                peer, msg.message_id, context_id, digest, context.state["trace"]
            )
        except (PermissionError, ValueError):
            raise InvalidParamsError(
                "Context unavailable or message conflict"
            ) from None
        if fresh:
            text = "\n".join(p.text for p in msg.parts)
            text = scrub(text, self.registry.redactions())
            req = PeerRequest(
                peer,
                task.id,
                task.context_id,
                "a2a:"
                + hashlib.sha256(f"{peer}\0{task.context_id}".encode()).hexdigest(),
                text,
            )
            job = asyncio.create_task(self._run(task, req, context.state["trace"]))
            self.running[task.id] = job
            job.add_done_callback(lambda _, tid=task.id: self.running.pop(tid, None))
        job = self.running.get(task.id)
        if job and not params.configuration.return_immediately:
            await asyncio.shield(job)
        return self.store.get(peer, task.id)

    async def _run(self, task, request, trace):
        current = self.store.get(request.principal, task.id)
        if current.status.state == TaskState.TASK_STATE_CANCELED:
            return
        task.status.state = TaskState.TASK_STATE_WORKING
        self.store.save(task)
        try:
            with tracer.start_as_current_span(
                "a2a.execute", context=propagator.extract(trace)
            ) as span:
                span.set_attribute("eotir.a2a.task.id", task.id)
                span.set_attribute("eotir.agent.id", "aerys")
                span.set_attribute("a2a.peer.id", request.principal)
                result = await self.execute(request)
            if not isinstance(result, str) or len(result.encode()) > 65536:
                raise ValueError("executor result must be bounded text")
            task.status.state = TaskState.TASK_STATE_COMPLETED
            task.artifacts.add(artifact_id=uuid4().hex).parts.add(
                text=scrub(result, self.registry.redactions())
            )
        except asyncio.CancelledError:
            task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
            task.status.message.role = 2
            task.status.message.message_id = uuid4().hex
            task.status.message.parts.add(
                text="Execution interrupted; reconcile before replacement."
            )
            raise
        except Exception:
            # No exception text: model/transport failures may contain secrets.
            task.status.state = TaskState.TASK_STATE_FAILED
            task.status.message.role = 2
            task.status.message.message_id = uuid4().hex
            task.status.message.parts.add(text="Restricted peer execution failed.")
        finally:
            self.store.save(task)

    async def on_get_task(self, params, context):
        peer = self.authorize(context, params, "get")
        task = self.store.get(peer, params.id)
        if not task:
            raise TaskNotFoundError("Task not found")
        return task

    async def on_list_tasks(self, params, context):
        peer = self.authorize(context, params, "list")
        # Deterministic bounded page, scoped to the authenticated peer.
        try:
            offset = int(params.page_token or "0")
            if offset < 0:
                raise ValueError()
        except ValueError:
            raise InvalidParamsError("Invalid page token") from None
        limit = min(params.page_size or 50, 100)
        if params.page_size < 0 or offset > 2147483647:
            raise InvalidParamsError("Invalid page bounds")
        if params.HasField("status_timestamp_after"):
            raise UnsupportedOperationError("Timestamp filtering not supported")
        page, total = self.store.list_page(
            peer,
            params.context_id,
            TaskState.Name(params.status) if params.status else "",
            offset=offset,
            limit=limit,
        )
        return ListTasksResponse(
            tasks=page,
            total_size=total,
            page_size=len(page),
            next_page_token=str(offset + limit) if offset + limit < total else "",
        )

    async def on_cancel_task(self, params, context):
        peer = self.authorize(context, params, "cancel")
        task = self.store.get(peer, params.id)
        if not task:
            raise TaskNotFoundError("Task not found")
        if task.status.state == TaskState.TASK_STATE_CANCELED:
            return task
        if task.status.state != TaskState.TASK_STATE_SUBMITTED:
            # Python thread cancellation cannot stop tool effects. Do not pretend.
            raise TaskNotCancelableError(
                "Running/uncertain/terminal task cannot be safely canceled"
            )
        task.status.state = TaskState.TASK_STATE_CANCELED
        self.store.save(task)
        return task

    async def unsupported(self, *args, **kwargs):
        raise UnsupportedOperationError("Operation not supported")

    async def unsupported_stream(self, *args, **kwargs):
        raise UnsupportedOperationError("Streaming not supported")
        yield  # marks the SDK interface as an async generator

    on_get_extended_agent_card = unsupported
    on_create_task_push_notification_config = unsupported
    on_get_task_push_notification_config = unsupported
    on_list_task_push_notification_configs = unsupported
    on_delete_task_push_notification_config = unsupported
    on_message_send_stream = unsupported_stream
    on_subscribe_to_task = unsupported_stream


def build_peer_app(
    registry, store, execute, *, url, tenant="aerys", capacity=8, max_body=65536
):
    """Build a dedicated listener; callers own the lifecycle and receipt path."""
    if not 1 <= capacity <= 128 or not 1 <= max_body <= 1048576:
        raise ValueError("Invalid resource bounds")
    handler = PeerHandler(registry, store, execute, tenant, capacity)

    @asynccontextmanager
    async def lifespan(app):
        store.start()
        try:
            yield
        finally:
            # Drain, don't abandon sync work then release the process lock.
            await asyncio.gather(
                *list(handler.running.values()), return_exceptions=True
            )
            store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def health():
        try:
            if store.healthy():
                return {"status": "ok"}
        except Exception:
            pass
        return JSONResponse({"status": "unavailable"}, status_code=503)

    card = ParseDict(
        {
            "name": "Aerys peer assistant",
            "description": "Restricted text exchanges; no owner tools or fleet assignment authority.",
            "version": "1.0.0",
            "supportedInterfaces": [
                {
                    "url": url,
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": "1.0",
                    "tenant": tenant,
                }
            ],
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "skills": [
                {
                    "id": "peer-chat",
                    "name": "Peer chat",
                    "description": "Restricted text exchange",
                    "tags": ["text"],
                }
            ],
            "securitySchemes": {
                "peer": {"httpAuthSecurityScheme": {"scheme": "bearer"}}
            },
            "securityRequirements": [{"schemes": {"peer": {"list": []}}}],
        },
        AgentCard(),
    )
    app.router.routes.extend(create_agent_card_routes(card))
    app.router.routes.extend(
        create_jsonrpc_routes(handler, "/", context_builder=PeerContext())
    )
    app.add_middleware(PeerGuard, registry=registry, max_body=max_body)
    return app
