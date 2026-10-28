"""Pinned-endpoint SDK client with durable uncertain-send reconciliation.

Caller supplies a stable dispatch ID. No timeout or restart triggers a new send.
Receipts remember protocol IDs only; they cannot assign/complete ledger work.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from a2a.client.card_resolver import parse_agent_card
from a2a.client.client import ClientCallContext
from a2a.client.transports.jsonrpc import JsonRpcTransport
from a2a.types import (
    CancelTaskRequest,
    GetTaskRequest,
    ListTasksRequest,
    Role,
    SendMessageRequest,
    Task,
)

from aerys_v2.a2a.contracts import ReconciliationRequired
from aerys_v2.a2a.propagation import propagator, scrub, tracer


class PeerClient:
    def __init__(self, registry, store, http: httpx.AsyncClient):
        if http.follow_redirects:
            raise ValueError("Peer client must not follow redirects")
        self.registry, self.store, self.http = registry, store, http

    def _context(self, peer):
        headers = {
            "Authorization": "Bearer " + self.registry.token(peer.id),
            "A2A-Version": "1.0",
        }
        propagator.inject(headers)
        return ClientCallContext(service_parameters=headers, timeout=peer.timeout)

    async def _transport(self, peer_id, operation):
        self.registry.authorize(peer_id, operation)
        peer = self.registry.peer(peer_id)
        # Never follow a card to another endpoint; enrolled URL is the trust root.
        r = await self.http.get(
            peer.url.rstrip("/") + "/.well-known/agent-card.json",
            headers=self._context(peer).service_parameters,
            timeout=peer.timeout,
        )
        r.raise_for_status()
        card = parse_agent_card(r.json())
        if card.name != peer.card_name or not any(
            i.url == peer.url
            and i.tenant == peer.tenant
            and i.protocol_binding == "JSONRPC"
            and i.protocol_version in {"1.0", "1.0.0"}
            for i in card.supported_interfaces
        ):
            raise ValueError("Agent card identity/profile/protocol mismatch")
        return peer, JsonRpcTransport(self.http, card, peer.url, is_shared_httpx=True)

    def _record(self, dispatch_id, task, expected_context):
        if not task.id or task.context_id != expected_context:
            raise ReconciliationRequired("Remote task identity/context mismatch")
        clean = Task(id=task.id, context_id=task.context_id)
        clean.status.state = task.status.state
        clean.status.timestamp.CopyFrom(task.status.timestamp)
        for art in task.artifacts:
            target = clean.artifacts.add(artifact_id=art.artifact_id)
            for part in art.parts:
                if part.WhichOneof("content") != "text":
                    raise ReconciliationRequired(
                        "Unsupported result part; do not infer success"
                    )
                target.parts.add(text=scrub(part.text, self.registry.redactions()))
        if task.status.message.parts:
            clean.status.message.role = Role.ROLE_AGENT
            clean.status.message.message_id = task.status.message.message_id
            for part in task.status.message.parts:
                if part.WhichOneof("content") == "text":
                    clean.status.message.parts.add(
                        text=scrub(part.text, self.registry.redactions())
                    )
        self.store.record_dispatch(dispatch_id, "accepted", clean)
        return clean

    async def submit(self, peer_id, text, *, dispatch_id):
        trace = {}
        propagator.inject(trace)
        row, fresh = self.store.prepare_dispatch(dispatch_id, peer_id, text, trace)
        if not fresh and row["state"] != "prepared":
            return await self.reconcile(dispatch_id)
        peer, transport = await self._transport(peer_id, "send")
        # Re-read after discovery await: another concurrent submit may have sent it.
        row = self.store.dispatch(dispatch_id)
        if row["state"] != "prepared":
            return await self.reconcile(dispatch_id)
        request = SendMessageRequest(tenant=peer.tenant)
        request.message.message_id = row["message"]
        request.message.context_id = row["context"]
        request.message.role = Role.ROLE_USER
        request.message.parts.add(text=scrub(text, self.registry.redactions()))
        request.configuration.return_immediately = False
        if not self.store.claim_dispatch(dispatch_id):
            return await self.reconcile(dispatch_id)
        try:
            with tracer.start_as_current_span("a2a.send"):
                response = await transport.send_message(
                    request, context=self._context(peer)
                )
            if not response.HasField("task"):
                raise ReconciliationRequired("Peer did not return a durable task")
            return self._record(dispatch_id, response.task, row["context"])
        except (Exception, asyncio.CancelledError):
            self.store.record_dispatch(dispatch_id, "uncertain")
            raise ReconciliationRequired(
                "Send outcome uncertain; reconcile original dispatch ID"
            ) from None

    async def reconcile(self, dispatch_id):
        row = self.store.dispatch(dispatch_id)
        if not row:
            raise KeyError(dispatch_id)
        with tracer.start_as_current_span(
            "a2a.reconcile", context=propagator.extract(json.loads(row["trace"]))
        ):
            return await self._reconcile(row)

    async def _reconcile(self, row):
        dispatch_id = row["id"]
        peer, transport = await self._transport(row["peer"], "get")
        try:
            if row["remote_id"]:
                task = await transport.get_task(
                    GetTaskRequest(id=row["remote_id"], tenant=peer.tenant),
                    context=self._context(peer),
                )
            else:
                self.registry.authorize(peer.id, "list")
                result = await transport.list_tasks(
                    ListTasksRequest(
                        context_id=row["context"],
                        tenant=peer.tenant,
                        page_size=100,
                        include_artifacts=True,
                    ),
                    context=self._context(peer),
                )
                if len(result.tasks) != 1 or result.next_page_token:
                    raise ReconciliationRequired(
                        "No unique remote task; operator reconciliation required"
                    )
                task = result.tasks[0]
            return self._record(dispatch_id, task, row["context"])
        except Exception:
            raise ReconciliationRequired(
                "Remote state unresolved; do not resubmit or reassign"
            ) from None

    async def status(self, dispatch_id):
        return await self.reconcile(dispatch_id)

    async def cancel(self, dispatch_id):
        row = self.store.dispatch(dispatch_id)
        if not row or not row["remote_id"]:
            raise ReconciliationRequired(
                "Resolve original remote task before canceling"
            )
        peer, transport = await self._transport(row["peer"], "cancel")
        try:
            task = await transport.cancel_task(
                CancelTaskRequest(id=row["remote_id"], tenant=peer.tenant),
                context=self._context(peer),
            )
            return self._record(dispatch_id, task, row["context"])
        except Exception:
            raise ReconciliationRequired(
                "Cancellation refused or uncertain; original work may continue"
            ) from None
