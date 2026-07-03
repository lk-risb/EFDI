# Vendor Cleanup + Zenoh Admin GUI — Design Spec
**Date:** 2026-07-02

## Overview

Two related changes to move this pod toward a minimal, self-owned base ("simple and stupid, working"):

1. **Vendor cleanup** — remove EFDI/goat vendor tooling that isn't load-bearing for pod operation (portal registration, proprietary audit sink, health-probe timer, partnership/contract docs, a stray tracked bundle artifact).
2. **Zenoh admin GUI** — a new web-based admin panel for the pod's Zenoh router, replacing manual `config.json5` editing with a browser UI. Mirrors the design of the sibling TAK Server admin panel (`/home/ndukve/IdeaProjects/TAK/docs/specs/2026-06-25-admin-panel-design.md`) for stack and pattern consistency across the two admin tools.

### Hard requirement: no regression to live data delivery

Both parts of this change must not break the pod's existing job — sensor data reaching C2 (TAK Server / WinTAK / other connected clients) must keep working throughout and after implementation. Concretely:

- **Part 1 (vendor cleanup)** touches nothing in the data path. Every item on the delete list is observability, registration, or documentation — `audit-sink` only *subscribes* for metrics (removing it doesn't affect delivery to anyone else), `register_topics.sh` registers topic schemas with EFDI's portal but nothing in any bridge checks or depends on that registration to publish. The bridges, `cot_layer.py`, `zenoh-router`, and the mesh/cert bootstrap are all untouched.
- **Part 2 (Zenoh admin GUI)** is purely additive — a new service alongside the existing stack, not a replacement of any existing one. The one point of live impact: applying a config change via `/config` triggers a `zenoh-router` restart, which causes a brief reconnect blip for every currently-connected session (bridges, `cot-tcp`/`cot-udp-tak` senders, other pods/users on the fabric) — existing reconnect logic in each bridge and in `cot_layer.py`'s `TcpSender`/`UdpSender` already handles this automatically. This only happens when an admin explicitly clicks apply — never from merely running the GUI, viewing the dashboard, or leaving it open.

Acceptance test for this requirement is in the Testing section below.

### Explicitly out of scope for this pass

- **NetBird / mesh bootstrap redesign** — the existing `host/first-boot.sh` + `goat-clientd` bootstrap is the pod's only working path to mesh membership and mTLS certs. It stays as-is; nothing replaces it yet. A future pass may redesign this (dropping the `goat-cli`/bundle-based enrollment for a standalone CA), but that is not part of this spec.
- **DNS** — was going to fold into the mesh redesign above; paused with it.
- **Bridge/track-data PostgreSQL** — a separate, undecided question (what would it store — track history? audit log?) from the Postgres this spec *does* introduce (see below). Not addressed here.
- **Hardware-key (CAC/PIV) admin auth** — documented as a future spec, not built this pass. When it is built: mandatory and the *only* login method (no password fallback), cert auto-issued at admin-account creation (same pattern as TAK Server's `generate_user.sh`), silent automatic login when a card is detected (no button, no prompt), superadmin revokes via the admin panel, and the shell-elevate re-auth flow (if this GUI ever grows a shell tab) also accepts the card. Uses its own CA, separate from whatever CA the pod's Zenoh mTLS ends up using — no cross-signing across trust boundaries.

---

## Part 1 — Vendor Cleanup

### Delete

| Item | Why safe to delete |
|---|---|
| `register_topics.sh` | Calls EFDI's proprietary portal API (`portal.efdi.netbird.efdi-backbone.net`) to register topic schemas. Not required for the pod to publish/subscribe — purely a registration nicety. |
| `audit-sink` service block in `compose/docker-compose.yml` | Proprietary `ghcr.io/desertgoat/goat-audit-subscriber` image, observe-only in v0 (no durable persistence configured). Not load-bearing for data flow. |
| `host/goat-doctor.sh`, `host/goat-doctor.service.example`, `host/goat-doctor.timer.example` | Host health-probe timer. Monitoring only, not required for pod function. |
| `1851281db70ccc0409dad4ecfc874cf5-goat-cli.cbor` (repo root) | Tracked bundle artifact — looks like an accidental commit of cert/bundle material. Should never have been tracked. |
| `docs/partner-contract.md`, `docs/efdi-vs-production.md`, `docs/goat-client-v02-contract.md`, `docs/workload-identity-spire-overlay.md`, `docs/quality-of-service.md`, `docs/proof-out-tracks.md`, `docs/partner-run-guide.md` | EFDI partnership/contract-process documentation. Docs only, no code dependency. |

### Keep (still functional, no replacement built yet)

- `host/first-boot.sh`, `host/goat-clientd.service.example`, `host/zenoh-router.json5.tmpl`
- `profiles/efdi/profile.env`, `profiles/production/profile.env.TODO`
- `clients/`, `examples/`, `tools/asterix_relay.py` — generic third-party integration docs / standalone utility, not vendor lock-in

### Follow-up consistency pass

After deletion, `CLAUDE.md`'s security-constraints section references `EFDI_PORTAL_KEY` and `EFDI_VENDOR_SLUG` (used only by `register_topics.sh`) — remove those two entries once the script is gone. `docker-compose.yml`'s header comment describes the `audit-sink` service; update to remove the stale reference.

---

## Part 2 — Zenoh Admin GUI

### Architecture

```
zenoh-admin (new compose service, :8890)
├── FastAPI (Python)                    REST API + static file serving
├── embedded eclipse-zenoh client       read-only status queries (@/** admin space)
├── PostgreSQL (own instance)           admin_users, refresh_tokens, audit_log
└── React + Vite + TypeScript + Tailwind
    + shadcn/ui + TanStack Router       same frontend stack as the TAK admin panel
```

This introduces a Postgres instance, but scoped narrowly to the GUI's own login/session/audit data — a separate concern from the (paused, undecided) bridge/track-data Postgres question. Same engine, different database, different purpose.

### How config changes are applied (Approach A — file-edit + restart)

Chosen over two alternatives (live Zenoh hot-reload; a separate status-snapshot sidecar) because it matches how this repo already works and invents nothing new:

- **Reads** (dashboard status): FastAPI holds one embedded `eclipse-zenoh` Python session (same library already used by every bridge in this repo) purely for read-only `@/**` admin-space introspection — connected sessions, active subscriptions/publishers, storage stats, endpoint reachability.
- **Writes** (config edits): validated against a schema, written directly to `zenoh/config.json5` (the same file that is the source of truth today), then `docker compose restart zenoh-router` is triggered over a mounted `/var/run/docker.sock` — the same container-op pattern the TAK admin panel already uses for its own service restarts.
- **Trade-off:** every config change causes a brief `zenoh-router` restart (a reconnect blip for all currently-connected bridges/subscribers). Acceptable — config edits are infrequent admin actions, not a runtime hot path.

### Routes

| Route | Roles | Description |
|---|---|---|
| `/login` | public | Password login |
| `/` | all | **Dashboard** — connected sessions, active subscriptions/publishers, storage stats, listen/connect endpoint health |
| `/config` | admin+ | View/edit `zenoh/config.json5` — listen endpoints, connect endpoint, TLS cert paths, storage key_exprs, ACL rules/subjects/policies |
| `/admin-users` | superadmin | Manage GUI admin accounts |

### Backend API

```
POST /auth/login          {username, password} → {access_token} + refresh cookie
POST /auth/refresh        (refresh cookie) → {access_token}
POST /auth/logout         revokes refresh token

GET  /api/status           live Zenoh admin-space snapshot (sessions, subs, storage, endpoints)
                           frontend polls this on an interval — no WebSocket stream (simpler than
                           the TAK panel's live health WS; revisit only if polling proves too slow)

GET  /api/config           current config.json5, parsed
PUT  /api/config           validated write → file + zenoh-router restart

GET    /api/admin-users
POST   /api/admin-users
PATCH  /api/admin-users/{id}
DELETE /api/admin-users/{id}
```

### Security

- Passwords: bcrypt-hashed, minimum 12 characters
- Sessions: JWT access token (15 min) + httpOnly refresh cookie (7 days)
- Docker exec scoped only to restarting the `zenoh-router` container — no arbitrary container exec
- Config writes validated against a schema before being written, to avoid bricking the router with malformed JSON5
- Audit log: every write action recorded (user, action, timestamp, detail)
- Own CA/cert boundary if/when cert-based auth is added later (#7) — separate from any other trust domain in this pod, no cross-signing
- Network binding: `127.0.0.1` by default — expose only via NetBird or a reverse proxy

### docker-compose addition

```yaml
zenoh-admin:
  build: ./zenoh-admin
  environment:
    ADMIN_DB_URL: postgresql://${ZENOH_ADMIN_DB_USER}:${ZENOH_ADMIN_DB_PASSWORD}@zenoh-admin-db:5432/admin
    ADMIN_SECRET_KEY: ${ZENOH_ADMIN_SECRET_KEY}
  volumes:
    - /var/run/docker.sock:/var/run/docker.sock
    - ${POD_STATE_DIR}/zenoh:/zenoh-config:rw
  ports:
    - "127.0.0.1:8890:8890"
  depends_on:
    zenoh-admin-db:
      condition: service_healthy
    zenoh-router:
      condition: service_healthy
  restart: unless-stopped

zenoh-admin-db:
  image: postgres:16-alpine
  volumes:
    - zenoh_admin_pg:/var/lib/postgresql/data
  environment:
    POSTGRES_DB: admin
    POSTGRES_USER: ${ZENOH_ADMIN_DB_USER}
    POSTGRES_PASSWORD: ${ZENOH_ADMIN_DB_PASSWORD}
  restart: unless-stopped
```

New env vars for `compose/.env`:
```
ZENOH_ADMIN_SECRET_KEY=        # JWT signing secret (generated by install.sh)
ZENOH_ADMIN_DB_USER=
ZENOH_ADMIN_DB_PASSWORD=
ZENOH_ADMIN_FIRST_USER=admin   # username created on first boot
ZENOH_ADMIN_FIRST_PASS=        # generated by install.sh, printed once
```

### Directory structure

```
zenoh-admin/
├── Dockerfile
├── api/
│   ├── main.py
│   ├── auth.py
│   ├── status.py
│   ├── config.py
│   ├── admin_users.py
│   ├── db.py
│   └── models.py
└── ui/
    ├── package.json
    ├── vite.config.ts
    └── src/
        ├── routes/
        │   ├── login.tsx
        │   ├── index.tsx          (dashboard)
        │   ├── config.tsx
        │   └── admin-users.tsx
        └── components/
```

### First-boot behaviour

On first start, if the `admin` database has no users, the container:
1. Creates the `admin` database and runs migrations
2. Creates the first `superadmin` account from `ZENOH_ADMIN_FIRST_USER` / `ZENOH_ADMIN_FIRST_PASS`
3. Prints credentials to container logs (one time only)

---

## Testing

- `docker compose up zenoh-admin zenoh-admin-db` starts cleanly against an already-running `zenoh-router`
- First-boot creates the superadmin account, credentials print once
- Dashboard (`/`) shows live connected sessions matching what's actually attached to `zenoh-router` (verify against a running bridge)
- Editing a config field via `/config`, applying it, and confirming `zenoh-router` restarts and the new config takes effect (check `zenoh/config.json5` on disk matches, check bridges reconnect)
- Malformed config write is rejected by schema validation before touching the file on disk

**Non-regression (hard requirement, must pass before this is considered done):**
- Before touching anything: confirm CoT is currently flowing end to end (bridge → `cot-tcp`/`cot-udp` → TAK Server → WinTAK/other connected clients showing live tracks) — baseline.
- After Part 1 deletions: same end-to-end check, zero change expected.
- After Part 2 deployment (GUI running, untouched): same end-to-end check, zero change expected — the GUI must not interfere with delivery just by existing.
- After an actual config apply via the GUI: brief reconnect blip is expected and acceptable, but delivery must resume automatically within the existing reconnect windows (bridges: `RECONNECT_S` in `cot_layer.py`, ~5s; Zenoh client reconnect in each bridge) — no manual restart of any bridge should be required to recover.
- Other users/pods on the fabric (anything subscribing to `LTU/CISB/<namespace>/**` besides this pod's own `cot_layer.py`) must keep receiving data across all of the above, since the topic prefix and ACL are unchanged by this work.
