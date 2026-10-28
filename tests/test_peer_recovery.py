import asyncio
from pathlib import Path

import httpx
import pytest
from a2a.types import TaskState
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_peer_server import registry, rpc, send

from aerys_v2.a2a.executor import PeerAssistant
from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.transports.a2a_server import build_peer_app


def test_restart_retains_results_and_never_replays_uncertain_work(tmp_path):
    path = tmp_path / "receipts.db"
    store = ReceiptStore(path)
    store.start()
    task, _ = store.reserve(
        "alice",
        "m1",
        "context",
        "digest",
        {"traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01"},
    )
    task.status.state = TaskState.TASK_STATE_WORKING
    store.save(task)
    contender = ReceiptStore(path)
    with pytest.raises(RuntimeError):
        contender.start()
    contender.close()
    store.close()
    store = ReceiptStore(path)
    store.start()
    task2, fresh = store.reserve("alice", "m1", "context", "digest", {})
    assert not fresh
    assert task2.id == task.id
    assert task2.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert "uncertain" in task2.status.message.parts[0].text
    store.close()


def test_running_cancel_refused_capacity_bounds_and_duplicate_race(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def execute(req):
            calls.append(req)
            started.set()
            await release.wait()
            return "done"

        store = ReceiptStore(tmp_path / "receipts.db")
        app = build_peer_app(
            registry(), store, execute, url="http://localhost/", capacity=1
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                pending = {**send(), "configuration": {"returnImmediately": True}}
                task = (await rpc(client, "SendMessage", pending)).json()["result"][
                    "task"
                ]
                await started.wait()
                assert (
                    "error"
                    in (
                        await rpc(
                            client, "CancelTask", {"id": task["id"], "tenant": "aerys"}
                        )
                    ).json()
                )
                assert (
                    "error" in (await rpc(client, "SendMessage", send("other"))).json()
                )
                duplicate = (await rpc(client, "SendMessage", pending)).json()[
                    "result"
                ]["task"]
                assert duplicate["id"] == task["id"]
                release.set()
                await asyncio.sleep(0)
                assert len(calls) == 1
                assert (
                    (
                        await rpc(
                            client,
                            "SendMessage",
                            {
                                **pending,
                                "message": {
                                    **pending["message"],
                                    "parts": [{"text": "changed"}],
                                },
                            },
                        )
                    )
                    .json()
                    .get("error")
                )
                oversized = await client.post(
                    "/",
                    content=b"x" * 70000,
                    headers={"Authorization": "Bearer alice-token"},
                )
                assert oversized.status_code == 413

    asyncio.run(run())


def test_real_ask_adapter_preserves_trace_parent_and_scrubs_credentials(tmp_path):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Isolate provider globals with monkeypatch to avoid affecting other suites.
    from unittest.mock import patch

    async def run():
        store = ReceiptStore(tmp_path / "receipts.db")
        execute = PeerAssistant(
            FakeListChatModel(
                responses=["answer alice-token password=synthetic-secret"]
            )
        )
        app = build_peer_app(registry(), store, execute, url="http://localhost/")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                headers = {
                    "Authorization": "Bearer alice-token",
                    "A2A-Version": "1.0",
                    "traceparent": "00-" + "1" * 32 + "-" + "2" * 16 + "-01",
                    "baggage": "password=secret",
                }
                response = await client.post(
                    "/",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "SendMessage",
                        "params": send(),
                    },
                )
                task = response.json()["result"]["task"]
                assert task["status"]["state"] == "TASK_STATE_COMPLETED"
                text = task["artifacts"][0]["parts"][0]["text"]
                assert "alice-token" not in text and "synthetic-secret" not in text
                assert "secret" not in Path(str(store.path)).read_bytes().decode(
                    errors="ignore"
                )

    with (
        patch(
            "aerys_v2.transports.a2a_server.tracer", provider.get_tracer("peer-test")
        ),
        patch("aerys_v2.service._TRACER", provider.get_tracer("ask-test")),
    ):
        asyncio.run(run())
    spans = exporter.get_finished_spans()
    execute = next(s for s in spans if s.name == "a2a.execute")
    assert execute.context.trace_id == int("1" * 32, 16)
    assert execute.parent.span_id == int("2" * 16, 16)
    ask_span = next(s for s in spans if s.name == "ask")
    assert ask_span.context.trace_id == execute.context.trace_id
    assert ask_span.parent.span_id == execute.context.span_id
    provider.shutdown()


def test_config_only_enrollment_disable_preserves_existing_receipts(tmp_path):
    async def run():
        from aerys_v2.a2a.registry import Peer, Registry

        async def execute(req):
            return req.principal

        path = tmp_path / "receipts.db"
        base = registry()
        extra = Peer(
            id="new-agent",
            url="http://localhost/new/",
            tenant="new",
            card_name="New",
            token_env="NEW",
        )
        extended = Registry(
            [base.peer("alice"), base.peer("bob"), extra],
            {"ALICE": "alice-token", "BOB": "bob-token", "NEW": "new-token"},
        )
        store = ReceiptStore(path)
        app = build_peer_app(extended, store, execute, url="http://localhost/")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                task = (
                    await rpc(
                        client,
                        "SendMessage",
                        send(context="new-context"),
                        token="new-token",
                    )
                ).json()["result"]["task"]
                assert task["artifacts"][0]["parts"][0]["text"] == "new-agent"
        disabled = Registry(
            [
                base.peer("alice"),
                base.peer("bob"),
                extra.model_copy(update={"state": "disabled"}),
            ],
            {"ALICE": "alice-token", "BOB": "bob-token"},
        )
        store = ReceiptStore(path)
        app = build_peer_app(disabled, store, execute, url="http://localhost/")
        async with app.router.lifespan_context(app):
            assert store.get("new-agent", task["id"]) is not None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                assert (
                    await rpc(
                        client,
                        "GetTask",
                        {"id": task["id"], "tenant": "aerys"},
                        token="new-token",
                    )
                ).status_code == 401
                assert (await rpc(client, "SendMessage", send())).json()["result"][
                    "task"
                ]["status"]["state"] == "TASK_STATE_COMPLETED"

    asyncio.run(run())
