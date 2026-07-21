# moon-pod — bidirectional smoke test

Proves the v0 core: data moves **both directions** securely through the pod.

## Two layers

### 1. Self-contained loopback (CI — no fabric needed)

`loopback.sh` starts a disposable pinned Zenoh 1.9 router and authenticated
clients, then asserts:

- **Outbound:** publish on `${PARTNER_NAMESPACE}/test/ping` at the pod side → received at the
  stub-remote side.
- **Inbound:** publish on `${INBOUND_NAMESPACE}/test/pong` at the stub-remote side → received via
  a Zenoh subscriber (e.g. `clients/examples/modern/python/subscribe.py`) at the pod side.
- **ACL negative:** publish outside `${PARTNER_NAMESPACE}/**` → **denied** (default-deny holds).
- certificate CN and username are enforced together;
- quarantine deny overrides allow; and
- an active link closes when its short-lived certificate expires.

This is the gate that runs in CI and uses only disposable keys below `mktemp`.

`managed-three-router.sh` is the second CI gate. It starts disposable root,
child, and grandchild Zenoh 1.9 routers with identity-bound direct-link ACLs and
asserts that scoped grandchild data reaches root, namespace escape is denied,
the branch continues while root is offline, and the chain recovers when root
returns.

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

Follow `live-efdi.md` for the deployment-only validation that CI cannot perform.

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
