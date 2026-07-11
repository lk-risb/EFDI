<div align="center">

# EFDI

**Partner-custodial sensor bridge stack — tactical sensors → Zenoh pub/sub → ATAK CoT, on your own hardware, under your own custody.**

[![Zenoh](https://img.shields.io/badge/Zenoh-1.9.0-blue)](https://zenoh.io/)
[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker%20Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![ATAK](https://img.shields.io/badge/ATAK-CoT-4c9a4c)](https://tak.gov/)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue)](https://www.apache.org/licenses/LICENSE-2.0)

[![shellcheck](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/shellcheck.yml)
[![compose-validate](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/compose-validate.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/compose-validate.yml)
[![bridge-syntax](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/bridge-syntax.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/bridge-syntax.yml)
[![zenoh-admin-frontend](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/zenoh-admin-frontend.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/zenoh-admin-frontend.yml)
[![docker-build](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/docker-build.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/docker-build.yml)

</div>

The **EFDI-goat-pod** ingests data from tactical sensors (radars, drone detection networks, datalinks, UAV telemetry), routes everything through a local Zenoh publish/subscribe fabric, and delivers it to ATAK as Cursor-on-Target (CoT) over UDP multicast or TAK Server TCP.

This repository is the EFDI-partner collaboration surface. It carries **no goat-internal infrastructure, credentials, or private links** — everything here is safe to develop against openly.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Networking](#networking)
- [Ports](#ports)
- [Bundle contents](#bundle-contents)
- [Repository layout](#repository-layout)
- [Deployment](#deployment)
- [Operations](#operations)
- [EFDI sandbox notes](#efdi-sandbox-notes)
- [License](#license)

---

## What it does

```
[Giraffe AMB]    ──ASTERIX UDP──► asterix_bridge     ─┐
[dronuradaras.lt]──REST/HTTPS──► dronuradaras_bridge  ─┤
[SitaWare HQ]    ──REST/HTTPS──► sitaware_bridge      ─┤──► Zenoh router (local) ──► cot_layer ──► ATAK
[NFFI source]    ──TCP/XML─────► nato_nffi_layer      ─┤                                      ├──► TAK Server
[Link-16]        ──UDP/TCP────► link16_bridge         ─┤                                      └──► nato_nvg_layer ──► SitaWare Edge
[MAVLink]        ──UDP/TCP────► mavlink_bridge        ─┘              └──► track_fusion_layer
```

Each bridge publishes tracks to a Zenoh router under a structured topic hierarchy (`{namespace}/{domain}/{source}/{protocol}/{affiliation}/{type}/v1`). Output layers subscribe to wildcard patterns, convert to CoT XML, and deliver downstream.

---

## Architecture

| Service | Role |
|---|---|
| `zenoh-router` | Local pub/sub fabric — mTLS to the fabric, plaintext TCP for local bridges/GUI |
| `asterix_bridge` | ASTERIX CAT-48 (tracks) + CAT-34 (radar status), Giraffe AMB |
| `dronuradaras_bridge` | REST polling, acoustic sensors + drone detection events |
| `sitaware_bridge` | SitaWare HQ friendly-force REST polling (inbound) |
| `nato_nffi_layer` | NATO NFFI (STANAG 4677) friendly-force XML feed (inbound) |
| `nato_nvg_layer` | EFDI tracks → SitaWare Edge NVG v2 push (outbound) |
| `link16_bridge` | JREAP-C UDP/TCP |
| `mavlink_bridge` | MAVLink 2 UDP/TCP |
| `cot_layer` | CoT XML output — UDP multicast + TAK Server TCP |
| `track_fusion_layer` | ASTERIX CAT-48 / CAT-21 correlation |
| `zenoh-admin` | FastAPI + React panel — router status, config editing, service health |

All bridges are lightweight Python processes with no shared state beyond the Zenoh session. They publish self-describing JSON payloads. The CoT layer translates any incoming Zenoh topic to the appropriate MIL-STD-2525C CoT type using a topic-pattern → CoT-type map — adding a new sensor requires no changes to the output layer.

Client onboarding is certificate-based: each pod gets a signed mTLS bundle issued by EFDI, scoped to its own namespace. Services run as individual host processes managed by PID files in `compose/state/.pids/`. The only containerised components are the Zenoh router and the zenoh-admin panel — this keeps the data path inspectable, restartable in isolation, and free of container networking overhead.

### SitaWare integration (bidirectional)

Three independent paths, each optional and separately configured — enable whichever your deployment actually has:

| Direction | Bridge/layer | Path |
|---|---|---|
| Inbound | `sitaware_bridge.py` | SitaWare **HQ** REST API → EFDI (poll-based) |
| Inbound | `nato_nffi_layer.py` | NATO NFFI (STANAG 4677) XML feed → EFDI (streaming TCP) |
| Outbound | `nato_nvg_layer.py` | EFDI tracks → SitaWare **Edge** NVG v2 REST API (push) |

HQ and Edge are typically separate SitaWare products/hosts with separate credentials — see `SITAWARE_*` vs `SITAWARE_NVG_*` in `compose/.env.example`.

---

## Networking

Every pod dials a single fabric endpoint over mutual TLS — the pod writes only within its assigned namespace (`<slot_id>/**`), and publishes outside it are silently denied by the router ACL. Where a partner's deployment routes its whole subnet over a VPN mesh (e.g. NetBird), no separate mesh-specific address is needed; the mTLS connection is the actual security boundary, not the transport underneath it.

---

## Ports

| Port / address | Direction | Purpose |
|---|---|---|
| TCP 7448 | localhost | Local Zenoh router (plaintext, bridges + zenoh-admin only) |
| TCP 7447 TLS | outbound | Remote/fabric Zenoh router (mTLS) |
| UDP `<CAT48_PORT>` (default 30048) | inbound | Giraffe AMB ASTERIX stream |
| UDP multicast `239.2.3.1:6969` | outbound | CoT delivery to ATAK |
| HTTP 8890 | inbound | zenoh-admin panel (web UI) |
| TCP `<NFFI_PORT>` (default 7010) | inbound | NATO NFFI friendly-force feed |
| HTTPS | outbound | SitaWare HQ/Edge, dronuradaras.lt REST APIs |

See [INSTALL.md](INSTALL.md) for the full network prerequisites table.

---

## Bundle contents

| Component | Status | Notes |
|---|---|---|
| Zenoh router | ✅ | `eclipse/zenoh:1.9.0`, digest-pinned, mTLS, ACL-scoped |
| ASTERIX bridge | ✅ | CAT-48 (tracks) + CAT-34 (radar status), Giraffe AMB |
| dronuradaras bridge | ✅ | REST polling, acoustic sensors + drone detection events |
| SitaWare HQ bridge | ✅ | Friendly-force REST polling (inbound) |
| NATO NFFI layer | ✅ | STANAG 4677 friendly-force XML feed (inbound) |
| SitaWare Edge (NVG) layer | ✅ | EFDI tracks → SitaWare Edge push (outbound) |
| Link-16 bridge | ✅ | JREAP-C UDP/TCP |
| MAVLink bridge | ✅ | MAVLink 2 UDP/TCP |
| CoT output layer | ✅ | UDP multicast + TAK Server TCP |
| Track fusion layer | ✅ | ASTERIX CAT-48 / CAT-21 correlation |
| zenoh-admin panel | ✅ | FastAPI + React — router status, config editor, health dashboard |

---

## Repository layout

```
EFDI/
├── README.md                     this file
├── INSTALL.md                    English deployment guide
├── DIEGIMAS.md                   Lithuanian deployment guide
├── SECURITY.md                   vulnerability reporting policy
├── compose/
│   ├── docker-compose.yml        Zenoh router + zenoh-admin containers
│   ├── .env.example              configuration template
│   ├── certs/                    (gitignored) router mTLS certificates
│   ├── state/                    (gitignored) runtime state — logs, pids, Zenoh config
│   ├── zenoh-admin/               FastAPI + React admin panel
│   └── bridge/
│       ├── bridges/              sensor bridge scripts
│       └── layers/               output layer scripts
├── docs/                         (gitignored) design specs, working notes
├── start.sh                      interactive service launcher
├── stop.sh                       service teardown
└── dev.sh                        disposable local Postgres + API for zenoh-admin UI preview
```

---

## Deployment

See **[INSTALL.md](INSTALL.md)** for the full English deployment guide, or **[DIEGIMAS.md](DIEGIMAS.md)** for the Lithuanian version. Both cover prerequisites, certificate setup, configuration reference, and troubleshooting.

---

## Operations

```bash
./start.sh              # interactive launcher — pick services, prompts for missing config
./stop.sh                # tear down running services
./dev.sh up              # zenoh-admin UI preview only — disposable Postgres + API, no router/certs
./dev.sh down             # tear down the dev preview stack
docker compose logs -f zenoh-router   # follow the router's logs
```

---

## EFDI sandbox notes

- **Enrollment** — a NetBird setup-key issued at onboarding. No silent control plane on the sandbox.
- **Zenoh identity** — a per-UUID slot assigned by the portal. Your cert CN, topic prefix, and write namespace all anchor to that slot.
- **Write namespace** — `<slot_id>/**`. Publishes outside this prefix are silently denied by the router ACL.

---

## License

Apache-2.0. See [`LICENSE`](LICENSE).
