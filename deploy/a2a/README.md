# Dedicated Proxmox LXC runtime handoff (Ansible installs, no nested OCI)

This package is source-only. Do **not** install a persistent service on Vector's
host. Stratus selected a dedicated homelab Proxmox LXC provisioned by Ansible;
OS, architecture, networking, model and exact rollout permission still come from
the integration/provisioning owners. This handoff does not authorize provisioning.

## Exact installation inputs

Use the integration owner's **reviewed source commit**, wheel SHA-256,
`uv.lock`/exported hash-locked runtime requirements and config fingerprint. Never
install moving `master`, copy Vector's OAuth credentials or borrow another
agent's bearer identity. The standalone launcher is `aerys-v2-a2a`; existing
`aerys-v2` owner runtime is a different executable and must not be enabled as an
accidental substitute.

Runtime: CPython >=3.11 (local qualification 3.11.15, Linux x86_64), official
`a2a-sdk[http-server]==1.3.0`, FastAPI/Starlette/httpx/protobuf resolved in `uv.lock`.
Wheel is pure Python, but dependencies include native wheels. Ansible must qualify
its actual LXC OS/architecture and interpreter; local x86_64 success does not
qualify ARM or another libc. No Docker socket, nesting or privileged LXC is needed.
The full Aerys dependency lock is retained to preserve the existing application;
this phase does not introduce an unreviewed slim dependency split.

## Layout Ansible should provision

- Dedicated nonlogin user/group `aerys-a2a`; no sudo, host credentials, owner
  home mount or Hermes runtime DB access.
- Immutable versioned release `/opt/aerys/releases/<exact-source-commit>/` with
  isolated `.venv`; `/opt/aerys/current` selects exactly one reviewed release.
- Root-managed `/etc/aerys/a2a.json` based on `config.example.json` (0640 or
  stricter, readable by service group), and `/etc/aerys/a2a.env` secret file
  (root:root 0600; systemd reads EnvironmentFile before dropping privileges).
- Private persistent `/var/lib/aerys-a2a/` (service user, 0700), containing local
  SQLite receipts/lock only. Do not point it at a native Kanban DB or a shared
  network filesystem. Code/config read-only; state on a backed-up volume.
- Install the supplied systemd unit as **a proposal to validate inside the LXC**.
  It binds the configured private listener, runs one worker, uses a nonroot user,
  private temp, restrictive write paths and a bounded restart delay. Local unit
  parsing found the expected missing `/opt/aerys/current` executable because no
  service was installed; remote systemd/LXC hardening is not qualified here.

Example release preparation on the authorized target, using the exact reviewed
source's lock (commands are a handoff, not actions performed on that target):

```
uv venv --python <approved-3.11+-interpreter> /opt/aerys/releases/<commit>/.venv
uv pip install --python /opt/aerys/releases/<commit>/.venv/bin/python \
  --require-hashes -r requirements.lock
uv pip install --python /opt/aerys/releases/<commit>/.venv/bin/python \
  --no-deps /staging/<sha256-verified-aerys-wheel>
```

The release receipt supplies actual checksums. The placeholders above are
intentionally not runnable deployment claims. Use the chosen exact patch version,
approved package mirror/cache and artifact verification procedure.

## Configuration and credentials

The service reads **only** explicit JSON plus referenced environment secrets; it
does not load owner `.env`/Settings. An approved OpenAI-compatible model ID/base URL
and dedicated model key are required for the default restricted assistant. This
chooses a transport adapter, not a paid model purchase; do not invent/select a
model or bill an unapproved provider during provisioning.

Each peer needs stable ID, exact verified card endpoint/name/tenant, independent
inbound/outbound secret references and explicit operation grants. Default
production examples use HTTPS. `allow_insecure_http=true` is per peer and must
have independently approved private/encrypted networking. JSON must contain
**references, never secret values**. Peers receive only the token identifying
their own inbound principal, not owner `API_TOKEN` or another peer's token.

Choose listener private IP/port and HTTPS ingress or approved tailnet path;
`host=0.0.0.0` in the example is for the isolated LXC network, not permission for
public ingress. Ansible must enforce source/destination firewall policy. Protect
model egress and remote A2A destinations. The public card endpoint and unauthenticated
`/healthz` contain no credentials but are not a substitute for network isolation.

## Startup, health and shutdown acceptance

1. Validate config/credentials with `aerys-v2-a2a --config /etc/aerys/a2a.json --check`.
   This does not bind, mutate receipts or call a model.
2. Verify the actual LXC service user, paths, Python/dependency versions, config
   fingerprint, secret-presence flags and systemd hardening support.
3. Start **only after** integration approval. Check `/healthz` from the intended
   monitor, not just localhost. Check the advertised endpoint, tenant and card name.
4. Missing/wrong peer credentials must be 401; cross-peer get/cancel/context use
   must fail. Verify both actual Aerys -> intended Hermes profile and reverse
   Hermes -> Aerys with newly issued scoped principals. Existing Vector/Rook
   smoke evidence is not new Aerys enrollment.
5. Health proves listener/receipt liveness only. Model/peer/collector health and
   external alerts need separate functional monitoring. Keep monitoring independent
   of this conversational process.
6. Graceful stop drains work. Systemd's 120s stop budget is larger than the default
   model request timeout; if forced to kill a process or if downstream work is
   uninterruptible, keep its original task **uncertain**, never automatically
   replay/reassign. Current Hermes task durability/fencing gaps block automated
   replacement. Do not interrupt live workers for an acceptance test.

## Backup, upgrades and rollback

Stop admission and drain before upgrades or snapshots; use coherent SQLite backup
or a stopped-volume snapshot, not an uncoordinated copy of a changing DB. Test
restore on a **different isolated path** and verify original IDs/results, file
permissions and uncertain-state behavior. Receipt schema v1 only adds trace data
from the pre-v1 development layout; unknown versions fail closed. Retain preupgrade
state snapshot and exact old release/config/wheel hashes before switching `current`.
Never roll old code onto a newer incompatible schema; stop and ask the owning
integration lane. No automatic state expiry/cleanup is installed; monitor volume
growth and disk capacity. Never delete dedup receipts while their requests might
be retried.

Restore the old source/config only under the same drain/snapshot gates, verify
health/negative auth and exact task identity, and reconcile uncertain effects before
allowing dispatch. A symlink flip or Ansible success code is not destination
acceptance. Return actual LXC/service/backup/network/config/artifact receipts to
the integration owner; no secret values in Discord or repository evidence.
