# Aerys A2A foundation (source-only)

This is an **optional separate text-only peer listener**, not a deployed EOTIR
coordinator. Existing `aerys-v2`, owner `/ask`, Kael callbacks, household tools and
personal-memory behavior are unchanged. No production DB, profile configuration,
service or infrastructure is changed by this package.

## Supported interface

- Python >=3.11; qualified on isolated CPython 3.11.15.
- Official `a2a-sdk[http-server]==1.3.0`, explicit `httpx>=0.28.1,<0.29`;
  `uv.lock` resolves dependencies. SDK version is **not** wire version.
- A2A **1.0 JSONRPC**: canonical `/.well-known/agent-card.json`, `SendMessage`,
  `GetTask`, bounded `ListTasks`, and `CancelTask`. SDK parsing/serialization,
  version checks, Agent Card compatibility parsing and JSON-RPC transport are
  used directly. No hand-written imitation of the wire schema.
- Text-only new tasks; no continued task IDs, streaming, push, binary/data
  parts, timestamp filtering or extended cards. Unsupported operations fail.
- `returnImmediately` returns accepted durable task identity; otherwise the
  caller waits. HTTP disconnection does not cancel the execution.
- Task completion records the **restricted assistant exchange**, not completion
  of an authoritative work item, approval, publication or deployment.

## Modules and trust

`a2a/registry.py` validates configuration, distinct machine credentials, grants,
active/draining/disabled state, exact endpoint, expected card name and tenant.
Cards/capabilities are discovery claims, not permission grants. Inbound and
outbound credential references can be different. Use separate tokens per peer
and direction in production; neither belongs in source/config JSON.

`transports/a2a_server.py` authenticates **before** SDK parsing. Missing/wrong/
disabled credentials are denied. Authenticated identity, not model text or display
name, owns tasks and contexts. Cross-peer reads/cancels/context reuse are denied.
Raw body, active execution count, response text and list page sizes are bounded.
Agent Card and `/healthz` are public; keep the listener private. `/healthz` checks
listener lifetime and receipt-store read reachability, **not** model/peer/ledger
health. No owner `/ask` route exists on this app.

`a2a/executor.py` invokes the existing `build_graph`/`ask` behavior with a distinct
`peer:<id>` identity, an isolated thread key and an empty action allowlist. It
creates a new restricted graph per task, with **no owner soul, personal memory,
context callbacks, tools, voice, turn recorder or persistent chat checkpoint**.
Context IDs correlate exchanges; this initial adapter does not promise multi-turn
memory. Its dedicated OpenAI-compatible model configuration cannot select a
Claude CLI subprocess with implicit tools. Inject an explicitly restricted model
adapter in other integrations; never pass the owner's configured graph/model.
The existing owner's model/provider behavior is untouched.

`a2a/client.py` pins POSTs to the enrolled endpoint. Card URL/name/tenant/version
mismatch is a refusal, not a redirect. Inject an `httpx.AsyncClient` with
`follow_redirects=False`; use `trust_env=False` on private machine links. No
URL supplied by model text or an Agent Card is followed to a different host.
HTTPS is required except loopback, or explicit per-endpoint private-network HTTP
opt-in. That opt-in requires an independently encrypted/private network; it does
not create one. Supply stable dispatch IDs and retain the receipt volume.

## Durability and uncertainty

`a2a/task_store.py` is SQLite **protocol receipt/mapping storage only**. It has no
assignee, backup, lease, completion contract or independently assignable queue.
It never opens a Hermes runtime/Kanban DB. Schema v1 is initialized on a new
private path; pre-v1 development receipts receive additive trace-column migration.
Unknown schema versions are refused. Use a private persistent **local** filesystem,
one listener process, no replicated/NFS locking assumptions. The file is 0600;
execution lifetime is protected by an exclusive process lock. A second listener
cannot recover/replay the first listener's tasks.

Inbound message ID + authenticated peer is durable dedup identity, with a digest
rejecting changed payloads. The receipt is committed before scheduling. On restart,
submitted/working receipts become `INPUT_REQUIRED` with an **uncertain execution**
notice, retaining original IDs; they are not replayed. Completed results survive.
Duplicate sends return the original task, including its uncertainty.

Outbound preparation stores distinct dispatch/message/context/remote-task IDs.
An atomic claim allows only one send for a dispatch ID, even across client
instances. Once send begins, *any* timeout, failed response, cancellation or restart
must reconcile the original dispatch. `GetTask` uses the receiver ID when known;
otherwise `ListTasks` must find one unique task for the persisted exact context.
Zero/multiple tasks or lost remote history remain unresolved: **no blind send**.
Hermes' current in-memory remote task store can lose history across restart; local
durable receipts cannot manufacture missing remote evidence.

