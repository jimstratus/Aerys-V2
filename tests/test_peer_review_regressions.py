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
