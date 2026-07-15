<div align="center">

# EFDI

**Partner-custodial sensor bridge stack — tactical sensors → Zenoh pub/sub → ATAK CoT / SitaWare, on your own hardware, under your own custody.**

[![Zenoh](https://img.shields.io/badge/Zenoh-1.9.0-blue)](https://zenoh.io/)
[![Python](https://img.shields.io/badge/Python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker%20Compose-2496ED?logo=docker&logoColor=white)](https://docs.docker.com/compose/)
[![ATAK](https://img.shields.io/badge/ATAK-CoT-4c9a4c)](https://tak.gov/)
[![License](https://img.shields.io/badge/License-Apache--2.0-blue)](https://www.apache.org/licenses/LICENSE-2.0)

[![shellcheck](https://github.com/risblicencijos/EFDI/actions/workflows/shellcheck.yml/badge.svg)](https://github.com/risblicencijos/EFDI/actions/workflows/shellcheck.yml)
[![compose-validate](https://github.com/risblicencijos/EFDI/actions/workflows/compose-validate.yml/badge.svg)](https://github.com/risblicencijos/EFDI/actions/workflows/compose-validate.yml)
[![bridge-syntax](https://github.com/risblicencijos/EFDI/actions/workflows/bridge-syntax.yml/badge.svg)](https://github.com/risblicencijos/EFDI/actions/workflows/bridge-syntax.yml)
[![zenoh-admin-frontend](https://github.com/risblicencijos/EFDI/actions/workflows/zenoh-admin-frontend.yml/badge.svg)](https://github.com/risblicencijos/EFDI/actions/workflows/zenoh-admin-frontend.yml)
[![docker-build](https://github.com/risblicencijos/EFDI/actions/workflows/docker-build.yml/badge.svg)](https://github.com/risblicencijos/EFDI/actions/workflows/docker-build.yml)

</div>

The **EFDI pod** ingests data from tactical sensors (radars, drone detection networks, datalinks, UAV telemetry), routes everything through a local Zenoh publish/subscribe fabric, and delivers it to ATAK as Cursor-on-Target (CoT) over UDP multicast or TAK Server TCP.

This repository is the EFDI-partner collaboration surface. It carries **no partner-internal infrastructure, credentials, or private links** — everything here is safe to develop against openly.

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
[AISstream]      ──WSS/AIS─────► aisstream_ws_bridge ─┤
[SitaWare HQ]    ──documented REST API──► sitaware_bridge ─┤──► Zenoh router (local) ──► cot_layer ──► ATAK
[NFFI source]    ──TCP/XML─────► nato_nffi_layer      ─┤                                      ├──► TAK Server
[Link-16]        ──UDP────────► link16_bridge         ─┤                                      ├──► nato_nvg_layer ──► SitaWare Edge
[MAVLink]        ──UDP/TCP────► mavlink_bridge        ─┘              └──► track_fusion_layer
                                                                      └──► sitaware_hq_nvg_feed ◄── SitaWare HQ polls
```

Each bridge publishes tracks to a Zenoh router under a structured topic hierarchy (`{namespace}/{domain}/{source}/{protocol}/{affiliation}/{type}/v1`). Output layers subscribe to wildcard patterns, convert to CoT XML, and deliver downstream.

The live `/v1` data-plane payload is currently JSON. The files under `proto/`
describe intended typed contracts but do not by themselves make a topic
Protobuf. Binary migration must use generated language bindings and a new `/v2`
topic during a dual-publish transition so existing CoT, TAK, NVG, and fusion
consumers continue to decode `/v1` safely.

---

## Architecture

| Service | Role |
|---|---|
| `zenoh-router` | Local pub/sub fabric — mTLS to the fabric, plaintext TCP for local bridges/GUI |
| `asterix_bridge` | ASTERIX CAT-48 (tracks) + CAT-34 (radar status), Giraffe AMB |
| `dronuradaras_bridge` | REST polling, currently-online acoustic sensors + drone detection events; offline markers are actively evicted |
| `aisstream_ws_bridge` | Authenticated WSS stream, AIS vessel positions and static data |
| `sitaware_bridge` | SitaWare HQ friendly-force REST polling (inbound) |
| `nato_nffi_layer` | NATO NFFI (STANAG 4677) friendly-force XML feed (inbound) |
| `nato_nvg_layer` | EFDI tracks → SitaWare Edge NVG v2 push (outbound) |
| `sitaware_hq_nvg_feed` | Pull-based NVG 2.0.2 snapshot for SitaWare HQ Import Subscriptions (outbound) |
| `cot_receiver_bridge` | Inbound plain CoT or TAK Server mTLS stream; can import loop-protected TAK ground-user SA positions |
| `link16_bridge` | JREAP-C UDP; TCP requires a verified gateway framing ICD |
| `mavlink_bridge` | MAVLink 2 UDP/TCP |
| `cot_layer` | CoT XML output — UDP multicast + TAK Server TCP |
| `track_fusion_layer` | ASTERIX CAT-48 / CAT-21 correlation |
| `zenoh-admin` | FastAPI + React panel — router status, config editing, service health |

All bridges are lightweight Python processes with no shared state beyond the Zenoh session. They publish self-describing JSON payloads. The CoT layer translates any incoming Zenoh topic to the appropriate MIL-STD-2525C CoT type using a topic-pattern → CoT-type map — adding a new sensor requires no changes to the output layer.

ASTERIX compatibility is edition-specific. CAT-48 is aligned to EUROCONTROL Edition 1.32 and CAT-34 to Edition 1.29. The existing CAT-20, CAT-21, and CAT-62 tables are retained only as legacy compatibility UAPs; they emit startup warnings and must not be used for modern CAT-20 1.9, CAT-21 2.2+, or CAT-62 1.21 streams without implementing the producer's exact edition/ICD. Link-16/JREAP-C field layouts are likewise gateway-profile dependent; UDP is available, while TCP is intentionally disabled until its stream framing is documented.

Client onboarding is certificate-based: each pod gets a signed mTLS bundle issued by EFDI, scoped to its own namespace. Services run as individual host processes managed by PID files in `compose/state/.pids/`. The only containerised components are the Zenoh router and the zenoh-admin panel — this keeps the data path inspectable, restartable in isolation, and free of container networking overhead.

### SitaWare integration (bidirectional)

Five independent paths are available; enable only interfaces documented and licensed in the target deployment. HQ, Frontline, and Edge expose different deployment-dependent interfaces, so the adapters use disjoint environment-variable prefixes rather than sharing endpoints or credentials.

| Direction | Bridge/layer | Path |
|---|---|---|
| Inbound | `sitaware_bridge.py` | SitaWare **HQ** REST API → EFDI (poll-based) |
| Inbound | `nato_nffi_layer.py` | NATO NFFI (STANAG 4677) XML feed → EFDI (streaming TCP) |
| Inbound | `cot_receiver_bridge.py` (`sitaware-cot-rx`) | Licensed SitaWare Edge/Frontline CoT Gateway → EFDI → TAK |
| Outbound | `nato_nvg_layer.py` | EFDI tracks → SitaWare **Edge** NVG v2 REST API (push) |
| Outbound | `sitaware_hq_nvg_feed.py` | EFDI tracks → pull-based NVG 2.0.2 feed for SitaWare **HQ** |

**`sitaware_bridge.py` (inbound, HQ)** — polls an explicitly configured `SITAWARE_API_PATH` on a `SITAWARE_POLL_S`-second interval. There is no universal `/rest/v2/units` resource: HQ 6.22 may map a MIP4 servlet at `/rest/v2/*` while returning 404 for that guessed resource. Enable this adapter only after confirming the deployment's resource path, schema, and authentication method in its API/ICD. Primary and fallback base URLs remain supported for deployments that do expose a compatible JSON unit resource.

**`nato_nffi_layer.py` (inbound, NFFI)** — a TCP client that connects to an external STANAG 4677 friendly-force server and publishes received XML records into EFDI. It supports 4-byte big-endian length-prefixed (`--framing length`, default) or newline-delimited XML documents (`--framing newline`); the source operator must confirm the endpoint, protocol profile, and framing.

**`sitaware-cot-rx` (inbound, Edge/Frontline)** — runs a second native `cot_receiver_bridge.py` instance for the SitaWare CoT Gateway, independently of the TAK Server receiver. Configure either `SITAWARE_COT_RX_PORT` for a SitaWare-initiated TCP connection or `SITAWARE_COT_RX_HOST` for an EFDI-initiated connection. Safe source CoT types are preserved end-to-end, so a Frontline tank/armoured-vehicle type reaches ATAK/WinTAK as that same military symbol rather than a generic point. Configure the SitaWare gateway to export the desired own-force/vehicle layers and exclude the EFDI Live Tracks source to prevent an NVG→SitaWare→CoT echo loop. If the deployment exposes NFFI instead of a licensed CoT Gateway, use the existing NFFI adapter for friendly-force positions.

**`nato_nvg_layer.py` (outbound, Edge)** — pushes every EFDI track update as a NATO Vector Graphics (NVG) 2.0 item via `PUT` to the SitaWare Edge REST API, so any SitaWare Frontline client connected to that Edge server sees EFDI tracks live. Each item carries position, speed, course, and a SIDC-derived symbol; deleted/stale tracks are removed with a corresponding NVG delete call rather than left stale on the Edge map.

**`sitaware_hq_nvg_feed.py` (outbound, HQ)** — maintains a bounded snapshot of live EFDI tracks and serves one NVG 2.0.2 document for HQ's **NVG Import Subscription** manager to poll. The endpoint supports GET/HEAD only, requires dedicated HTTP Basic credentials unless anonymous access is explicitly enabled, expires stale tracks, includes an NVG `TimeSpan` so HQ also hides expired objects if the feed goes offline, and attaches standard symbol modifiers plus bounded `ExtendedData`. Its Attributes view reuses the same domain-aware stat-card formatter as CoT/TAK, with clean Identity, IFF, Kinematics, Radar, Flight Plan, Status, Altitude, Guidance, and ADS-B Quality sections instead of raw Python keys. Aircraft include distinct barometric/geometric altitude readings, primary altitude in metres/feet/flight level, climb/descent rate, selected/target altitude, speed/heading, identity, route, emergency/autopilot state, ADS-B quality, and other safe scalar source fields available in the track. SitaWare uses the same RU/BY ICAO and MMSI hostile-affiliation classifiers as CoT rather than assigning every ADS-B/AIS topic one static affiliation. Because HQ 6.22 renders the standards-native METOC scheme as Unknown, weather stations use its supported neutral emplaced-sensor SIDC, distinct from the generic neutral sensor used by dronuradaras.lt. The dronuradaras bridge publishes only devices whose latest API status is `is_online=true`; an offline transition immediately removes that device from this snapshot and from the CoT/Edge caches. XML-illegal upstream characters are removed and a malformed cached record cannot break the complete feed. The endpoint refuses non-loopback cleartext HTTP unless the lab-only override is set. Prefer HTTPS with a certificate trusted by the HQ Windows host. This native Python service is enabled with `SITAWARE_HQ_NVG_ENABLE=1`; it does not add a bridge container.

ADS-B emitter categories `C1` and `C2` are surface emergency/service vehicles, not aircraft. CoT and both SitaWare NVG paths classify them as neutral ground vehicles (`a-n-G-E-V` / `SNGPEV----*****`). The ordinary `on_ground` flag alone is not used for this decision because taxiing aircraft must remain aircraft.

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
| UDP `<TAK_UDP_PORT>` (default 8087) | outbound | Optional direct CoT unicast to WinTAK/ATAK |
| HTTP 8890 | inbound | zenoh-admin panel (web UI) |
| TCP `<NFFI_PORT>` (default 7010) | inbound | NATO NFFI friendly-force feed |
| TCP `<SITAWARE_HQ_NVG_PORT>` (default 8088) | inbound | SitaWare HQ polls the EFDI NVG feed |
| HTTPS | outbound | SitaWare HQ/Edge and dronuradaras.lt APIs |

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
| SitaWare HQ NVG feed | ✅ | EFDI tracks → HQ NVG Import Subscription (outbound pull feed) |
| Link-16 bridge | ✅ | JREAP-C UDP |
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

`start.sh` lists all 28 retained infrastructure, bridge, protocol-adapter, and
output services. It restores the
previous selection, merges in processes already running from `run.sh`, displays
the complete result, and auto-starts it after a five-second window. Press `c`
during that window to change the selection. Endpoint addresses are remembered
in the mode-600 runtime state file; passwords and API keys are never saved.

---

## EFDI sandbox notes

- **Enrollment** — a NetBird setup-key issued at onboarding. No silent control plane on the sandbox.
- **Zenoh identity** — a per-UUID slot assigned by the portal. Your cert CN, topic prefix, and write namespace all anchor to that slot.
- **Write namespace** — `<slot_id>/**`. Publishes outside this prefix are silently denied by the router ACL.

---

## License

Apache-2.0. See [`LICENSE`](LICENSE).
