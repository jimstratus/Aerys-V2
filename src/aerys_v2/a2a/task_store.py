"""Single-process durable protocol receipts, NOT assignments/leases/completion authority.

No native Kanban database is opened. A process lock prevents overlapping executors.
Unfinished receipts are retained as uncertain on restart and never replayed.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from a2a.types import Role, Task, TaskState
from google.protobuf.json_format import MessageToDict, ParseDict


class ReceiptStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            os.close(fd)
            raise PermissionError("receipt file must be private and owned by this user")
        os.close(fd)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        if self.db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
            self.db.close()
            raise ValueError("Unsupported receipt schema version")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS contexts (id TEXT PRIMARY KEY, peer TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY, peer TEXT NOT NULL, message TEXT NOT NULL,
                digest TEXT NOT NULL, context TEXT NOT NULL, task TEXT NOT NULL,
                trace TEXT NOT NULL, UNIQUE(peer, message));
            CREATE TABLE IF NOT EXISTS bindings (
                direction TEXT NOT NULL, protocol_id TEXT NOT NULL, authority TEXT NOT NULL,
                work_id TEXT NOT NULL, PRIMARY KEY(direction, protocol_id));
            CREATE TABLE IF NOT EXISTS dispatches (
                id TEXT PRIMARY KEY, peer TEXT NOT NULL, context TEXT NOT NULL,
                message TEXT NOT NULL, digest TEXT NOT NULL, state TEXT NOT NULL,
                remote_id TEXT, task TEXT, trace TEXT NOT NULL DEFAULT '{}');
        """)
        # Additive migration for pre-v1 development receipts; retain IDs/results.
        if "trace" not in {
            r["name"] for r in self.db.execute("PRAGMA table_info(dispatches)")
        }:
            self.db.execute(
                "ALTER TABLE dispatches ADD COLUMN trace TEXT NOT NULL DEFAULT '{}'"
            )
        self.db.execute("PRAGMA user_version=1")
        self._lock = None

    def start(self):
        if self._lock is not None:
            raise RuntimeError("receipt store already started")
        lock = os.fdopen(
            os.open(
                str(self.path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            ),
            "a",
        )
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lock.close()
            raise RuntimeError("receipt executor already active") from None
        self._lock = lock
        for row in self.db.execute("SELECT * FROM receipts").fetchall():
            task = ParseDict(json.loads(row["task"]), Task())
            if task.status.state in {
                TaskState.TASK_STATE_SUBMITTED,
                TaskState.TASK_STATE_WORKING,
            }:
                task.status.state = TaskState.TASK_STATE_INPUT_REQUIRED
                task.status.message.role = Role.ROLE_AGENT
                task.status.message.message_id = uuid4().hex
                task.status.message.parts.add(
                    text="Execution uncertain after restart; reconcile before replacement."
                )
                self.save(task)

    def close(self):
        self.db.close()
        if self._lock:
            self._lock.close()
            self._lock = None

    def reserve(self, peer, message, context, digest, trace):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM receipts WHERE peer=? AND message=?", (peer, message)
            ).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ValueError("message ID reused with different content")
                self.db.execute("COMMIT")
                return ParseDict(json.loads(row["task"]), Task()), False
            owner = self.db.execute(
                "SELECT peer FROM contexts WHERE id=?", (context,)
            ).fetchone()
            if owner and owner["peer"] != peer:
                raise PermissionError("context belongs to another peer")
            self.db.execute(
                "INSERT OR IGNORE INTO contexts VALUES (?,?)", (context, peer)
            )
            task = Task(id=uuid4().hex, context_id=context)
            task.status.state = TaskState.TASK_STATE_SUBMITTED
            self.db.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?)",
                (
                    task.id,
                    peer,
                    message,
                    digest,
                    context,
                    json.dumps(MessageToDict(task)),
                    json.dumps(trace),
                ),
            )
            self.db.execute("COMMIT")
            return task, True
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def save(self, task):
        task.status.timestamp.GetCurrentTime()
        self.db.execute(
            "UPDATE receipts SET task=? WHERE id=?",
            (json.dumps(MessageToDict(task)), task.id),
        )

    def get(self, peer, task_id):
        row = self.db.execute(
            "SELECT task FROM receipts WHERE peer=? AND id=?", (peer, task_id)
        ).fetchone()
        return ParseDict(json.loads(row["task"]), Task()) if row else None

    def list_page(self, peer, context="", status="", *, offset=0, limit=50):
        where = "peer=? AND (?='' OR context=?) AND (?='' OR json_extract(task,'$.status.state')=?)"
        args = (peer, context, context, status, status)
        total = self.db.execute(
            "SELECT COUNT(*) FROM receipts WHERE " + where, args
        ).fetchone()[0]
        rows = self.db.execute(
            "SELECT task FROM receipts WHERE "
            + where
            + " ORDER BY id LIMIT ? OFFSET ?",
            (*args, limit, offset),
        )
        return [ParseDict(json.loads(r["task"]), Task()) for r in rows], total

    def prepare_dispatch(self, dispatch_id, peer, text, trace=None):
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.db.execute(
            "INSERT OR IGNORE INTO dispatches (id,peer,context,message,digest,state,trace) VALUES (?,?,?,?,?,?,?)",
            (
                dispatch_id,
                peer,
                uuid4().hex,
                uuid4().hex,
                digest,
                "prepared",
                json.dumps(trace or {}),
            ),
        )
        row = self.dispatch(dispatch_id)
        if row is None:
            raise RuntimeError("Dispatch persistence failed")
        if row["peer"] != peer or row["digest"] != digest:
            raise ValueError("dispatch ID reused with different request")
        return row, row["state"] == "prepared"

    def dispatch(self, dispatch_id):
        row = self.db.execute(
            "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)
        ).fetchone()
        return dict(row) if row else None

    def bind_work(self, direction, protocol_id, reference):
        if direction not in {"inbound", "outbound"}:
            raise ValueError("Invalid protocol direction")
        table = "receipts" if direction == "inbound" else "dispatches"
        if not self.db.execute(
            f"SELECT 1 FROM {table} WHERE id=?", (protocol_id,)
        ).fetchone():
            raise KeyError(protocol_id)
        self.db.execute(
            "INSERT OR IGNORE INTO bindings VALUES (?,?,?,?)",
            (direction, protocol_id, reference.authority, reference.work_id),
        )
        if self.work_reference(direction, protocol_id) != reference:
            raise ValueError("Protocol identity already bound to different work")

    def work_reference(self, direction, protocol_id):
        from aerys_v2.coordination.ledger import LedgerReference

        row = self.db.execute(
            "SELECT authority,work_id FROM bindings WHERE direction=? AND protocol_id=?",
            (direction, protocol_id),
        ).fetchone()
        return LedgerReference(row["authority"], row["work_id"]) if row else None

    def healthy(self):
        return self._lock is not None and self.db.execute("SELECT 1").fetchone()[0] == 1

    def has_message(self, peer, message):
        return (
            self.db.execute(
                "SELECT 1 FROM receipts WHERE peer=? AND message=?", (peer, message)
            ).fetchone()
            is not None
        )

    def claim_dispatch(self, dispatch_id):
        return (
            self.db.execute(
                "UPDATE dispatches SET state='sending' WHERE id=? AND state='prepared'",
                (dispatch_id,),
            ).rowcount
            == 1
        )

    def record_dispatch(self, dispatch_id, state, task=None):
        self.db.execute(
            "UPDATE dispatches SET state=?, remote_id=COALESCE(?, remote_id), task=COALESCE(?, task) WHERE id=?",
            (
                state,
                task.id if task else None,
                json.dumps(MessageToDict(task)) if task else None,
                dispatch_id,
            ),
        )
