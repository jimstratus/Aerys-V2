"""Abrupt death of a disposable executor, never a live gateway/worker."""

import json
import subprocess
import sys

from a2a.types import TaskState

from aerys_v2.a2a.task_store import ReceiptStore


def test_abrupt_process_exit_reopens_original_ids_without_replay(tmp_path):
    path = tmp_path / "receipts.db"
    code = """
import json, os, sys
from pathlib import Path
from a2a.types import TaskState
from aerys_v2.a2a.task_store import ReceiptStore
store = ReceiptStore(Path(sys.argv[1]))
store.start()
working, _ = store.reserve('alice', 'working', 'working-context', 'digest-one', {})
working.status.state = TaskState.TASK_STATE_WORKING
store.save(working)
complete, _ = store.reserve('alice', 'complete', 'complete-context', 'digest-two', {})
complete.status.state = TaskState.TASK_STATE_COMPLETED
complete.artifacts.add(artifact_id='result').parts.add(text='retained result')
store.save(complete)
print(json.dumps({'working': working.id, 'complete': complete.id}), flush=True)
os._exit(23)
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 23
    ids = json.loads(result.stdout)
    store = ReceiptStore(path)
    store.start()  # kernel released only the disposable child's process lock
    working = store.get("alice", ids["working"])
    complete = store.get("alice", ids["complete"])
    assert working.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert complete.status.state == TaskState.TASK_STATE_COMPLETED
    assert complete.artifacts[0].parts[0].text == "retained result"
    duplicate, fresh = store.reserve(
        "alice", "working", "working-context", "digest-one", {}
    )
    assert not fresh and duplicate.id == working.id
    assert duplicate.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    store.close()
