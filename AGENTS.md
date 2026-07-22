# EFDI Agent Memory

Read `CLAUDE.md` first; its project constraints apply to every agent and tool.

## Non-negotiable workflow

- Never run `git add`, `git commit`, or `git push`. Leave index management,
  commits, and GitHub pushes entirely to the user.
- Preserve unrelated staged/worktree changes; do not alter the existing index.
- Certificates, API keys, `compose/.env`, runtime state, and real deployment identifiers never enter tracked files.
- After Python bridge/layer edits, compile-check all bridge and layer modules. Also run the frontend type-check/build, shell syntax/ShellCheck, Compose YAML validation, and `git diff --check` when relevant.

## Runtime architecture

- `zenoh-router`, MariaDB, socket proxy, `zenoh-admin`, and its Caddy proxy are Docker infrastructure.
- Sensor bridges and output layers are ordinary Python processes launched by `start.sh` or `run.sh`. Their logs and PID files live under `${POD_STATE_DIR}/logs` and `${POD_STATE_DIR}/.pids`. Do not reintroduce one Docker container per bridge/layer.
- `stop.sh` stops those PID-managed processes. `compose/rebuild.sh` rebuilds infrastructure only.
- The admin backend is FastAPI + async SQLAlchemy/MariaDB; the UI is React/Vite/TanStack Router/Tailwind.
- Data flows external sensor/protocol → Python bridge → local Zenoh router → output layer (CoT/TAK/SitaWare/etc.). Namespace shape is `{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/...`.

## Authoritative handoff material

- `.superpowers/sdd/progress.md` is Claude Code's execution ledger.
- `.superpowers/sdd/*-brief.md` and `*-report.md` record exact task contracts, reviews, fixes, and deferred live checks.
- `docs/superpowers/plans/` and `docs/superpowers/specs/` are ignored working documents but remain the design source for ongoing work.
- Root/deployment/client/example Markdown documents describe the shipped operational contract. Some old `docs/architecture.md` language is historical and should not override current README/launcher behavior.

## Current work state (2026-07-14)

- Completed before this handoff: publish-script builder, direct signed federation push/status, ACL and replay protection, configurable namespace prefix, TAK-style UI pass, and topology ACL.
- Completed in the current continuation: restored PID-managed Python bridge runtime; removed live per-bridge containers and their Compose/admin-control wrappers; completed topology aggregation and shared UI map integration for Dashboard, Topology, and Federation.
- Bridge/protocol audit completed: launchers now provide the shared Python module path and `run.sh all` covers every retained integration; HTTP/WebSocket inputs are bounded. SitaWare adapters remain interface-specific: optional deployment-documented HQ REST inbound, Edge NVG REST outbound, and the native HQ NVG pull feed used by an HQ NVG Import Subscription. `/rest/v2/units` is not a universal HQ resource. CoT parsing/routing is hardened; CAT-48/34 were checked against current EUROCONTROL specifications. CAT-20/21/62 are explicitly documented legacy UAPs, and Link-16 TCP is disabled until a gateway framing ICD exists.
- Windy, yr.no, PurpleAir, OSM/Overpass, and N2YO bridges and their dedicated schemas/downstream wiring were removed at the user's request.
- Static checks pass. Live browser and real multi-pod topology/config-push checks still require rebuilding/deploying the admin containers.
- Zero-trust cascading relay remains the next planned feature, but its plan requires successful live topology verification first.
- `tests/smoke/README.md` still records a genuine missing CI loopback test and live validation runbook.
- SitaWare HQ aircraft points now carry distinct barometric/geometric altitude,
  metres/feet/flight level, vertical rates, selected altitude, airspeeds,
  identity/status, ADS-B quality, and bounded source metadata through standard
  NVG modifiers and ExtendedData. The live authenticated feed has been verified.
- Protobuf is the intended internal data-plane direction, but `/v1` remains JSON.
  The three aircraft proto3 contracts now match their Python records with
  explicit optional presence. Migrate safely by generating Python bindings and
  dual-publishing a new `/v2` Protobuf topic before changing consumers; do not
  silently replace the bytes carried by `/v1`.
- SitaWare NVG Attributes reuse CoT's domain-aware TAK stat-card formatter.
  Weather is `a-n-G-I-R` in CoT and `SNGPESE---*****` in NVG; dronuradaras.lt
  sensors are `a-*-G-E-S` in CoT and `SNGPES----*****` in NVG. Keep these
  categories visually and semantically distinct.
- Publish dronuradaras.lt devices only when the latest successful public API
  poll reports `is_online=true`. Preserve the offline tombstone path: it evicts
  old markers from CoT, SitaWare Edge, and the SitaWare HQ NVG snapshot.
- CoT and SitaWare NVG share the same RU/BY ICAO/MMSI affiliation classifiers;
  do not regress ADS-B/AIS topics to one static affiliation. Full vessel traffic
  requires the native `aisstream` bridge and a runtime-only `AISSTREAM_KEY`.
  `start.sh` prompts for that key without saving it or placing it in argv.
- ADS-B emitter categories `C1`/`C2` are airport surface emergency/service
  vehicles. Keep them as `a-n-G-E-V` in CoT and `SNGPEV----*****` in NVG;
  never use `on_ground` alone because it also describes taxiing aircraft.
- `start.sh` exposes all 28 retained native/infrastructure services, merges its
  saved selection with currently running PID-managed processes, and auto-starts
  that restored set after a five-second change window. It remembers only
  selections and non-secret endpoints. The Zenoh Config route uses TAK's
  PageHeader/HUD card design while preserving EFDI transport, namespace,
  federation-target, and policy fields.
- TAK Server output uses the mTLS `cot-bridge` service and the
  `bridges/cot_bridge.py` entrypoint with a TAK-issued certificate, not the
  Zenoh certificate. There is no EFDI-managed TAK/SitaWare CoT receive bridge
  in the current runtime catalog.

## Installed agent skills

The repository-local `.agents/skills/` contains Supabase and Supabase Postgres guidance installed for agent use. EFDI itself uses ordinary MariaDB through SQLAlchemy, not Supabase; do not migrate or introduce Supabase merely because those skills are installed.
