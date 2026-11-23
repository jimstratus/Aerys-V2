import asyncio
import json
from unittest.mock import patch

import httpx
import pytest
from a2a.types import Task, TaskState
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceState, use_span
from test_peer_server import registry, rpc, send

from aerys_v2.a2a.client import PeerClient
from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.transports.a2a_server import build_peer_app


def test_original_remote_id_is_immutable_and_terminal_results_do_not_regress(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.db")
    row, _ = store.prepare_dispatch("dispatch", "peer", "hello")
    complete = Task(id="original", context_id=row["context"])
    complete.status.state = TaskState.TASK_STATE_COMPLETED
    complete.artifacts.add(artifact_id="evidence").parts.add(text="result")
    store.record_dispatch("dispatch", "accepted", complete)
    replacement = Task(id="replacement", context_id=row["context"])
    replacement.status.state = TaskState.TASK_STATE_WORKING
    with pytest.raises(ValueError):
        store.record_dispatch("dispatch", "accepted", replacement)
    stale = Task(id="original", context_id=row["context"])
    stale.status.state = TaskState.TASK_STATE_WORKING
    store.record_dispatch("dispatch", "accepted", stale)
    store.record_dispatch("dispatch", "uncertain")
    after = store.dispatch("dispatch")
    assert after["remote_id"] == "original"
    assert after["state"] == "accepted"
    assert json.loads(after["task"])["status"]["state"] == "TASK_STATE_COMPLETED"
    assert json.loads(after["task"])["artifacts"][0]["parts"][0]["text"] == "result"
    store.close()


def test_outbound_drops_tracestate_from_headers_and_receipts(tmp_path):
    async def run():
        store = ReceiptStore(tmp_path / "receipts.db")
        async with httpx.AsyncClient() as http:
            client = PeerClient(registry(), store, http)
            with use_span(
                NonRecordingSpan(
                    SpanContext(
                        1,
                        2,
                        False,
                        trace_state=TraceState([("vendor", "private-value")]),
                    )
                )
            ):
                headers = client._context(registry().peer("alice")).service_parameters
                assert "tracestate" not in headers
            # Real submit discovery refusal is enough to inspect prepared receipt.
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda req: httpx.Response(401))
            ) as mock:
                with use_span(
                    NonRecordingSpan(
                        SpanContext(
                            1,
                            2,
                            False,
                            trace_state=TraceState([("vendor", "private-value")]),
                        )
                    )
                ):
                    with pytest.raises(httpx.HTTPStatusError):
                        await PeerClient(registry(), store, mock).submit(
                            "alice", "hi", dispatch_id="trace"
                        )
            assert "private-value" not in store.dispatch("trace")["trace"]
        store.close()

    asyncio.run(run())


def test_executor_exception_text_is_not_in_peer_spans(tmp_path):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    async def run():
        async def execute(req):
            raise RuntimeError("password=synthetic-credential")

        store = ReceiptStore(tmp_path / "receipts.db")
        app = build_peer_app(registry(), store, execute, url="http://localhost/")
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as http:
                task = (await rpc(http, "SendMessage", send())).json()["result"]["task"]
                assert task["status"]["state"] == "TASK_STATE_FAILED"

    with patch("aerys_v2.transports.a2a_server.tracer", provider.get_tracer("privacy")):
        asyncio.run(run())
    assert "synthetic-credential" not in str(
        [
            (s.attributes, s.events, s.status.description)
            for s in exporter.get_finished_spans()
        ]
    )
    provider.shutdown()


def test_message_only_terminal_result_survives_later_bare_snapshot(tmp_path):
    # A terminal task can carry its answer in status.message with no artifacts.
    # A later same-state poll (equal or newer timestamp) that omits the message
    # must not erase that evidence, while newly supplied artifacts still land.
    store = ReceiptStore(tmp_path / "receipts.db")
    row, _ = store.prepare_dispatch("dispatch", "peer", "hello")
    done = Task(id="original", context_id=row["context"])
    done.status.state = TaskState.TASK_STATE_COMPLETED
    done.status.timestamp.seconds = 100
    done.status.message.message_id = "reply"
    done.status.message.parts.add(text="final answer")
    store.record_dispatch("dispatch", "accepted", done)

    bare = Task(id="original", context_id=row["context"])
    bare.status.state = TaskState.TASK_STATE_COMPLETED
    bare.status.timestamp.seconds = 100
    store.record_dispatch("dispatch", "accepted", bare)
    stored = json.loads(store.dispatch("dispatch")["task"])
    assert stored["status"]["message"]["parts"][0]["text"] == "final answer"

    newer = Task(id="original", context_id=row["context"])
    newer.status.state = TaskState.TASK_STATE_COMPLETED
    newer.status.timestamp.seconds = 200
    newer.artifacts.add(artifact_id="late").parts.add(text="artifact")
    store.record_dispatch("dispatch", "accepted", newer)
    stored = json.loads(store.dispatch("dispatch")["task"])
    assert stored["status"]["message"]["parts"][0]["text"] == "final answer"
    assert stored["artifacts"][0]["parts"][0]["text"] == "artifact"
    store.close()


def test_peer_adapter_is_stateless_and_never_fetches_images():
    # Review findings on PR #1: the per-task peer graph must not claim durable
    # memory, and peer text naming a Discord CDN image must not trigger a fetch.
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from aerys_v2.a2a.contracts import PeerRequest
    from aerys_v2.a2a.executor import PeerAssistant

    seen = []

    class Capture(FakeListChatModel):
        def invoke(self, prompt, *args, **kwargs):
            seen.append(prompt)
            return super().invoke(prompt, *args, **kwargs)

    request = PeerRequest(
        principal="alice",
        task_id="t",
        context_id="c",
        thread_id="peer:alice:c",
        text="look https://cdn.discordapp.com/attachments/1/2/cat.png",
    )
    with patch(
        "aerys_v2.images.inline_current_images",
        side_effect=AssertionError("peer graph fetched an image"),
    ):
        reply = PeerAssistant(Capture(responses=["ok"]))._ask(request)
    assert reply == "ok"
    system = seen[0][0].content
    assert "stateless" in system
    assert "memory is durable" not in system
    assert "you see it yourself" not in system


def test_owner_graph_defaults_still_inline_images_and_claim_memory():
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from aerys_v2.factory import build_graph
    from aerys_v2.service import ask

    seen = []

    def spy(prompt, **kwargs):
        seen.append(prompt)
        return prompt

    with patch("aerys_v2.images.inline_current_images", side_effect=spy):
        ask(
            build_graph(FakeListChatModel(responses=["ok"]), "soul"),
            "hello",
            identity={
                "user_id": "u",
                "display_name": "U",
                "privacy_context": "private",
            },
            thread_id="owner-thread",
        )
    assert seen, "default graph must keep inlining images"
    assert "memory is durable" in seen[0][0].content
