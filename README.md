<div align="center">

# efdi-moon-pod

**Partner-custodial sensor bridge stack — tactical sensors → Zenoh pub/sub → ATAK CoT, on your own hardware, under your own custody.**

![Zenoh](https://img.shields.io/badge/Zenoh-1.9.0-blue)
![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker%20Compose-2496ED?logo=docker&logoColor=white)
![ATAK](https://img.shields.io/badge/ATAK-CoT-4c9a4c)
![License](https://img.shields.io/badge/License-Apache--2.0-blue)

[![shellcheck](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/shellcheck.yml)
[![compose-validate](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/compose-validate.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/compose-validate.yml)
[![bridge-syntax](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/bridge-syntax.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/bridge-syntax.yml)
[![zenoh-admin-frontend](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/zenoh-admin-frontend.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/zenoh-admin-frontend.yml)
[![docker-build](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/docker-build.yml/badge.svg)](https://github.com/risblicencijos/efdi-moon-pod/actions/workflows/docker-build.yml)

</div>

The **EFDI moon-pod** ingests data from tactical sensors (radars, drone detection networks, datalinks, UAV telemetry), routes everything through a local Zenoh publish/subscribe fabric, and delivers it to ATAK as Cursor-on-Target (CoT) over UDP multicast or TAK Server TCP.

This repository is the EFDI-partner collaboration surface. It carries **no goat-internal infrastructure, credentials, or private links** — everything here is safe to develop against openly.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Bundle contents](#bundle-contents)
- [Repository layout](#repository-layout)
- [Deployment](#deployment)
- [EFDI sandbox notes](#efdi-sandbox-notes)
- [License](#license)

---

## What it does

```
[Giraffe AMB]    ──ASTERIX UDP──► asterix_bridge     ─┐
[dronuradaras.lt]──REST/HTTPS──► dronuradaras_bridge  ─┤
[SitaWare]       ──REST/HTTPS──► sitaware_bridge      ─┤──► Zenoh router (local) ──► cot_layer ──► ATAK
[Link-16]        ──UDP/TCP────► link16_bridge         ─┤                                      └──► TAK Server
[MAVLink]        ──UDP/TCP────► mavlink_bridge        ─┘              └──► track_fusion_layer
```

Each bridge publishes tracks to a Zenoh router under a structured topic hierarchy (`{namespace}/{domain}/{source}/{protocol}/{affiliation}/{type}/v1`). Output layers subscribe to wildcard patterns, convert to CoT XML, and deliver downstream.

---

## Architecture

### Data plane

All bridges are lightweight Python processes with no shared state beyond the Zenoh session. They publish self-describing JSON payloads. The CoT layer translates any incoming Zenoh topic to the appropriate MIL-STD-2525C CoT type using a topic-pattern → CoT-type map — adding a new sensor requires no changes to the output layer.

### Transport security

The Zenoh router uses mutual TLS with per-pod certificates issued by EFDI. The pod writes only within its assigned namespace (`<slot_id>/**`); publishes outside it are silently denied by the router ACL. Certificates are stored outside the repository and never committed.

### Process model

Services run as individual host processes managed by PID files in `.pids/`. The only containerised component is the Zenoh router (Docker). This keeps the stack inspectable, restartable in isolation, and free of container networking overhead on the data path.

---

## Bundle contents

| Component | Status | Notes |
|---|---|---|
| Zenoh router | ✅ | `eclipse/zenoh:1.9.0`, digest-pinned, mTLS, ACL-scoped |
| ASTERIX bridge | ✅ | CAT-48 (tracks) + CAT-34 (radar status), Giraffe AMB |
| dronuradaras bridge | ✅ | REST polling, acoustic sensors + drone detection events |
| SitaWare bridge | ✅ | Friendly-force REST polling |
| Link-16 bridge | ✅ | JREAP-C UDP/TCP |
| MAVLink bridge | ✅ | MAVLink 2 UDP/TCP |
| CoT output layer | ✅ | UDP multicast + TAK Server TCP |
| Track fusion layer | ✅ | ASTERIX CAT-48 / CAT-21 correlation |

---

## Repository layout

```
efdi-moon-pod/
├── README.md                     this file
├── INSTALL.md                    English deployment guide
├── DIEGIMAS.md                   Lithuanian deployment guide
├── compose/
│   ├── docker-compose.yml        Zenoh router container
│   ├── .env.example              configuration template
│   └── bridge/
│       ├── bridges/              sensor bridge scripts
│       └── layers/               output layer scripts
├── docs/
│   └── architecture.md           bundle shape + data flow
├── start.sh                      interactive service launcher
└── stop.sh                       service teardown
```

---

## Deployment

See **[INSTALL.md](INSTALL.md)** for the full English deployment guide, or **[DIEGIMAS.md](DIEGIMAS.md)** for the Lithuanian version. Both cover prerequisites, certificate setup, configuration reference, and troubleshooting.

---

## EFDI sandbox notes

- **Enrollment** — a NetBird setup-key issued at onboarding. No silent control plane on the sandbox.
- **Zenoh identity** — a per-UUID slot assigned by the portal. Your cert CN, topic prefix, and write namespace all anchor to that slot.
- **Write namespace** — `<slot_id>/**`. Publishes outside this prefix are silently denied by the router ACL.

---

## License

Apache-2.0. See [`LICENSE`](LICENSE).