Only unstarted `SUBMITTED` tasks can be canceled by this executor; cancellation is
idempotent there. Running/terminal/uncertain tasks refuse cancellation. Python
thread cancellation cannot prove effects stopped. The default assistant drains its
worker before releasing its execution lease. A refused/unknown cancellation never
authorizes replacement. Forced process death produces uncertainty on next start,
not a false success or automatic replay.

## One authoritative ledger

`coordination/ledger.py` provides an immutable `{authority, work_id}` reference and
a constrained injected read adapter. `ReceiptStore.bind_work` retains that original
reference without copying ownership/status into an independently writable task.
Protocol results never close ledger work. Incoming metadata cannot choose ledger
authority. The foundation exposes **no ledger complete/claim/reassign mutation**.

The parallel qualification found no verified remotely scoped CAS/fencing interface
for native Kanban. Automatic reassignment/supervision therefore remains disabled;
its owner must qualify the supported integration and original effects first. No
raw cross-runtime DB writes, API guesswork or unsafe fallback ledger is supplied.

## Context and privacy

W3C `traceparent` flows client -> HTTP -> async task -> worker thread/`ask`.
Only validated trace context is persisted, not Authorization/baggage. Outbound
receipt context parents reconciliation after restart. `tracestate` and baggage
are deliberately dropped at this privacy boundary. Spans carry
`eotir.a2a.task.id`, `eotir.agent.id` and authenticated `a2a.peer.id`; these IDs are
not metric labels. Authoritative `eotir.task.id`/board/attempt references belong to
the future supported ledger adapter, not to the JSON-RPC request ID.

Known peer/model credentials and common labeled credential values are scrubbed
before model input, transport output and result persistence. Exceptions return
fixed nonsecret messages. Prompts are not stored in receipts (digest only), and
unknown remote metadata/history/nontext artifacts are not persisted. This is
**bounded credential redaction, not general PII/DLP qualification**. No diagnostic
payload collector/exporter is enabled by the standalone launcher. Do not enable
raw LangChain/ask payload export without the separately reviewed source-redaction
policy. Backend/export resource identity is the observability lane's responsibility.

## Reproduce tests

```
UV_PROJECT_ENVIRONMENT=/path/to/isolated-venv uv sync --frozen --dev
/path/to/isolated-venv/bin/python -m pytest -q tests/test_peer_*.py
/path/to/isolated-venv/bin/python -m pytest -q
uvx pyright --pythonpath /path/to/isolated-venv/bin/python \
  src/aerys_v2/a2a src/aerys_v2/coordination src/aerys_v2/transports/a2a_server.py
uvx ruff check --isolated --select E4,E7,E9,F,I \
  src/aerys_v2/a2a src/aerys_v2/coordination src/aerys_v2/transports/a2a_server.py \
  tests/test_peer_*.py
```

The installed-Hermes outbound-helper integration test is opt-in: resolve
`hermes --print-runtime-command`, preserve that launcher's bootstrap, and set
`HERMES_RUNTIME_PYTHON` and `HERMES_SOURCE_ROOT` to the verified interpreter/source.
It calls actual installed protocol/HTTP helpers against an ephemeral loopback
listener, without editing live profiles, audit logs or databases. Ordinary CI
skips that one test when no installed Hermes runtime is supplied.

The live source-client smoke reached **Rook (RK-1), tenant rook** using the existing
**Vector** credential, not a newly enrolled Aerys principal: receiver task
`task-f8e9c66b28b6457f`, context `0971331838f942d7b1bacbfb280f98f1`, nonce
`VCT-AERYS-20261028-1`. A second invocation read back the same task with `GetTask`.
This does **not** establish production Aerys enrollment, reverse live connectivity,
peer isolation inside Hermes, or remote durability/fencing. Fixtures establish
Aerys' own peer isolation; Hermes isolation gaps remain integration gates.

Exact baseline `0111e62ffa2289e4bbae1c9e7fce142e944c47e1` full suite: **2109 passed,
11 failed**. The same failure IDs were reproduced with the new locked dependencies:
`test_action_specialist` tool arming, `test_oauth_model` builtin tools,
`test_tiering` (seven cases), and `test_web_read` (two cases). They are retained
baseline debt, not silently fixed out of scope. Final candidate results and exact
source/build hashes belong in the release receipt, not a claim of all-green CI.

## Grounding

- Hermes current A2A docs actually retrieved:
  https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a
- Installed Hermes protocol/adapter/tools inspected under
  `plugins/platforms/a2a/`; qualify its exact revision at integration time.
- Actual installed SDK 1.3.0 source inspected: `a2a/server/routes`,
  `server/request_handlers/request_handler.py`, `client/transports/jsonrpc.py`,
  `client/card_resolver.py`, `types/a2a_pb2.py`. This release uses protobuf types and
  route factories, not historical `a2a.server.apps.A2AFastAPIApplication` imports.
- Canonical source home: https://github.com/jimstratus/Aerys-V2 .
