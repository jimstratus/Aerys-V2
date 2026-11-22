"""Regressions for the PR #3 (combined A2A + pilot) review findings."""

import asyncio
import threading

import httpx
import pytest
from a2a.types import Task, TaskState
from test_peer_server import registry, rpc, send

from aerys_v2.a2a.client import PeerClient
from aerys_v2.a2a.contracts import ReconciliationRequired
from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.transports.a2a_server import build_peer_app


def _client(tmp_path):
    store = ReceiptStore(tmp_path / "client.db")
    row, _ = store.prepare_dispatch("d1", "alice", "hello")
    return PeerClient(registry(), store, httpx.AsyncClient()), store, row


# --- client.py: pin identity before validating parts; reject non-text messages


def test_unsupported_artifact_still_pins_receiver_identity(tmp_path):
    client, store, row = _client(tmp_path)
    task = Task(id="original", context_id=row["context"])
    task.status.state = TaskState.TASK_STATE_COMPLETED
    task.artifacts.add(artifact_id="a").parts.add(url="https://example.invalid/f")
    with pytest.raises(ReconciliationRequired):
        client._record("d1", task, row["context"])
    # Recovery must now GetTask the observed task, never ListTasks-and-rebind.
    assert store.dispatch("d1")["remote_id"] == "original"
    other = Task(id="impostor", context_id=row["context"])
    other.status.state = TaskState.TASK_STATE_COMPLETED
    other.artifacts.add(artifact_id="b").parts.add(text="looks fine")
    with pytest.raises(ValueError, match="immutable"):
        client._record("d1", other, row["context"])
    store.close()


def test_non_text_status_message_is_not_accepted_as_completion(tmp_path):
    client, store, row = _client(tmp_path)
    task = Task(id="original", context_id=row["context"])
    task.status.state = TaskState.TASK_STATE_COMPLETED
    task.status.message.message_id = "m"
    task.status.message.parts.add(url="https://example.invalid/answer.pdf")
    with pytest.raises(ReconciliationRequired, match="Unsupported"):
        client._record("d1", task, row["context"])
    assert store.dispatch("d1")["task"] is None  # no fake "accepted" completion
    store.close()


# --- task_store.py: terminal transition supersedes timestamp ordering


def test_untimestamped_completion_supersedes_timestamped_working(tmp_path):
    store = ReceiptStore(tmp_path / "r.db")
    row, _ = store.prepare_dispatch("d1", "peer", "hello")
    working = Task(id="t", context_id=row["context"])
    working.status.state = TaskState.TASK_STATE_WORKING
    working.status.timestamp.seconds = 1_000
    store.record_dispatch("d1", "accepted", working)
    done = Task(id="t", context_id=row["context"])
    done.status.state = TaskState.TASK_STATE_COMPLETED  # peer omitted timestamp
    done.artifacts.add(artifact_id="a").parts.add(text="final")
    stored = store.record_dispatch("d1", "accepted", done)
    assert stored.status.state == TaskState.TASK_STATE_COMPLETED
    # ...but a stale NONTERMINAL snapshot still can't roll back a newer one.
    stale = Task(id="t", context_id=row["context"])
    stale.status.state = TaskState.TASK_STATE_WORKING
    assert store.record_dispatch("d1", "accepted", stale).status.state == (
        TaskState.TASK_STATE_COMPLETED
    )
    store.close()


# --- a2a_server.py: size bound after scrubbing; ListTasks includeArtifacts


async def _serve(tmp_path, execute):
    app = build_peer_app(
        registry(), ReceiptStore(tmp_path / "s.db"), execute, url="http://localhost/"
    )
    return app


def test_result_bound_is_enforced_after_redaction(tmp_path):
    async def run():
        async def execute(_):
            return "token=x " * 8192  # exactly 64 KiB raw; ~2x after redaction

        app = await _serve(tmp_path, execute)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as c:
                task = (await rpc(c, "SendMessage", send())).json()["result"]["task"]
        assert task["status"]["state"] == "TASK_STATE_FAILED"
        assert "artifacts" not in task

    asyncio.run(run())


def test_list_tasks_honors_include_artifacts(tmp_path):
    async def run():
        async def execute(_):
            return "the answer"

        app = await _serve(tmp_path, execute)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost"
            ) as c:
                await rpc(c, "SendMessage", send())
                bare = (await rpc(c, "ListTasks", {"tenant": "aerys"})).json()
                full = (
                    await rpc(
                        c, "ListTasks", {"tenant": "aerys", "includeArtifacts": True}
                    )
                ).json()
        assert "artifacts" not in bare["result"]["tasks"][0]
        assert (
            full["result"]["tasks"][0]["artifacts"][0]["parts"][0]["text"]
            == "the answer"
        )

    asyncio.run(run())


# --- executor.py: repeated cancellation must not release before the worker exits


def test_repeated_cancel_waits_for_worker_thread(monkeypatch):
    from aerys_v2.a2a.executor import PeerAssistant

    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocking_ask(self, request):
        started.set()
        release.wait(10)
        finished.set()
        return "late"

    monkeypatch.setattr(PeerAssistant, "_ask", blocking_ask)

    async def run():
        waiter = asyncio.create_task(PeerAssistant(model=None)(object()))
        while not started.is_set():
            await asyncio.sleep(0.01)
        for _ in range(3):  # shutdown paths can cancel more than once
            waiter.cancel()
            await asyncio.sleep(0.05)
            assert not waiter.done(), "released while the worker thread still runs"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert finished.is_set()

    asyncio.run(run())
