# EFDI Agent Memory

Read `CLAUDE.md` first; its project constraints apply to every agent and tool.

## Non-negotiable workflow

- Never run `git add`, `git commit`, or `git push`. Leave index management,
  commits, and GitHub pushes entirely to the user.
- Preserve unrelated staged/worktree changes; do not alter the existing index.
- Certificates, API keys, `compose/.env`, runtime state, and real deployment identifiers never enter tracked files.
- After Python bridge/layer edits, compile-check all bridge and layer modules. Also run the frontend type-check/build, shell syntax/ShellCheck, Compose YAML validation, and `git diff --check` when relevant.

## Runtime architecture

- `zenoh-router`, PostgreSQL, socket proxy, `zenoh-admin`, and its Caddy proxy are Docker infrastructure.
- Sensor bridges and output layers are ordinary Python processes launched by `start.sh` or `run.sh`. Their logs and PID files live under `${POD_STATE_DIR}/logs` and `${POD_STATE_DIR}/.pids`. Do not reintroduce one Docker container per bridge/layer.
- `stop.sh` stops those PID-managed processes. `compose/rebuild.sh` rebuilds infrastructure only.
- The admin backend is FastAPI + async SQLAlchemy/PostgreSQL; the UI is React/Vite/TanStack Router/Tailwind.
- Data flows external sensor/protocol → Python bridge → local Zenoh router → output layer (CoT/TAK/SitaWare/etc.). Namespace shape is `{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/...`.

## Authoritative handoff material

- `.superpowers/sdd/progress.md` is Claude Code's execution ledger.
- `.superpowers/sdd/*-brief.md` and `*-report.md` record exact task contracts, reviews, fixes, and deferred live checks.
- `docs/superpowers/plans/` and `docs/superpowers/specs/` are ignored working documents but remain the design source for ongoing work.
- Root/deployment/client/example Markdown documents describe the shipped operational contract. Some old `docs/architecture.md` language is historical and should not override current README/launcher behavior.

## Current work state (2026-07-14)

- Completed before this handoff: publish-script builder, direct signed federation push/status, ACL and replay protection, configurable namespace prefix, TAK-style UI pass, and topology ACL.
- Completed in the current continuation: restored PID-managed Python bridge runtime; removed live per-bridge containers and their Compose/admin-control wrappers; completed topology aggregation and shared UI map integration for Dashboard, Topology, and Federation.
- Bridge/protocol audit completed: launchers now provide the shared Python module path and `run.sh all` covers every retained integration; HTTP/WebSocket inputs are bounded; SitaWare HQ inbound and SitaWare Edge NVG outbound remain intentionally separate; CoT parsing/routing is hardened; CAT-48/34 were checked against current EUROCONTROL specifications. CAT-20/21/62 are explicitly documented legacy UAPs, and Link-16 TCP is disabled until a gateway framing ICD exists.
- Windy, yr.no, PurpleAir, OSM/Overpass, and N2YO bridges and their dedicated schemas/downstream wiring were removed at the user's request.
- Static checks pass. Live browser and real multi-pod topology/config-push checks still require rebuilding/deploying the admin containers.
- Zero-trust cascading relay remains the next planned feature, but its plan requires successful live topology verification first.
- `tests/smoke/README.md` still records a genuine missing CI loopback test and live validation runbook.

## Installed agent skills

The repository-local `.agents/skills/` contains Supabase and Supabase Postgres guidance installed for agent use. EFDI itself currently uses ordinary PostgreSQL through SQLAlchemy, not Supabase; do not migrate or introduce Supabase merely because those skills are installed.
