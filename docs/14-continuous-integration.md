# 14 — Continuous Integration

## Continuous Integration

Five workflows in `.github/workflows/` run on every push/PR to `main`:

| Workflow | Checks |
| --- | --- |
| `shellcheck.yml` | Lints every `.sh` script in the repo (`-S warning`) |
| `compose-validate.yml` | Confirms `compose/docker-compose.yml` parses as valid YAML |
| `bridge-syntax.yml` | `py_compile` on every file in `compose/bridges/`, `compose/protocols/`, and `compose/layers/` |
| `zenoh-admin-frontend.yml` | `pnpm type-check` + `pnpm build` for `compose/zenoh-admin/ui` |
| `docker-build.yml` | Builds the flattened `compose/Dockerfile` and the `compose/zenoh-admin` image (no push) |

This catches syntax errors, TypeScript errors, and Dockerfile breakage before merge — it does **not** run the bridges themselves (most need real API keys/network access CI doesn't have).

---

## Changelog

| Date | Change |
| --- | --- |
| 2026-06-14 | Initial commit — forked from official `efdi-moon-pod-main` repository |
| 2026-06-15 | Base bridge adapters wired; repository structure established; README added |
| 2026-06-16 | Protocol Buffer definitions for track types; contracts now live beside translators in `compose/protocols/` |
| 2026-06-17/18 | Quality-of-life improvements: bridge robustness, layer deduplication, track fusion tuning |
| 2026-06-18 | ASTERIX full-decode design specification document added |
| 2026-06-19/22 | Further bridge and layer improvements; Giraffe ASTERIX bridge complete |
| 2026-06-22 | `dronuradaras.lt` bridge: acoustic sensor network + drone detection events |
| 2026-06-22 | CoT DETECTION section with audio clip URL in ATAK remarks field |
| 2026-06-22 | Radar site marker: startup publish + 60 s keepalive so ATAK never loses the marker |
| 2026-06-23 | Security audit: removed hardcoded API token from `register_topics.sh`; token moved to `$EFDI_PORTAL_KEY` env var |
| 2026-06-23 | Security: personal namespace UUID, email, IP, and vendor slug removed from all tracked files; bridges read `PARTNER_NAMESPACE` from environment |
| 2026-06-23 | Security: `compose/.env` and `register_topics.sh` added to `.gitignore` — credentials stay local only |
| 2026-06-23 | Security: unbounded HTTP body read in `rest-http/bridge.py` capped at 10 MB |
| 2026-06-23 | Documentation overhaul: `INSTALL.md` (English), `DIEGIMAS.md` (Lithuanian), `README.md` rewritten as architecture overview |
| 2026-06-23 | ASTERIX CAT-34 I034/120 decoder: radar self-reports WGS-84 position from live stream — no manual coordinate config required |
| 2026-06-23 | Mobile radar support: position, speed, and course derived from successive I034/120 reports; ATAK shows motion trail on vehicle-mounted radars |
| 2026-07-05 | Zenoh admin GUI: FastAPI + React panel for router status and `config.json5` editing, styled after the TAK admin panel |
| 2026-07-05 | Fixed `zenoh-router.json5.tmpl` drift: template was missing the plaintext `tcp/0.0.0.0:7448` local listen endpoint that the live config already had |
| 2026-07-05 | Zenoh admin GUI config tab: added `verify_name_on_connect` and storage-plugin-loading toggles; fabric endpoint now entered as separate Host/Port fields with one-click presets instead of a raw `tls/host:port` string |
| 2026-07-05 | Zenoh admin GUI: added `/api/health` (CPU/RAM/disk/uptime/load/network/cert-expiry, TAK-admin-panel style) to the dashboard |
| 2026-07-05 | Fixed SPA routing bug: direct navigation/refresh/back-button to any GUI sub-route (`/config`, `/admin-users`) 404'd as raw JSON instead of loading the app — the fallback code caught `fastapi.HTTPException`, but `StaticFiles.get_response` raises `starlette.exceptions.HTTPException` (a different, parent class), so the catch never matched |
| 2026-07-05 | Added isolated `zenoh-router-test` service (`test` compose profile) for local pub/sub testing without touching the real pod or its fabric connection |
| 2026-07-05 | Removed the `gps-ew` bridge (GPSJam-based) — gpsjam.org has no public API for its own processed data, so this bridge never actually worked; removed from `start.sh` and `tak_layer.py` rather than left silently broken |
| 2026-07-05 | Fixed cross-source/cross-pod duplicate tracks in SitaWare: `nato_sitaware_layer.py`'s `_uid()` baked the source name into the track ID (unlike `tak_layer.py`'s already-correct version), so the same aircraft from two sources got two different SitaWare tracks |
| 2026-07-05 | `dronuradaras_bridge.py` was changed to publish every positioned registered sensor; superseded by the 2026-07-15 online-only operator policy below |
| 2026-07-05 | Added `.github/workflows/ci.yml`: compile-checks bridges/layers, type-checks + builds the zenoh-admin frontend, builds both Docker images on every push/PR |
| 2026-07-05 | Added `shellcheck` and `compose-validate` CI jobs; fixed the one real finding (`compose/rebuild.sh` missing `cd ... \|\| exit`) and silenced a false-positive (`SC2163` on the intentional "export by dynamic name" idiom in `start.sh`/`stop.sh`/`run.sh`) |
| 2026-07-10 | Fixed `nato_sitaware_layer.py` reusing the inbound `sitaware_bridge.py`'s env var names (`SITAWARE_URL`/`USER`/`PASS`) — renamed to `SITAWARE_NVG_*` since HQ (inbound) and Edge (outbound) are usually separate hosts/credentials |
| 2026-07-10 | Wired `nffi` into `start.sh` — it existed in the repo but was never registered as a launchable service |
| 2026-07-10 | Zenoh admin GUI: added a "Connected routers" panel — parses `router/transport/unicast/*` entries already present in the admin-space query used for the subscriber/queryable lists, no new ACL or query needed |
| 2026-07-10 | Zenoh admin GUI: ported the TAK-hud visual language (`hud-card`, `hud-frame`/reticle corners, `hud-glass` sidebar, `hud-grid-bg` backdrop, accent-glow buttons, staggered fade-in) into `index.css`/`Layout.tsx`/dashboard |
| 2026-07-11 | Zenoh admin GUI: full TAK port (not just style) — runtime branding via DB-backed store, theme toggle, notifications bell, username-change, all routes retrofitted with light/dark variants |
| 2026-07-11 | Zenoh admin panel HTTPS: uvicorn now binds `127.0.0.1:8895` only; new `zenoh-admin-proxy` (Caddy) terminates real TLS on `:8890` via Caddy's internal CA, `on_demand` issuance (operators reach it by raw IP, no SNI) |
| 2026-07-11 | `BUNDLE_DIR`/`POD_STATE_DIR` defaults moved from `$HOME/goat-bundle`/`$HOME/goat-moon` to `compose/certs/`/`compose/state/` (in-repo, gitignored) — scattered state across `$HOME` made cleanup unreliable |
| 2026-07-11 | Added `dev.sh`: disposable local MariaDB + directly-run uvicorn for zenoh-admin UI preview only, bypassing zenoh-router/certs/fabric entirely |
| 2026-07-11 | Removed the external "goat" vendor entirely: certs are now self-issued via `scripts/gen-certs.sh` (EFDI root CA, no portal/CBOR bundle), containers renamed `goat-moon-*` → `efdi-pod-*`, `GOAT_CERT_DIR` env var renamed `EFDI_CERT_DIR`, `../examples/first-boot.sh` rewritten to read `compose/.env` directly and drop the `goat-clientd` wrapper (NetBird is called natively — it was always EFDI's own asset, not vendor lock-in), `profiles/` directory removed (orphaned by the rewrite) |
| 2026-07-15 | `dronuradaras_bridge.py` now publishes only devices explicitly reported as `is_online=true`; offline devices emit deletion events so CoT, SitaWare Edge, and the HQ NVG snapshot evict cached markers |
| 2026-07-17 | Added deterministic ASTERIX category listener conventions: CAT-010/020/021/034/048/062 use UDP 50010/50020/50021/50034/50048/50062 by default; these are EFDI conventions, not vendor defaults |
| 2026-07-17 | Added Zenoh-native CAP, GeoJSON/OGC, spectrum, sensor-health, mission-route, and raw-ingress translation paths |
| 2026-07-17 | Security refresh: Vite upgraded, Compose images pinned/refreshed, Python image OS packages upgraded, and authenticated SitaWare/UTM endpoints restricted to HTTPS |
| 2026-07-18 | Added TAK-style Runtime Control for native bridge/protocol/layer lifecycle, bounded logs, endpoint/topic/port editing, write-only credentials, a localhost admin-control agent, and a live Vite dev stack with aligned API/proxy ports |
| 2026-08-02 | Merged `HOST_SETUP.md`, `INTEGRATIONS.md`, `C2_RUNBOOK.md`, `ADDING_A_SENSOR.md`, `TROUBLESHOOTING.md`, and `GOTCHAS.md` into this document ([Bootstrap and Install](03-bootstrap-and-install.md) §1; [Integrations](08-integrations.md), [C2 ↔ Zenoh Runbook](09-c2-zenoh-runbook.md), [Adding a Sensor](10-adding-a-sensor.md) §§7-9; [Troubleshooting](11-troubleshooting.md) §11) — one deployment guide instead of eight; [ZENOH_ADMIN.md](12-zenoh-admin-gui.md) stays separate |
| 2026-08-02 | Added BDS 1,0/1,7 (Data Link Capability / Common Usage GICB Capability) decoding to the 7 ASTERIX categories that already reuse BDS 3,0/4,0/5,0/6,0 GICB-extraction helpers (CAT-010/011/018/020/021/048/062), sourced from pyModeS |
| 2026-08-02 | Renamed `layers/cot_layer.py` → `layers/tak_layer.py` and `layers/nvg_layer.py` → `layers/sitaware_layer.py` (vendor-named egress, matching `tak_bridge.py`/`sitaware_bridge.py`'s ingress naming); removed the unused `cot-udp`/`cot-udp-tak` UDP multicast/unicast launcher entries and the `nvg_bridge.py` NVG-XML ingress bridge (SitaWare ingress is REST-only now) |
| 2026-08-02 | Consolidated every EFDI-authored `.proto` schema under `compose/protocols/proto/` (was split across `compose/protocols/random/`, `compose/protocols/vendors/proto/`, and `compose/protocols/vendors/sparkplug/`); vendored third-party schemas (SAPIENT `sapient_msg/`, Sparkplug B) stay under their own `vendors/<name>/` directory |
| 2026-08-28 | `zenoh-admin`'s backing store switched from MariaDB to PostgreSQL 18 (its own container, port `ZENOH_ADMIN_DB_PORT` default `5433`); new `EFDI_DB_DATA_DIR` variable pins the datadir to local disk, kept deliberately outside `POD_STATE_DIR` now that the latter can live on a JuiceFS mount. `scripts/migrate_mariadb_to_postgres.py` carries existing accounts/audit log/PKI data across for deployments installed before this date. |

---

*Internal use only — do not distribute outside the project.*
