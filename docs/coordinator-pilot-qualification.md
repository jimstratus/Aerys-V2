# Coordinator pilot qualification checkpoint (t_c8eab228)

This is a **partial qualification checkpoint**, not production acceptance or a
replacement for the source reviews. No runtime Python source is changed. The
pilot's runtime inputs are pinned in `deploy/a2a/pilot-source-lock.json`: install
the original t_c776ba93 wheel with its original hash-locked requirements, not a
new wheel built from this qualification branch. The attached artifact is the
runtime input; this branch adds regression tests, provenance and documentation.

## Ledger ownership and write authority

| Record | Authority | Permitted pilot behavior |
| --- | --- | --- |
| Native board task / assignee / run epoch / dependency / completion | Owning native runtime and its admitted supported API | Observe a scoped projection; preserve original board/task/run IDs. No raw runtime database access. |
| Inbound A2A task, context, peer/message dedup and execution uncertainty | Aerys `ReceiptStore`, private local SQLite | Write protocol receipts only. Distinct authenticated peer context ownership. Completion is transport evidence, **not** native work completion. |
| Outbound dispatch and original receiver task ID | Aerys `ReceiptStore` | Prepare/send/reconcile original receiver ID; retain uncertain send, never infer safe replay. |
| Protocol-to-native-work binding | Immutable `(direction, protocol_id)` mapping | Bind only an existing receipt; never replace original native work/authority. |
| Telemetry/history | Collector and selected trace backend | Observe diagnostics only; exporter failure never releases a native claim. PostgreSQL used for Phoenix is not a native work ledger. |

`LedgerView` verifies authority before calling the supplied owner reader, verifies
the returned original work ID, and does not cache ownership. A newer observation
can reflect owner-performed reassignment; an old protocol result cannot perform
that reassignment. Reader failure propagates instead of inventing closure.

On restart Aerys retains bindings and terminal protocol receipts; unfinished
inbound receipts become execution-uncertain (`INPUT_REQUIRED`) rather than
being replayed. Outbound recovery queries the original receiver ID. A cancel
receipt is not executor fencing. Reassignment must remain disabled until the
owning runtime can atomically check the expected run/epoch and fence effects.

The seven cases in `tests/test_pilot_ledger_ownership.py` cover wrong authority,
wrong work ID, fresh owner reassignment observations/no mutation surface, owner
unavailability, inbound/outbound immutable restart bindings, cross-peer receipt
visibility, and missing-protocol binding rejection. Existing peer process-restart,
failure-semantics and review-regression suites cover uncertainty/no replay and
receiver/terminal-evidence preservation. These are verification of the reviewed
read/reference seam, **not implementation of a remote claim/fencing API**.

## Actual execution

Workspace for this checkpoint is the task-owned `aerys-pilot` worktree. Commands
below use the sibling isolated `qualification-venv` (Python 3.11.15) with the
original wheel installed. Source execution explicitly sets `PYTHONPATH=src`.

- `PYTHONPATH=src ../qualification-venv/bin/python -m pytest -q tests/test_pilot_ledger_ownership.py tests/test_peer_*.py --junitxml=../c8-resume-focused.xml`: exit 0, **28 passed, 1 opt-in skip**.
- `PYTHONPATH=src ../qualification-venv/bin/python -m pytest -q --junitxml=../c8-resume-full.xml`: exit 1, **2137 passed, 11 failed, 1 skipped**. Failure IDs programmatically match the retained Caliper exact-base XML and prior-run source XML; zero new failure IDs. The full suite is **not green**. The existing OAuth test also assumes `/tmp`, incompatible with this environment's required managed scratch directory; no workaround or unrelated source fix was applied.
- From workspace root: `PYTHONPATH=infra-pinned/tools/observability qualification-venv/bin/python -m unittest discover -s infra-pinned/tools/observability -p 'test_*.py' -v`: execution log `c8-infra-tests.log`.
- `qualification-venv/bin/python c8-audit.py`: exit 0. Original wheel and requirements SHA-256 match the lock; all **88** packaged Python modules are byte-identical in wheel, installed package and source. JUnit comparison is in `c8-audit.json`.

The opt-in installed-Hermes test was not run in this checkpoint: it requires
another runtime interpreter. No other profile was modified or invoked.

## Portability: consume, do not duplicate

`t_05a9039a` already exercised the same pinned infrastructure source and Collector
0.164.0 against authenticated Phoenix 20.26.1, Jaeger 2.22.0, then an independent
authenticated Phoenix instance: **36/36 checks**, final separate audit **13/13**.
Only exporter target/auth reference and destination-specific queue changed;
source, receiver, privacy processors and corpus remained fixed. Readback included
exact traces and parent-child metadata, with negative credential/cross-routing
checks. Evidence is `t05-retarget-evidence.zip` and `t05-RETARGET-REPORT.md` in the
shared task workspace / durable originating task. This checkpoint consumes that
local disposable proof; it does not relabel it managed-backend acceptance.

## PostgreSQL reuse and migration boundary

Consume the original PostgreSQL16 non-superuser Phoenix migration/restart fixture
from t_098361d5, the pinned-CA TLS findings in the gap manifest, and the shared
cluster backup/restore findings from t_2ea4868e. Do not repeat broad discovery or
copy another application's grants/credentials. Those receipts establish a
candidate reuse path, **not safe onboarding of a new Phoenix database**.

Required owner receipt is t_565ee1bb: dedicated Phoenix database and nonsuperuser
role; PUBLIC CONNECT/schema isolation; source-IP scoped HBA ahead of catch-all;
name-addressed pinned-CA `verify-full`; capacity, backup membership and actual
restore; negative sibling-access checks from target LXCs. That card is `todo`
with no completed receipt at this checkpoint. No credential was requested or
read and no PostgreSQL connection/migration was attempted by this task.

Migration path: retain the original Phoenix-version lock; let its native
migrations initialize only the owner-provisioned dedicated database, as exercised
in the parent disposable fixture. Never target a sibling/canon/native-ledger
schema. Application startup migrations imply DDL authority: a migrator/runtime
role split remains unqualified. Preserve a verified pre-upgrade backup and
version-specific restore plan; do not assume a source rollback undoes schema
changes. Collector backend retargeting does not migrate retained history.

## Remaining acceptance gaps / handoff to Vector

1. The approved/admitted native owner mutation/fencing API is absent (parent
   `CONTRACT.md` explicitly labels it proposed). Aerys cannot safely implement
   remote claim/reassign/complete by inventing that contract or writing native
   SQLite. The current seam remains fail-closed.
2. Consume t_565ee1bb's completed nonsecret PostgreSQL isolation/migration/backup
   qualification receipt before claiming production reuse.
3. Full-suite baseline debt remains 11 failures; no all-suite PASS claimed.
4. Independent Caliper pilot review is pre-created as t_064d0301. Do not duplicate
   it or release it with a false implementation completion. Exact tested source,
   original wheel/lock digests, JUnit evidence and this checkpoint are preserved
   for that reviewer once the remaining acceptance scope is resolved.

No deployment, source merge, protected-config change, production database write,
credential administration, external model call, SSH or other-profile change.
