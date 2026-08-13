# 12 — Zenoh Admin GUI

A web GUI for operating the pod without SSH: router and system status, starting and stopping bridges and layers, editing configuration and credentials, the certificate authority, and branding. It uses a modern-minimal dark theme (solid soft-dark cards, self-hosted Inter, a teal accent) that a superadmin can rebrand from WebUI Settings.

The Dashboard's "Connected routers" panel lists every other zenoh instance (router or peer) this router has a live link to — pulled from the router's own admin space, same source as the subscriber/queryable topic lists, so it needs no separate configuration beyond the existing `pod-admin-introspect` ACL rule.

## Panel walkthrough

The panel runs at `http://127.0.0.1:8890` (or the pod's address) and manages one
EFDI pod. If you are new to it, this is the orientation; the deeper subsections
(*Runtime Control page*, *Roles*, *Config tab fields*) follow below.

**First-time flow.** Log in with the admin account created during install (the
first login may prompt a password change). The Dashboard opens — confirm the pod
is healthy. From there the everyday loop is simple: **Runtime Control** to run
services, **Config** to configure them, **Dashboard** to confirm health.

**The pages** (sidebar order):

- **Dashboard** — health overview: CPU/RAM/disk/uptime/load/network, core-service
  status, a small federation preview, and live Zenoh stats (subscribers,
  queryables, storages, connected routers). Start here to answer "is everything
  up?".
- **Network** — *Managed Router Network*. Only relevant when this pod is an
  HQ/root managing branch routers: topology, direct children, trust status, and
  **Apply trust ACL**. A single standalone pod shows "0 direct children" and can
  ignore this page.
- **Config** — two layers on one page. **Zenoh Config** edits the router itself
  (local ports, fabric uplink endpoints and certificate identity, namespace,
  connection policy — see *Config tab fields* below). **Integration Settings**
  edits the services' environment without SSH: TAK host/port, SitaWare HQ NVG
  import/feed and REST paths, MQTT/SensorThings, and ASTERIX ports.
  Passwords are write-only. Save, then restart the affected service from Runtime
  Control for it to take effect.
- **Runtime Control** — start / stop / restart every bridge (sensor input),
  protocol (translator), and layer (C2 output); filter by category or role;
  choose the launch set (remembered across restarts); read logs inline. See
  *Runtime Control page* below.
- **Changes** — history of Zenoh-config revisions and their applied / rejected /
  rolled-back outcome (it records the outcome and a hash, not the config body).
- **Admin Users** — manage panel accounts and their roles (superadmin only).
- **Certificates** — *Certificate Authority*: create single-use child invitations
  for federation (the child generates its own keys and submits only signing
  requests) and track certificate expiry. Standalone pods do not need this.
- **Publish Script** — assemble a Zenoh publish command for testing or feeding
  data in, without hand-writing key expressions.
- **Shell** — a scoped, audited shell into the router container for diagnostics.
- **Logs** — live log tail for any host-managed service.
- **Audit Logs** — a record of privileged actions taken through the panel (config
  changes, branding, user changes, logins).
- **WebUI Settings** (top-right account menu) — **Branding** (organisation name,
  accent colour, and logo — superadmin), **Appearance** (row animations, dense
  rows), **Live behavior** (refresh interval), and the light/dark theme toggle.

**Two guards you may meet — both intentional.** EFDI's federation layer refuses
actions that could silently break trust boundaries:

1. *"Apply trust ACL" blocked — unmanaged fabric uplink.* The root still dials an
   outbound fabric peer that is not an enrolled child. Either enroll that peer, or
   clear the uplink under **Config → Fabric endpoints → "Root / no upstream"**.
2. *Deleting a managed router is disabled.* You decommission or quarantine it
   instead, so a removed router cannot re-appear as an untrusted peer.

## Setup

Add to `compose/.env` (see `compose/.env.example` for the full block):

```bash
ZENOH_ADMIN_DB_USER=zenoh_admin
ZENOH_ADMIN_DB_PASSWORD=<random>
ZENOH_ADMIN_DB_ROOT_PASSWORD=<different-random-value>
ZENOH_ADMIN_DB_PORT=3307                # non-default: avoids clashing with MariaDB/MySQL on 3306
ZENOH_ADMIN_SECRET_KEY=<openssl rand -hex 32>
ZENOH_ADMIN_FIRST_USER=admin
ZENOH_ADMIN_FIRST_PASS=<set once, then blank it out after first login>
```

`ZENOH_ADMIN_FIRST_PASS` only creates the first `superadmin` account if it doesn't already exist — it is safe to blank it out again after the first login (the account persists in MariaDB).

The admin service is MariaDB-only. Historical PostgreSQL migration tooling was
removed after the deployment cutover; upgrades must back up
`${POD_STATE_DIR}/zenoh-admin/mariadb` and `compose/.env` before rebuilding.

## Launching

```bash
cd compose
docker compose up -d zenoh-admin-db zenoh-admin zenoh-admin-proxy
```

Then open `https://<pod-host>:8890`.

The panel itself (`zenoh-admin`) binds `127.0.0.1:8895` only — not directly reachable. A Caddy reverse proxy (`zenoh-admin-proxy`) terminates real TLS on `:8890` using Caddy's own internal CA (`local_certs` + `tls internal`, no external ACME/CA dependency), persisted in the `zenoh_admin_caddy_data` volume so the CA survives restarts. Your browser will show a self-signed-certificate warning on first visit — trust Caddy's local CA (or accept the warning) to proceed; there is no public certificate here by design, since this panel isn't meant to be internet-facing.

## Runtime Control page

The TAK-style **Runtime Control** page gives a `superadmin` one place to:

- start, stop, restart, and inspect logs for every registered bridge, protocol translator, raw ingress, and TAK/SitaWare output layer;
- edit endpoints, ports, Zenoh topics, API URLs, and protocol settings;
- show and edit additional deployment-specific `.env` fields already present on the pod;
- enter usernames, passwords, API keys, and tokens without displaying existing secret values.

Native processes remain host PID-managed. `start.sh` and `run.sh all` keep
`admin-control` running on localhost port 18896. The API delegates to those same
launcher scripts rather than creating one container per integration. Set
`EFDI_CONTROL_TOKEN` in `compose/.env` for a bearer token between the admin API
and the local control process. Restart an affected service after saving a
setting so it reads the new environment.

For `./dev.sh up`, the disposable control agent automatically moves to port
18896 when the development/default 8896 is already occupied, and the dev API is
pointed at that selected port.

## Managed router hierarchy and delegated CA

Initialize the first managed router's bounded subordinate CA during an offline
ceremony. The parent/global CA private key is read for this command only and is
not copied into router state:

```bash
scripts/pki/init-router-ca.sh \
  <this-router-namespace> \
  /offline/efdi-global-root.pem \
  /offline/efdi-global-root-key.pem \
  "${POD_STATE_DIR}/pki"
```

Move the global root key back offline immediately. Create the router's non-CA
policy signer, then initialize the optional online leaf issuer beneath the
bounded router CA:

```bash
scripts/pki/init-policy-signer.sh \
  <namespace-prefix>/<this-router-namespace> \
  "${POD_STATE_DIR}/pki/router-ca-cert.pem" \
  "${POD_STATE_DIR}/pki/router-ca-key.pem" \
  "${POD_STATE_DIR}/pki"

scripts/pki/init-step-ca.sh \
  "${POD_STATE_DIR}/pki/router-ca-cert.pem" \
  "${POD_STATE_DIR}/pki/router-ca-key.pem" \
  "${POD_STATE_DIR}/pki/step-ca" \
  <vpn-dns-name-or-ip>
```

The policy key signs delegation and management envelopes but cannot issue
certificates. step-ca receives a generated online intermediate and never keeps
the bounded router-CA key. Set the host paths in `compose/.env`, then restart
`admin-control`:

```bash
EFDI_ROUTER_CA_CERT_PATH=/absolute/runtime/pki/router-ca-cert.pem
EFDI_ROUTER_CA_KEY_PATH=/absolute/runtime/pki/router-ca-key.pem
EFDI_ROUTER_CA_CHAIN_PATH=/absolute/runtime/pki/router-ca-chain.pem
EFDI_POLICY_SIGNER_CERT_PATH=/absolute/runtime/pki/policy-signer-cert.pem
EFDI_POLICY_SIGNER_KEY_PATH=/absolute/runtime/pki/policy-signer-key.pem
EFDI_STEP_CA_STATE_PATH=/absolute/runtime/pki/step-ca
./stop.sh admin-control
./start.sh --service admin-control
```

In **Certificate Authority**, create a single-use invitation for the child
namespace and choose how many further CA levels that child may delegate. The UI
derives the maximum from the issuer certificate; every child depth must be
strictly lower than its parent's X.509 path-length constraint.

On the child, generate and enroll all three identities locally:

```bash
scripts/pki/enroll-router.sh \
  https://<parent-management-host>:8890 \
  <child-namespace> \
  "${BUNDLE_DIR}/efdi" \
  "${POD_STATE_DIR}/pki"
```

The script prompts for the invitation token without placing it in argv and
sends only router-CA, transport, and policy-signer CSRs. The response contains
the complete signed delegation chain, public parent trust, and a one-time link
credential; private keys never leave the child. Configure the printed paths
and run the normal first-boot/rebuild flow. CA private keys remain behind the
localhost host-control boundary.

When the parent has step-ca initialized, enrollment receives a renewable
24-hour transport certificate. On the child, set `EFDI_STEP_CA_URL` to the
parent's VPN URL and optionally override `EFDI_STEP_RENEW_*_PATH`. `start.sh`
and `run.sh` then keep the PID-managed `cert-renewer` running. It checks every
15 minutes, renews within the eight-hour window configured by
`EFDI_STEP_RENEW_BEFORE_SECONDS`, updates the active router certificate,
and restarts the router and admin certificate consumers. Router-CA and policy
authority rotation remains an explicit re-enrollment operation rather than an
automatic online privilege escalation.

Each router can then manage its own direct children. **Zenoh Config** can target
any proven descendant, but the command is signed and forwarded one parent/child
hop at a time. The receiver validates the complete rendered file by starting
the pinned Zenoh binary with networking disabled, atomically activates it,
waits for health, and restores the last-known-good file on failure. **Changes**
shows the revision path and terminal status. Loss of the parent link stops new
upstream commands but does not stop the branch's existing data plane, local
WebUI, or management of its own subnet.

The remote editor always starts from that router's latest reported structured
snapshot. A parent push cannot change the child's identity, listener ports,
fabric CA profile, certificate-name verification policy, or organization
control prefix. Uplink replacement is deliberately two-stage: add the new
endpoint while retaining an existing one, verify the change, then remove the
old endpoint. The child restores its prior config if no remote router session
returns after restart.

Topology and status facts include the complete bounded public delegation proof.
A root verifies every CA signature, policy signer, namespace narrowing, depth,
lifetime, and revocation before displaying a descendant as verified. Generated
ACL activation is deliberately rejected when a root still has an unmanaged
fabric uplink; enroll or migrate that peer before applying managed ACLs.

Run the disposable runtime gate before deployment:

```bash
tests/smoke/loopback.sh
```

## Roles

| Role | Dashboard | Config (view) | Config (edit + restart router) | Admin Users |
| --- | --- | --- | --- | --- |
| `readonly` | ✓ | | | |
| `admin` | ✓ | ✓ | | |
| `superadmin` | ✓ | ✓ | ✓ | ✓ |

Saving a config edit first renders the structured fields and validates the full
candidate with the same pinned Zenoh binary used at runtime, with listeners,
connectors, scouting, and plugins disabled for the probe. Only an accepted
candidate is written atomically. The router is restarted and health-checked;
failure restores the last-known-good config and restarts the previous state.

## Config tab fields

The Config tab exposes structured fields, not raw JSON5 — each save re-renders `../examples/zenoh-router.json5.tmpl` (the same template `first-boot.sh` uses) with the values below, so a saved config can never drift from the template's structure.

| Field | Effect |
| --- | --- |
| Local mTLS port | Mesh-facing listen port for bridges, audit-sink (default 7447) |
| Local TCP port | Plaintext local-only listen port for bridges + this GUI (default 7448) |
| Fabric endpoint | The peer endpoint this pod dials out to — entered as separate Host + Port fields (scheme is always `tls`, never exposed); one-click presets are available for previously-used endpoints |
| Partner namespace | This pod's first-party publish/subscribe prefix (its slot) — **changing this requires the other side of the fabric to also allow the new value in its ACL, or publishes silently stop reaching it** |
| Inbound namespace | Bilateral prefix the fabric publishes TO this pod |
| Verify name on connect | Off by default — the gateway cert SAN binds the mesh IP, not the DNS name dialed; turning this on can break the fabric connection |
| Storage plugin loading | Off means new subscribers no longer get a last-known value via `get()` — publish/subscribe still works |

Three deliberately **not** exposed in the GUI (too easy to lock out every client, including the GUI itself, if misconfigured): `access_control.enabled`, `default_permission`, `enable_mtls`. Edit those directly in `zenoh/config.json5` if ever needed.

### Endpoint helper usage

The Config page's `Fabric endpoints` section is the helper shown in the screenshot:

- enter a host and port,
- click `Add direct link` to append another `connect.endpoints` entry,
- pick `Root / no upstream` to clear the list, or one of the presets to seed a known endpoint,
- save the config to render the `connect.endpoints` array back into `config.json5`.

The publish-builder has the same shortcut at the raw-config level: `Add to connect.endpoints` inserts a candidate endpoint into the current router config text.

### Three-router mesh example

For the `zenoh1` / `zenoh2` / `zenoh3` cluster, the consistent pattern is:

```json5
zenoh1: {
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447"] },
  connect: { endpoints: ["tls/zenoh2.efdi.ltu:7447", "tls/zenoh3.efdi.ltu:7447"] },
  transport: { link: { tls: { root_ca_certificate: "/root/.zenoh/certs/efdi_ca.crt", listen_certificate: "/root/.zenoh/certs/zenoh1.pem", listen_private_key: "/root/.zenoh/certs/zenoh1.key", connect_certificate: "/root/.zenoh/certs/zenoh1.pem", connect_private_key: "/root/.zenoh/certs/zenoh1.key", enable_mtls: true, verify_name_on_connect: true } } },
  plugins: { rest: { http_port: 8000 } },
  plugins_loading: { enabled: true },
  access_control: { enabled: true, default_permission: "allow", rules: [], subjects: [], policies: [] }
}
```

```json5
zenoh2: {
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447"] },
  connect: { endpoints: ["tls/zenoh1.efdi.ltu:7447", "tls/zenoh3.efdi.ltu:7447"] },
  transport: { link: { tls: { root_ca_certificate: "/root/.zenoh/certs/efdi_ca.crt", listen_certificate: "/root/.zenoh/certs/zenoh2.pem", listen_private_key: "/root/.zenoh/certs/zenoh2.key", connect_certificate: "/root/.zenoh/certs/zenoh2.pem", connect_private_key: "/root/.zenoh/certs/zenoh2.key", enable_mtls: true, verify_name_on_connect: true } } },
  plugins: { rest: { http_port: 8000 } },
  plugins_loading: { enabled: true },
  access_control: { enabled: true, default_permission: "allow", rules: [], subjects: [], policies: [] }
}
```

```json5
zenoh3: {
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447"] },
  connect: { endpoints: ["tls/zenoh1.efdi.ltu:7447", "tls/zenoh2.efdi.ltu:7447"] },
  transport: { link: { tls: { root_ca_certificate: "/root/.zenoh/certs/efdi_ca.crt", listen_certificate: "/root/.zenoh/certs/zenoh3.pem", listen_private_key: "/root/.zenoh/certs/zenoh3.key", connect_certificate: "/root/.zenoh/certs/zenoh3.pem", connect_private_key: "/root/.zenoh/certs/zenoh3.key", enable_mtls: true, verify_name_on_connect: true } } },
  plugins: { rest: { http_port: 8000 } },
  plugins_loading: { enabled: true },
  access_control: { enabled: true, default_permission: "allow", rules: [], subjects: [], policies: [] }
}
```

If you want a fourth router to join that cluster, add its DNS name or IP to all three `connect.endpoints` lists and make sure the certificate SAN matches the name you dial.

## Isolated test router

For local pub/sub testing without touching the real pod or its fabric connection: `zenoh-router-test`, behind the `test` compose profile (never starts with the rest of the stack).

```bash
cd compose
docker compose --profile test up -d zenoh-router-test
```

Config lives at `${POD_STATE_DIR}/zenoh-test/config.json5` — same certs/namespace/ACL as the real router, but different ports (`7457` mTLS / `7458` TCP, vs. `7447`/`7448`) and **no `connect.endpoints`** (never dials the fabric). Safe to leave running alongside the real router; nothing conflicts.
