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
| Set up a bare host from scratch, install/deploy the pod, see which sensors/protocols/C2 systems are wired, add a new sensor step by step, wire up TAK/SitaWare C2, or fix a specific symptom | [INSTALL.md](INSTALL.md) *(Lietuviškai: [DIEGIMAS.md](DIEGIMAS.md))* — one guide, §§1-13 |
| Know how the Zenoh topic keys are structured | [topic-taxonomy.md](topic-taxonomy.md) |
| Operate the Zenoh Admin web GUI | [ZENOH_ADMIN.md](ZENOH_ADMIN.md) |
| Understand the security & sovereignty model | [SECURITY.md](SECURITY.md) |
| Contribute changes | [CONTRIBUTING.md](CONTRIBUTING.md) |
| See planned/proposed work | [superpowers/plans/](superpowers/plans/) |
| Follow coding rules & ASTERIX decode gotchas | [../CLAUDE.md](../.ai/.claude/CLAUDE.md) |

## Document roles

| Document | Role | Size |
|---|---|---|
| `EXPLAINED.md` | Ground-up narrative of the entire system | medium |
| `INSTALL.md` / `DIEGIMAS.md` | The deployment guide (EN / LT) — bare-host bootstrap, install, configure, launch, ATAK setup, service reference, integrations matrix + client SDKs, C2 bidirectional runbook, adding a new sensor, operations, troubleshooting + gotchas, Zenoh Admin GUI pointer, CI | very large |
| `topic-taxonomy.md` | The published-key contract, incl. `/tracks/v1` | small |
| `ZENOH_ADMIN.md` | The Zenoh Admin web GUI: setup, pages, roles, managed CA | large |
| `SECURITY.md` | Security policy & disclosure | small |
| `CONTRIBUTING.md` | Contribution guidance | small |
| `superpowers/plans/` | Dated design plans (proposed → implemented) | — |
| `superpowers/specs/` | Specifications | — |

## Conventions

- Plans live in `superpowers/plans/YYYY-MM-DD-<slug>.md` and carry a
  **Status** line (`proposed` / `implemented`).
- `INSTALL.md` (English) and `DIEGIMAS.md` (Lithuanian) are translations of the
  same guide, cover-to-cover — keep them in step when either changes. Every
  section that used to be its own document (bare-host bootstrap, integrations
  matrix, C2 runbook, adding a sensor, troubleshooting, gotchas) is now a
  numbered section inside both, fully translated in `DIEGIMAS.md` rather than
  pointing back to an English-only original.
- The authoritative coding rules and the ASTERIX bit-level gotchas live in the
  repo-root [`../.ai/.claude/CLAUDE.md`](../.ai/.claude/CLAUDE.md), not here.
- `ZENOH_ADMIN.md` remains its own document (large enough on its own — web GUI
  setup, pages, roles, managed CA) rather than folded into `INSTALL.md`.
