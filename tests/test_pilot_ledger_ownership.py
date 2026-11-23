"""Pilot ownership boundary: receipts correlate work; native owners alone mutate it.

These tests exercise the reviewed source, not a second assignment/lease ledger.
All state and identities below are disposable fixtures.
"""

import pytest
from a2a.types import TaskState

from aerys_v2.a2a.task_store import ReceiptStore
from aerys_v2.coordination.ledger import LedgerReference, LedgerView


def test_wrong_authority_is_rejected_before_owner_read():
    calls = []
    def read(work_id):
        calls.append(work_id)
        return {"id": work_id}

    view = LedgerView("native-board", read)
    with pytest.raises(PermissionError, match="authority"):
        view.observe(LedgerReference("another-board", "original-task"))
    assert calls == []


def test_owner_cannot_substitute_original_work_identity():
    view = LedgerView("native-board", lambda _: {"id": "replacement-task"})
    with pytest.raises(ValueError, match="different work item"):
        view.observe(LedgerReference("native-board", "original-task"))


def test_reassignment_is_observed_from_owner_not_cached_receipts():
    record = {"id": "original-task", "assignee": "first", "current_run_id": 1}
    view = LedgerView("native-board", lambda _: dict(record))
    reference = LedgerReference("native-board", "original-task")
    assert view.observe(reference)["current_run_id"] == 1
    record.update(assignee="replacement", current_run_id=2)
    assert view.observe(reference) == record
    # The adapter has no claim, complete, reassign, cancel or renewal interface.
    for operation in ("claim", "complete", "reassign", "cancel", "renew"):
        assert not hasattr(view, operation)


def test_owner_unavailable_does_not_fabricate_assignment_or_completion():
    def unavailable(_):
        raise ConnectionError("isolated owner unavailable")

    view = LedgerView("native-board", unavailable)
    with pytest.raises(ConnectionError):
        view.observe(LedgerReference("native-board", "original-task"))


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
def test_protocol_binding_is_immutable_across_restart(tmp_path, direction):
    path = tmp_path / "receipts.db"
    store = ReceiptStore(path)
    store.start()
    reference = LedgerReference("native-board", "original-task")
    if direction == "inbound":
        task, fresh = store.reserve("peer", "message", "context", "digest", {})
        assert fresh
        protocol_id = task.id
        task.status.state = TaskState.TASK_STATE_COMPLETED
        store.save(task)
    else:
        protocol_id = "dispatch"
        store.prepare_dispatch(protocol_id, "peer", "fixture text")
    store.bind_work(direction, protocol_id, reference)
    store.close()

    store = ReceiptStore(path)
    store.start()
    try:
        assert store.work_reference(direction, protocol_id) == reference
        store.bind_work(direction, protocol_id, reference)
        for replacement in (
            LedgerReference("native-board", "replacement-task"),
            LedgerReference("another-board", "original-task"),
        ):
            with pytest.raises(ValueError, match="already bound"):
                store.bind_work(direction, protocol_id, replacement)
        assert store.work_reference(direction, protocol_id) == reference
        if direction == "inbound":
            # Transport completion does not change the referenced native work.
            saved = store.get("peer", protocol_id)
            assert saved is not None
            assert saved.status.state == TaskState.TASK_STATE_COMPLETED
            assert store.get("different-peer", protocol_id) is None
    finally:
        store.close()


def test_binding_requires_an_existing_protocol_record(tmp_path):
    store = ReceiptStore(tmp_path / "receipts.db")
    try:
        with pytest.raises(KeyError):
            store.bind_work("inbound", "missing", LedgerReference("native-board", "task"))
        assert store.work_reference("inbound", "missing") is None
    finally:
        store.close()
