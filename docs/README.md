# EFDI documentation

Start here. This is the map of every document under `docs/`, organised by what
you are trying to do.

## New to the project?

Read **[EXPLAINED.md](EXPLAINED.md)** first — it explains the whole system from
A to Z (what it is, how data flows, how it connects, how to run it) in one pass,
without needing the code. Everything below is reference detail behind it.

## By need

| I want to… | Read |
|---|---|
| Understand the whole project, ground up | [EXPLAINED.md](EXPLAINED.md) |
| Set up a bare host from scratch (Docker, Python, git, NetBird) | [HOST_SETUP.md](HOST_SETUP.md) *(Lietuviškai: [PARUOSIMAS.md](PARUOSIMAS.md))* |
| Install / deploy the pod | [INSTALL.md](INSTALL.md) *(Lietuviškai: [DIEGIMAS.md](DIEGIMAS.md))* |
| See the v0 architecture / secure-pipe core | [architecture.md](architecture.md) |
| Know how the Zenoh topic keys are structured | [topic-taxonomy.md](topic-taxonomy.md) |
| See which sensors/protocols/C2 systems are wired | [INTEGRATIONS.md](INTEGRATIONS.md) |
| Add a new sensor/protocol, step by step | [ADDING_A_SENSOR.md](ADDING_A_SENSOR.md) |
| Operate the Zenoh Admin web GUI | [ZENOH_ADMIN.md](ZENOH_ADMIN.md) |
| Wire up TAK/SitaWare C2 (both directions) | [C2_RUNBOOK.md](C2_RUNBOOK.md) |
| Fix a specific symptom (icon missing, feed not appearing, ...) | [TROUBLESHOOTING.md](TROUBLESHOOTING.md) |
| Understand the security & sovereignty model | [SECURITY.md](SECURITY.md) |
| Contribute changes | [CONTRIBUTING.md](CONTRIBUTING.md) |
| See planned/proposed work | [superpowers/plans/](superpowers/plans/) |
| Follow coding rules & ASTERIX decode gotchas | [../CLAUDE.md](../CLAUDE.md) |
| Avoid operational mistakes already made once | [GOTCHAS.md](GOTCHAS.md) |

## Document roles

| Document | Role | Size |
|---|---|---|
| `EXPLAINED.md` | Ground-up narrative of the entire system | medium |
| `HOST_SETUP.md` / `PARUOSIMAS.md` | Bare-host bootstrap: Docker, Python, git, NetBird (EN / LT) | medium |
| `INSTALL.md` / `DIEGIMAS.md` | Step-by-step deployment (EN / LT) | large |
| `architecture.md` | The v0 "secure-pipe core" shape | small |
| `topic-taxonomy.md` | The published-key contract, incl. `/tracks/v1` | small |
| `INTEGRATIONS.md` | Source / protocol / C2 integration matrix | medium |
| `ADDING_A_SENSOR.md` | Step-by-step: connect a brand-new sensor/protocol | small |
| `ZENOH_ADMIN.md` | The Zenoh Admin web GUI: setup, pages, roles, managed CA | large |
| `C2_RUNBOOK.md` | TAK/SitaWare bidirectional operator runbook | medium |
| `TROUBLESHOOTING.md` | Symptom-first fixes for common problems | medium |
| `GOTCHAS.md` | Operational lessons learned (infra, not decode bugs) | small |
| `SECURITY.md` | Security policy & disclosure | small |
| `CONTRIBUTING.md` | Contribution guidance | small |
| `superpowers/plans/` | Dated design plans (proposed → implemented) | — |
| `superpowers/specs/` | Specifications | — |

## Conventions

- Plans live in `superpowers/plans/YYYY-MM-DD-<slug>.md` and carry a
  **Status** line (`proposed` / `implemented`).
- `INSTALL.md` (English) and `DIEGIMAS.md` (Lithuanian) are translations of the
  same guide — keep them in step when either changes. `HOST_SETUP.md` /
  `PARUOSIMAS.md` are the same pairing, one level earlier (bare host, before
  the repo is even cloned).
- The authoritative coding rules and the ASTERIX bit-level gotchas live in the
  repo-root [`CLAUDE.md`](../CLAUDE.md), not here.
- `ZENOH_ADMIN.md`, `C2_RUNBOOK.md`, and `TROUBLESHOOTING.md` are shared
  English-only (`DIEGIMAS.md` points to them directly) — they're almost
  entirely commands, env vars, and field names that stay English either way,
  per `DIEGIMAS.md`'s own stated convention.
