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
| Install / deploy the pod | [INSTALL.md](INSTALL.md) *(Lietuviškai: [DIEGIMAS.md](DIEGIMAS.md))* |
| See the v0 architecture / secure-pipe core | [architecture.md](architecture.md) |
| Know how the Zenoh topic keys are structured | [topic-taxonomy.md](topic-taxonomy.md) |
| See which sensors/protocols/C2 systems are wired | [INTEGRATIONS.md](INTEGRATIONS.md) |
| Understand the security & sovereignty model | [SECURITY.md](SECURITY.md) |
| Contribute changes | [CONTRIBUTING.md](CONTRIBUTING.md) |
| See planned/proposed work | [superpowers/plans/](superpowers/plans/) |
| Follow coding rules & ASTERIX gotchas | [../CLAUDE.md](../CLAUDE.md) |

## Document roles

| Document | Role | Size |
|---|---|---|
| `EXPLAINED.md` | Ground-up narrative of the entire system | medium |
| `INSTALL.md` / `DIEGIMAS.md` | Step-by-step deployment (EN / LT) | large |
| `architecture.md` | The v0 "secure-pipe core" shape | small |
| `topic-taxonomy.md` | The published-key contract, incl. `/tracks/v1` | small |
| `INTEGRATIONS.md` | Source / protocol / C2 integration matrix | medium |
| `SECURITY.md` | Security policy & disclosure | small |
| `CONTRIBUTING.md` | Contribution guidance | small |
| `superpowers/plans/` | Dated design plans (proposed → implemented) | — |
| `superpowers/specs/` | Specifications | — |

## Conventions

- Plans live in `superpowers/plans/YYYY-MM-DD-<slug>.md` and carry a
  **Status** line (`proposed` / `implemented`).
- `INSTALL.md` (English) and `DIEGIMAS.md` (Lithuanian) are translations of the
  same guide — keep them in step when either changes.
- The authoritative coding rules and the ASTERIX bit-level gotchas live in the
  repo-root [`CLAUDE.md`](../CLAUDE.md), not here.
