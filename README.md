<div align="center">

# EFDI

**Partner-custodial sensor fusion bridge — tactical sensors → Zenoh pub/sub → ATAK *and* SitaWare HQ, on your own hardware, under your own custody.**

[![Zenoh](https://img.shields.io/badge/Zenoh-1.9.0-blue)](https://zenoh.io/)
[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Debian](https://img.shields.io/badge/Debian-13_trixie-A81D33?logo=debian&logoColor=white)](https://www.debian.org/)
[![Docker](https://img.shields.io/badge/Docker%20Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![ATAK](https://img.shields.io/badge/ATAK-CoT-4c9a4c)](https://tak.gov/)
[![SitaWare](https://img.shields.io/badge/SitaWare-NVG%202.0.2-6c5ce7)](https://systematic.com/)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue)](https://www.apache.org/licenses/LICENSE-2.0)

[![shellcheck](https://github.com/lk-risb/EFDI/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/lk-risb/EFDI/actions/workflows/shellcheck.yml)
[![compose-validate](https://github.com/lk-risb/EFDI/actions/workflows/compose-validate.yml/badge.svg)](https://github.com/lk-risb/EFDI/actions/workflows/compose-validate.yml)
[![bridge-syntax](https://github.com/lk-risb/EFDI/actions/workflows/bridge-syntax.yml/badge.svg)](https://github.com/lk-risb/EFDI/actions/workflows/bridge-syntax.yml)
[![zenoh-admin-frontend](https://github.com/lk-risb/EFDI/actions/workflows/zenoh-admin-frontend.yml/badge.svg)](https://github.com/lk-risb/EFDI/actions/workflows/zenoh-admin-frontend.yml)
[![docker-build](https://github.com/lk-risb/EFDI/actions/workflows/docker-build.yml/badge.svg)](https://github.com/lk-risb/EFDI/actions/workflows/docker-build.yml)

</div>

The **EFDI pod** ingests tactical sensor data — radars, drone-detection networks, datalinks, UAV telemetry — normalizes it onto a local Zenoh publish/subscribe fabric, and delivers a single fused track picture to **both** major C2 surfaces at once: **ATAK / TAK Server** as Cursor-on-Target, and **SitaWare HQ** as NVG 2.0.2. The two paths are independent and symmetric — enable either, both, or neither.

This repository is the EFDI-partner collaboration surface. It carries **no partner-internal infrastructure, credentials, or private links** — everything here is safe to develop against openly.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [C2 integrations](#c2-integrations)
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
                  INGRESS (bridges/protocols)          FABRIC            EGRESS (layers)

[CAT-34/48]    ──UDP────────────────► udp_ingress_bridge ──┐
[dronuradaras] ──REST/HTTPS─────────► dronuradaras_bridge ─┤                    │
[SitaWare HQ]  ──REST/JSON──────────► sitaware_bridge     ─┤
[TAK Server]   ──CoT stream─────────► tak_bridge          ─┘
                                          └─► fusion (CAT-48 / CAT-21 correlation)
```

Each inbound bridge or protocol publishes normalized tracks under a structured topic hierarchy (`{namespace}/{partner}/{domain}/{source}/{protocol}/{affiliation}/{type}/…`). Output layers subscribe to wildcard patterns, translate to their target format, and deliver downstream. Adding a sensor never requires touching an output layer — the CoT and NVG translators derive symbology from the topic, not from per-source code.

Zenoh-native translators are also bundled for CAP 1.2 alerts, sensor health, and mission routes. Their raw publishers write complete payloads below `raw/**`, so a router host needs no radio or sensor hardware of its own.

The live data-plane payload is JSON. The `.proto` files beside each translator under `compose/protocols/` describe the intended typed contracts but do not by themselves make a topic Protobuf; a binary migration would dual-publish onto a new topic version so existing CoT, TAK, NVG, and fusion consumers keep decoding safely.

---

## Architecture

| Service | Direction | Role |
|---|---|---|
| `zenoh-router` | — | Local pub/sub fabric — mTLS to the wider fabric, plaintext TCP for local bridges/GUI |
| `tak_layer` | Zenoh → C2 | **CoT XML output to TAK Server** (authenticated TCP/mTLS) |
| `tak_bridge` | C2 → Zenoh | TAK Server / CoT ingress — normalizes CoT back onto the fabric |
| `sitaware_layer` | Zenoh → C2 | **NVG 2.0.2 feed served to SitaWare HQ** (HQ's Import Subscription polls it) |
| `sitaware_bridge` | C2 → Zenoh | SitaWare HQ friendly-force REST/JSON polling (deployment-documented resource) |
| `udp_ingress_bridge` | → Zenoh | Generic UDP ingress; preserves every datagram and safely dispatches complete ASTERIX frames |
| `asterix_bridge` | Zenoh → local Zenoh | Relays validated `raw/asterix/catN` frames from a remote sensor-side router into this pod |
| `asterix` | protocol | ASTERIX family: CAT-001/002/004/007/008/009/010/011/015/016/017/018/019/020/021/023/025/032/034/048/062/063/065/150/205/240/247 translators |
| `stanag` | protocol | STANAG family, one file (`stanag.py --proto N`): 4586 VSM/CUCS feed, 4607 GMTI ground radar, 4609 SRT transport + KLV decode, 5516 Link 16 over JREAP-C |
| `dronuradaras_bridge` | → Zenoh | REST polling of currently-online acoustic sensors + drone-detection events |
| Partner ADS-B / CAT-21 | → Zenoh | Registered fabric topics or ASTERIX CAT-021 → normalized aircraft tracks |
| `flex335_bridge` | → Zenoh | SAPIENT BSI Flex 335 v2 sensor/detection ingress |
| `4586_bridge` / `5516_bridge` | → Zenoh | Optional STANAG 4586 / 5516 socket ingress; publishes raw bytes only |
| `nffi` | protocol | NATO NFFI / ADatP-36 (STANAG 5527) friendly-force XML translator |
| `cap` / `mission_route` | protocol | Alerts/areas, UAV routes and corridors |
| `sensor_health` | protocol | Vendor-neutral sensor-status translator |
| `mqtt_bridge` | → Zenoh | Generic MQTT ingress |
| `sparkplug` | protocol | Eclipse Sparkplug B (MQTT protobuf); resolves BIRTH-declared metric aliases |
| `fusion` | fabric | ASTERIX CAT-48 / CAT-21 correlation |
| `zenoh-admin` | — | FastAPI + React panel — router status, config, lifecycle, endpoints, write-only credentials |

**Naming convention.** The prefix carries direction: a **`_bridge`** brings an external system *into* the fabric, a **`_layer`** writes the fabric *out* to a C2 system. Which side opens the socket is irrelevant — SitaWare polling `sitaware_layer`'s served feed is still egress, so it is a `_layer`.

All bridges and protocols are lightweight Python processes with no shared state beyond the Zenoh session. Native processes connect to `ZENOH_LOCAL_ENDPOINT` (default `tcp/127.0.0.1:7448`) and never default to a remote router; only the local `zenoh-router` owns `ZENOH_FABRIC_ENDPOINTS`, so changing a parent or federation address does not require restarting every adapter. Services run as individual host processes managed by PID files in `compose/state/.pids/` and are kept alive by `supervisor.py`; the only containerised components are the Zenoh router and the zenoh-admin panel, keeping the data path inspectable and restartable in isolation.

ASTERIX compatibility is edition-specific: CAT-001 Ed. 1.4, CAT-002 Ed. 1.2, CAT-004 Ed. 1.13, CAT-007 Ed. 1.12, CAT-008 Ed. 1.3, CAT-009 Ed. 2.1, CAT-010 Ed. 1.1, CAT-011 Ed. 1.3, CAT-015 Ed. 1.2, CAT-016 Ed. 1.0, CAT-017 Ed. 1.3, CAT-018 Ed. 1.8, CAT-019 Ed. 1.3, CAT-020 Ed. 1.11, CAT-021 Ed. 2.7, CAT-023 Ed. 1.3, CAT-025 Ed. 1.6, CAT-032 Ed. 1.2, CAT-034 Ed. 1.29, CAT-048 Ed. 1.32, CAT-062 Ed. 1.21, CAT-063 Ed. 1.7, CAT-065 Ed. 1.6, CAT-150 Ed. 3.0, CAT-205 Ed. 1.0, CAT-240 Ed. 1.3, CAT-247 Ed. 1.3. A producer using another edition needs a separate, explicitly selected profile — do not feed it into one of these decoders by resemblance.

---

## C2 integrations

EFDI treats ATAK/TAK and SitaWare HQ as first-class, co-equal consumers. Each has an **egress** layer (fabric → C2) and an **ingress** bridge (C2 → fabric); enabling one direction never enables the other. Both are selected and configured independently in `start.sh` or the admin panel.

### ATAK / TAK Server

| Direction | Service | Path |
|---|---|---|
| Egress | `tak_layer.py` | Zenoh tracks → CoT XML → authenticated TAK Server TCP |
| Ingress | `tak_bridge.py` | TAK Server CoT stream → normalized tracks on the fabric |

`tak_layer` translates any incoming Zenoh topic to the appropriate MIL-STD-2525C CoT type via a topic-pattern → CoT-type map, then delivers it over an authenticated TCP connection to a TAK Server. **Use the mTLS streaming input (`TAK_PORT=8089`, `TAK_TLS=1`)** with a TAK-issued client bundle — the anonymous `8087` input accepts and ACKs CoT but does **not** distribute it to `8089` subscribers, which looks like "delivered but invisible." Multiple TAK hosts are treated as the same server: the layer does not rotate its certificate between them, avoiding reconnect storms. TAK client certificates are issued by **TAK, not by the Zenoh CA**.

### SitaWare HQ

| Direction | Service | Path |
|---|---|---|
| Egress | `sitaware_layer.py` | Zenoh tracks → NVG 2.0.2 document served over HTTP(S); HQ's Import Subscription polls it |
| Ingress | `sitaware_bridge.py` | HQ friendly-force REST/JSON resource (deployment-documented) |

`sitaware_layer` maintains a bounded snapshot of live EFDI tracks and serves **one** NVG 2.0.2 document for HQ's **NVG → NVG Import Subscription** manager to GET/HEAD on a poll interval. It requires dedicated HTTP Basic credentials unless anonymous access is explicitly enabled, expires stale tracks, stamps every object with an NVG `TimeSpan`, and attaches standard symbol modifiers plus bounded `ExtendedData`. Because NVG data documents carry no per-object delete, omission from a later snapshot does not retract an object HQ already imported. There is no separate NVG-XML ingest bridge — `sitaware_bridge` is the only ingress, reading HQ's REST/JSON resource.

**Symbology is shared and standards-native.** Both egress paths derive affiliation from the same classifiers — RU/BY ICAO and MMSI ranges resolve to hostile rather than assigning one static affiliation per feed. ADS-B emitter categories `C1`/`C2` are surface emergency/service vehicles and map to neutral ground vehicles (`a-n-G-E-V` / `SNGPEV----*****`) on both surfaces, while a taxiing aircraft (bare `on_ground`) stays an aircraft. Where HQ 6.22 renders a standards-native scheme as Unknown, the layer substitutes HQ's supported SIDC (e.g. neutral emplaced-sensor for weather stations).

### Activation summary

| Direction | Select | Required runtime contract |
|---|---|---|
| Zenoh → TAK Server | `tak_layer` | `TAK_HOST`/`TAK_PORT`; `TAK_TLS=1` + TAK-issued client bundle for mTLS |
| TAK Server → Zenoh | `tak-bridge` | Reachable CoT stream input |
| Zenoh → SitaWare HQ | `sitaware_layer` | `SITAWARE_HQ_NVG_PORT` + an HQ NVG Import Subscription pointed at it |
| SitaWare HQ → Zenoh | `sitaware` | Deployment-documented REST resource, schema, URL and credentials |

SitaWare's REST resource path is **not** assumed — there is no universal `/rest/v2/units`; confirm it against the deployment's ICD. Full commands and the operator-side HQ subscription fields are in [09 — C2 ↔ Zenoh Runbook](docs/09-c2-zenoh-runbook.md).

Inbound C2 records are written under this pod's normal `{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/…` namespace, becoming ordinary Zenoh data for authorized subscribers and federated partner routers; the router ACL and federation policy — not the C2 adapter — decide which pods receive them.

---

## Networking

Every pod dials a single fabric endpoint over mutual TLS — it writes only within its assigned namespace (`<slot_id>/**`), and publishes outside it are silently denied by the router ACL. Where a partner routes its whole subnet over a VPN mesh (e.g. NetBird), no separate mesh-specific address is needed; the mTLS connection is the security boundary, not the transport underneath it.

---

## Ports

| Port / address | Direction | Purpose |
|---|---|---|
| TCP 7448 | localhost | Local Zenoh router (plaintext, bridges + zenoh-admin only) |
| TCP 7447 TLS | outbound | Remote/fabric Zenoh router (mTLS) |
| UDP 50000 | inbound | Generic UDP ingress — all ASTERIX categories (CAT-34/48 by default) arrive combined on this one port; unknown protocols are preserved without guessing |
| TCP `<TAK_PORT>` (mTLS, default 8089) | outbound | CoT to TAK Server streaming input |
| HTTP 8890 | inbound | zenoh-admin panel (web UI) |
| TCP `<SITAWARE_HQ_NVG_PORT>` (default 8088) | inbound | SitaWare HQ polls the EFDI NVG feed |
| HTTPS | outbound | SitaWare HQ and dronuradaras.lt |

See [03 — Bootstrap & Install](docs/03-bootstrap-and-install.md) for the full network prerequisites table.

---

## Bundle contents

| Component | Status | Notes |
|---|---|---|
| Zenoh router | ✅ | `eclipse/zenoh:1.9.0`, digest-pinned, mTLS, ACL-scoped |
| ASTERIX bridge + translators | ✅ | Mixed UDP demux; CAT-001/002/004/007/008/009/010/011/015/016/017/018/019/020/021/023/025/032/034/048/062/063/065/150/205/240/247 |
| dronuradaras bridge | ✅ | REST polling, acoustic sensors + drone detection |
| Partner ADS-B / CAT-21 input | ✅ | Registered Zenoh topics or ASTERIX CAT-021; no public scraping bridge |
| SAPIENT BSI Flex 335 v2 bridge | ✅ | Sensor/detection ingress |
| CoT output layer (`tak_layer`) | ✅ | TAK Server TCP/mTLS |
| TAK ingress bridge (`tak_bridge`) | ✅ | TAK Server CoT → Zenoh |
| SitaWare HQ NVG feed (`sitaware_layer`) | ✅ | EFDI tracks → HQ NVG Import Subscription |
| NATO NFFI protocol | ✅ | ADatP-36 / STANAG 5527 XML through Zenoh |
| STANAG 4586 / 4607 / 4609 / 5516 | ✅ | UAS VSM/CUCS telemetry, GMTI ground radar, MISB KLV video metadata, Link 16 over JREAP-C — one file, `stanag.py --proto N` |
| Track fusion layer | ✅ | ASTERIX CAT-48 / CAT-21 correlation |
| zenoh-admin panel | ✅ | FastAPI + React — status, config editor, health dashboard, data-volume counters |

---

## Repository layout

```
EFDI/
├── README.md                    this file
├── CONTRIBUTING.md               contribution guide
├── SECURITY.md                   vulnerability reporting policy
├── CLAUDE.md, AGENTS.md         agent / contributor instructions
├── docs/                        numbered operator manual + reference (see docs/00-start-here.md)
│   ├── 00-start-here.md         doc map + where to start
│   ├── 01-14-*.md               architecture, install, config, ATAK/SitaWare, integrations,
│   │                            C2 runbook, adding a sensor, troubleshooting, admin GUI, CI —
│   │                            03-11 and 14 have English + Lithuanian pairs
│   ├── references/              source-and-trust notes per external spec (ASTERIX/SAPIENT/STANAG/TAK/SitaWare)
│   └── superpowers/              (gitignored) design specs, working notes
├── compose/
│   ├── docker-compose.yml       Zenoh router + zenoh-admin containers
│   ├── .env.example             configuration template
│   ├── certs/                   (gitignored) router mTLS certificates
│   ├── state/                   (gitignored) runtime state — logs, pids, Zenoh config
│   ├── control/                 shared helpers + host control-plane (supervisor.py, admin_control.py, presence.py, ...)
│   ├── zenoh-admin/             FastAPI + React admin panel
│   ├── bridges/                 source/socket + C2-ingress bridge scripts
│   ├── protocols/               protocol translators and .proto contracts
│   └── layers/                  C2-egress output layers (tak_layer, sitaware_layer)
├── start.sh                     interactive service launcher
├── stop.sh                      service teardown
└── dev.sh                       disposable local PostgreSQL + API for admin UI preview
```

---

## Deployment

On a fresh Debian 13 (or RHEL/Rocky/Alma) host:

```bash
curl -fsSL https://raw.githubusercontent.com/lk-risb/EFDI/main/install.sh | bash
```

`install.sh` auto-installs every prerequisite (git, Python 3.10+, Docker Engine + Compose, openssl, gettext) if missing, then walks through certs and configuration interactively.

See **[docs/00-start-here.md](docs/00-start-here.md)** for the full operator manual (English/Lithuanian) — prerequisites, certificate setup, configuration reference, and troubleshooting, split into numbered topic docs.

Wondering where a protocol decoder's wire-format knowledge came from and how trustworthy it is? See **[docs/references/](docs/references/README.md)** — per-category source URLs, verification method, and a copy of the actual spec text used.

---

## Operations

New to the admin panel? **[12 — Zenoh Admin GUI](docs/12-zenoh-admin-gui.md)** has a
plain-language, page-by-page walkthrough of every tab.

```bash
./start.sh                            # interactive launcher — pick services, prompts for missing config
./start.sh --check-all                # pre-flight: which services are ready vs blocked (run before a demo)
./tests/c2_preflight.sh               # one-glance C2 readiness — all three TAK/SitaWare legs green
./stop.sh                             # tear down running services
./dev.sh up                           # live zenoh-admin preview — disposable PostgreSQL + API + Vite UI
docker compose logs -f zenoh-router   # follow the router's logs
```

`start.sh` lists all retained infrastructure, bridge, protocol, and output services, restores the previous selection, merges in already-running processes, and auto-starts after a five-second window (press `c` to change the selection). Endpoint addresses are remembered in a mode-600 runtime state file; passwords and API keys are never written there — Runtime Control writes credentials only to the gitignored `compose/.env` (mode 600) when an administrator explicitly saves them.

`supervisor.py` watches host-managed processes and restarts those that **crashed** (pidfile present, process gone) with a 15 s→600 s backoff, while leaving cleanly **stopped** services alone.

The admin panel's **Runtime Control** page can start, stop, restart, and inspect logs for host-managed processes, and reports a per-service *blocked* reason when prerequisites are missing. `start.sh` keeps a localhost `admin-control` process on port 18896; the panel uses it to update `.env` as data and delegate lifecycle actions to the same launcher scripts used from a terminal. Setting `EFDI_CONTROL_TOKEN` in `compose/.env` is recommended; it authenticates both the control API and the Zenoh-router shell helper.

The panel also provides managed-router operation for a hierarchy of HQ and branch routers:

- **Network** shows direct children and observed descendants, link freshness, management authority, and last-known state. A branch keeps its local router, PostgreSQL, WebUI, and child-management capability when its parent is offline — the database's own datadir stays on local disk even if the branch's other pod state lives on shared storage.
- **Zenoh Config** validates a candidate with the pinned Zenoh binary before touching the active file, waits for router health on activation, and restores the last-known-good config automatically if restart or health checks fail.
- **Changes** records local and relayed revisions, target, hash, and final applied/rejected/rolled-back state without storing configuration bodies.
- **Certificate Authority** creates single-use child invitations. The child generates router-CA, transport, and non-CA policy-signer keys locally, submits only CSRs, and receives a cryptographically bounded delegation chain. An optional step-ca intermediate issues and renews short-lived transport certificates without exposing a router-CA key to the WebUI.

Parent-to-descendant changes travel one authenticated hop at a time: each router accepts control only from its configured parent and re-signs a bounded relay for a direct child, so a root router never needs a descendant's private identity or direct endpoint.

---

## EFDI sandbox notes

- **Enrollment** — a NetBird setup-key issued at onboarding. No silent control plane on the sandbox.
- **Zenoh identity** — a per-UUID slot assigned by the portal. Your cert CN, topic prefix, and write namespace all anchor to that slot.
- **Write namespace** — `<slot_id>/**`. Publishes outside this prefix are silently denied by the router ACL.

---

## License

Apache-2.0. See [`LICENSE`](LICENSE).
