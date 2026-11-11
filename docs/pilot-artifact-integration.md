# Pinned Aerys pilot artifact integration — t_b2bdfb2c

This phase consumes R1 of the reconciliation gap manifest and the reviewed
`t_c776ba93` source, without repeating its implementation/review. Base checkpoint:
`905122d98c9929ba5df7b6eae4f101f2ef6af469`. Runtime source remains
`jimstratus/Aerys-V2` **42e7abb5643af9a1152fa541c9147a974249d3b9**.
No runtime module, dependency, service template, configuration template, or existing
source lock is modified. The build stages the original reviewed wheel, not a
replacement wheel from this integration branch. The service/config remain inert
examples; neither is installed or enabled.

## Imported original artifacts

Every artifact below belongs to t_c776ba93 and the runtime source revision above.
SHA-256 is computed and checked before any output directory is created.

| Artifact | SHA-256 | Original source |
| --- | --- | --- |
| aerys_v2-0.1.0-py3-none-any.whl | e5da2c97c7186c0575bd3d8e9062a33c1c79f945ac5bad2e37dda97bf41e1a37 | Parent attachment |
| requirements.lock | 8986308962fe6c4032c704e9098df755a72a9ac2d09c4f4a8b39ad77952afd5e | Parent attachment |
| aerys-runtime-manifest.json | 89035b692137818663ce0077051ed3f949198daf56105c29d98503069b3e8145 | Parent attachment (original historical receipt, not updated) |
| uv.lock | a8c63c0b17dc39fda2628aa2d9f33d4c73694e22a6f6fc9101da1bed81ce4b99 | Pinned Git tree |
| aerys-a2a.service | b4437e6d4008d361d84728cbb2214062ed1642bda4e96dc955d01a39756b3f21 | deploy/a2a/ at pinned Git tree |
| config.example.json | cda764403509ae68b11004965730bb3852db666546764aeea08a3068baa9f5cb | deploy/a2a/ at pinned Git tree |

The historical parent manifest's `phase` predates Caliper approval. Preserve its
original bytes; approval continuity is recorded on t_8a235abd/t_ae318e0d, not by
rewriting that receipt. Its five artifact hashes anchor the import; its own digest
is independently pinned in the builder. All 88 packaged Python modules are
compared against BOTH the exact Git tree and the current pilot source; inventories
must match, not just intersect.

## Reproduce the local build

From this worktree, with original attachments accessible locally:

```sh
../qualification-venv/bin/python deploy/a2a/build_pilot.py \
  --inputs /home/keshaun/.hermes/kanban/attachments/t_c776ba93 \
  --output ../b2-pilot-build
```

The output directory must not exist. No download, credentials, model invocation,
package installation or service changes occur. `build-metadata.json` records the
integration commit and dirty status separately from immutable runtime source
identity, builder/source-lock hashes, and every imported artifact's repository,
commit, task, origin, size and digest. Capture the build from a clean committed
checkout for handoff. No output is an authorization to deploy.

## Actual execution

Python 3.11.15, existing isolated qualification venv; original wheel is installed
there. Source suites explicitly use `PYTHONPATH=src`.

```sh
AERYS_PILOT_INPUTS=/home/keshaun/.hermes/kanban/attachments/t_c776ba93 \
PYTHONPATH=src ../qualification-venv/bin/python -m pytest -q \
  tests/test_pilot_build.py tests/test_pilot_ledger_ownership.py tests/test_peer_*.py \
  --junitxml=../b2-focused.xml
# 33 passed, 1 opt-in installed-Hermes skip; exit 0

AERYS_PILOT_INPUTS=/home/keshaun/.hermes/kanban/attachments/t_c776ba93 \
PYTHONPATH=src ../qualification-venv/bin/python -m pytest -q \
  --junitxml=../b2-full.xml
# 2142 passed, 11 failed, 1 skipped; exit 1
```

The first builder test failed before implementation (missing builder). Integration
cases now cover exact original bytes/provenance, manifest/wheel/requirements
corruption rejection before output, and refusing to overwrite an existing build.
Original-artifact integration tests explicitly skip without `AERYS_PILOT_INPUTS`;
a skip is not build qualification. They were enabled here. Baseline failures are
compared programmatically against retained Caliper base XML; full-suite PASS is
not claimed. Full regression/independent review of the subsequent ledger/backend
changes remains with the pre-created downstream lanes and t_064d0301.

## Decisions and scope boundaries

Consume the approved roadmap's pilot-only (Hermes + Aerys + one app), dedicated
Proxmox LXC/Ansible placement, separate database/role, and portable telemetry
boundaries. The reconciliation manifest's pause references are superseded by the
orchestrator's verified release 1569892832871841813; no new hold is inferred.
The subsequent two-tier decision on t_c8eab228 keeps Aerys ledger-read-only and
emits bounded proposals for native-owner application. That implementation belongs
to t_64021bf2, not this import phase. PostgreSQL reuse/backend portability belongs
to t_d2238a30; production PostgreSQL owner receipt remains t_565ee1bb.
No credential, production DB, deployment, protected configuration, external
publication, or other-profile changes in this phase. Review is not release
approval. Preserve this shared worktree for dependent source tasks.
