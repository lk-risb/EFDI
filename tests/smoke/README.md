# moon-pod — bidirectional smoke test

Proves the v0 core: data moves **both directions** securely through the pod.

## Two layers

### 1. Self-contained loopback (CI — no fabric needed)

Stand up the pod's `zenoh-router` plus a **stub remote Zenoh** in one compose, both mTLS, and
assert:

- **Outbound:** publish on `${PARTNER_NAMESPACE}/test/ping` at the pod side → received at the
  stub-remote side.
- **Inbound:** publish on `${INBOUND_NAMESPACE}/test/pong` at the stub-remote side → received via
  a Zenoh subscriber (e.g. `clients/examples/modern/python/subscribe.py`) at the pod side.
- **ACL negative:** publish outside `${PARTNER_NAMESPACE}/**` → **denied** (default-deny holds).
- **Audit:** both deliveries appear as append-only NDJSON lines in `${POD_STATE_DIR}/audit/`.

This is the gate that runs in CI. TODO: implement `loopback.sh` + a stub-remote compose overlay.

### 2. Live EFDI-sandbox validation (slow step, on the validation host)

Run the real `first-boot.sh` (as root, after populating `compose/.env`) against the **live EFDI
sandbox fabric** (sandbox router directly; no partner-net, no release-bridge), then:

- `netbird status` → mesh up; the pub/sub round trip below confirms router handshake + fabric
  reachability.
- A Zenoh publisher (e.g. `clients/examples/modern/python/publish.py`) on
  `release/<partner>/test/...` → observed on the fabric.
- A Zenoh subscriber on `${INBOUND_NAMESPACE}/...` → observed at the pod (when a counterpart
  publishes).
- Audit NDJSON accumulates on the LUKS volume.

TODO: capture this as `live-efdi.md` runbook once `first-boot.sh` is wired.

## Identity-bound ACL compatibility gate

Before enabling generated delegation policies, run:

```bash
tests/security/zenoh_identity_acl.sh
```

This self-contained security test uses disposable certificates and passwords
under `mktemp`, the exact `eclipse/zenoh:1.9.0` router image used by Compose,
and the pinned `eclipse-zenoh==1.9.0` Python binding. It proves that certificate
CN and username subject fields are enforced together, scope escapes and partial
identities are denied, an explicit quarantine deny overrides an allow, and an
active mTLS link is closed when its short-lived certificate expires. It never
reads deployment certificates, `compose/.env`, API keys, or runtime state.

The image must already be present locally. If the bridge virtual environment is
not available, set `EFDI_ZENOH_PYTHON` to a Python executable with
`eclipse-zenoh==1.9.0` installed.
