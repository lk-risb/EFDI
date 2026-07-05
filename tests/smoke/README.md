# moon-pod — bidirectional smoke test

Proves the v0 core: data moves **both directions** securely through the pod.

## Two layers

### 1. Self-contained loopback (CI — no fabric needed)

Stand up the pod's `zenoh-router` plus a **stub remote Zenoh** in one compose, both mTLS, and
assert:

- **Outbound:** publish on `${PARTNER_NAMESPACE}/test/ping` at the pod side → received at the
  stub-remote side.
- **Inbound:** publish on `release/goat/test/pong` at the stub-remote side → received via
  `goat-cli sub` at the pod side.
- **ACL negative:** publish outside `${PARTNER_NAMESPACE}/**` → **denied** (default-deny holds).
- **Audit:** both deliveries appear as append-only NDJSON lines in `${POD_STATE_DIR}/audit/`.

This is the gate that runs in CI. TODO: implement `loopback.sh` + a stub-remote compose overlay.

### 2. Live EFDI-sandbox validation (slow step, on the validation host)

Run the real `first-boot.sh efdi <bundle.cbor>` against the **live EFDI sandbox fabric**
(sandbox router directly; no partner-net, no release-bridge), then:

- `goat doctor` → mesh up, router handshake, fabric reachable.
- `goat-cli pub release/<partner>/test/...` → observed on the fabric.
- `goat-cli sub release/goat/...` → observed at the pod (when a counterpart publishes).
- Audit NDJSON accumulates on the LUKS volume.

TODO: capture this as `live-efdi.md` runbook once `first-boot.sh` is wired.
