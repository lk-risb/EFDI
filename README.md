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
[Giraffe AMB]    ──mixed ASTERIX UDP──► asterix_udp_bridge ──Zenoh/raw──► asterix_cat34/48 ─┐
[ADSB.lol]       ──REST/HTTPS───► adsblol_bridge      ─┤
[Oro navigacija UTM]──authorized JSON/GeoJSON──► utm_ans_bridge ─┤
[dronuradaras.lt]──REST/HTTPS──► dronuradaras_bridge  ─┤
[Drone RID node] ──Zenoh/raw───► opendroneid          ─┤
[AISstream]      ──WSS/AIS─────► aisstream_ws_bridge ─┤
[SitaWare HQ]    ──documented REST API──► sitaware_bridge ─┤──► Zenoh router (local) ──► cot_layer ──► ATAK
[NFFI publisher] ──Zenoh/XML───► nffi                  ─┤                                      ├──► TAK Server
[Link-16]        ──UDP────────► link16                 ─┤                                      ├──► nvg_bridge ◄── SitaWare HQ polls
[MAVLink]        ──UDP/TCP────► mavlink                ─┘              └──► track_fusion_bridge
                                                                      └──► nvg_bridge ◄── SitaWare HQ polls
```

Zenoh-native translators are also included for CAP 1.2 alerts, GeoJSON/OGC
Features, AIS NMEA, RF spectrum observations, sensor health, and mission
routes. Their raw publishers write complete payloads below `raw/**`; the
router host does not need radio or sensor hardware.

Each inbound bridge or protocol publishes tracks to a Zenoh router under a structured topic hierarchy (`{namespace}/{domain}/{source}/{protocol}/{affiliation}/{type}/v1`). Output layers subscribe to wildcard patterns, convert to CoT XML, and deliver downstream.

The live `/v1` data-plane payload is currently JSON. The `.proto` files beside
their translators under `compose/protocols/`
describe intended typed contracts but do not by themselves make a topic
Protobuf. Binary migration must use generated language bindings and a new `/v2`
topic during a dual-publish transition so existing CoT, TAK, NVG, and fusion
consumers continue to decode `/v1` safely.

---

## Architecture

| Service | Role |
|---|---|
| `zenoh-router` | Local pub/sub fabric — mTLS to the fabric, plaintext TCP for local bridges/GUI |
| `asterix_udp_bridge` | Optional mixed-category UDP ingress; validates and demultiplexes complete ASTERIX frames onto raw Zenoh topics |
| `asterix_cat10/20/21/34/48/62` | Independent, edition-scoped ASTERIX input protocols |
| `dronuradaras_bridge` | REST polling, currently-online acoustic sensors + drone detection events; offline markers are actively evicted |
| `adsblol_bridge` | Free/open-data ADS-B API → normalized regional civil/military aircraft tracks |
| `utm_ans_bridge` | Authorized Oro navigacija UTM JSON/GeoJSON export → declared civilian UAV tracks; not a national Remote ID feed |
| `opendroneid` | Raw ASTM/ASD-STAN messages published by receiver/detection nodes → normalized Zenoh UAV tracks; routers require no radio hardware |
| `aisstream_ws_bridge` | Authenticated WSS stream, AIS vessel positions and static data |
| `sitaware_bridge` | SitaWare HQ friendly-force REST polling (inbound) |
| `nffi` | NATO NFFI / ADatP-36 (STANAG 5527) XML translator for raw Zenoh publications |
| `nvg_bridge` | Pull-based NVG 2.0.2 snapshot for SitaWare HQ Import Subscriptions (outbound) |
| `link16` | JREAP-C UDP; TCP requires a verified gateway framing ICD |
| `mavlink` | MAVLink 2 UDP/TCP, including OPEN_DRONE_ID messages |
| `mavlink_raw_bridge` / `link16_jreap_bridge` / `vmf_bridge` / `stanag4586_bridge` | Optional socket ingress only; publishes raw bytes to Zenoh |
| `cap` | CAP 1.2 XML → time-bounded alerts and areas |
| `geojson_features` | GeoJSON/OGC Features → zones and overlays |
| `ais_nmea` | NMEA AIVDM/AIVDO → vessel tracks |
| `spectrum_observation` / `sensor_health` | Vendor-neutral RF and sensor-status translators |
| `mission_route` | GeoJSON/JSON UAV routes and corridors |
| `dji_cloud_api_bridge` | Source-specific DJI Cloud API MQTT 5 aircraft telemetry bridge |
| `cot_layer` | CoT XML output — UDP multicast + TAK Server TCP |
| `track_fusion_bridge` | ASTERIX CAT-48 / CAT-21 correlation |
| `zenoh-admin` | FastAPI + React panel — router status, config, bridge/protocol/layer lifecycle, endpoints, and write-only credentials |

All bridges and protocols are lightweight Python processes with no shared state beyond the Zenoh session. They publish self-describing JSON payloads. The CoT layer translates any incoming Zenoh topic to the appropriate MIL-STD-2525C CoT type using a topic-pattern → CoT-type map — adding a new sensor requires no changes to the output layer.

Native processes connect to `ZENOH_LOCAL_ENDPOINT`, which defaults to
`tcp/127.0.0.1:7448`. They never default to a remote hackathon/backbone router.
Only the local `zenoh-router` owns `ZENOH_FABRIC_ENDPOINT` (or the preferred
multi-link `ZENOH_FABRIC_ENDPOINTS` JSON array), so changing a parent or
federation address does not require modifying or restarting every adapter.

ASTERIX compatibility is edition-specific. CAT-48 is aligned to EUROCONTROL Edition 1.32 and CAT-34 to Edition 1.29. The existing CAT-20, CAT-21, and CAT-62 tables are retained only as legacy compatibility UAPs; they emit startup warnings and must not be used for modern CAT-20 1.9, CAT-21 2.2+, or CAT-62 1.21 streams without implementing the producer's exact edition/ICD. Link-16/JREAP-C field layouts are likewise gateway-profile dependent; UDP is available, while TCP is intentionally disabled until its stream framing is documented.

Client onboarding is certificate-based: each pod gets a signed mTLS bundle issued by EFDI, scoped to its own namespace. Services run as individual host processes managed by PID files in `compose/state/.pids/`. The only containerised components are the Zenoh router and the zenoh-admin panel — this keeps the data path inspectable, restartable in isolation, and free of container networking overhead.

### SitaWare integration (bidirectional)

Four independent paths are available; enable only interfaces documented and licensed in the target deployment. This setup is HQ-focused, so the adapters use disjoint environment-variable prefixes rather than sharing endpoints or credentials.

| Direction | Bridge/layer | Path |
|---|---|---|
| Inbound | `sitaware_bridge.py` | SitaWare **HQ** REST API → EFDI (poll-based) |
| Inbound | `nffi.py` | Raw NATO NFFI / ADatP-36 XML on Zenoh → normalized EFDI tracks |
| Outbound | `nvg_bridge.py` | EFDI tracks → pull-based NVG 2.0.2 feed for SitaWare **HQ** |

**`sitaware_bridge.py` (inbound, HQ)** — polls an explicitly configured `SITAWARE_API_PATH` on a `SITAWARE_POLL_S`-second interval. There is no universal `/rest/v2/units` resource: HQ 6.22 may map a MIP4 servlet at `/rest/v2/*` while returning 404 for that guessed resource. Enable this adapter only after confirming the deployment's resource path, schema, and authentication method in its API/ICD. Primary and fallback base URLs remain supported for deployments that do expose a compatible JSON unit resource.

**`nffi.py` (protocol translator)** — subscribes to complete NFFI XML documents already carried by Zenoh under `…/raw/nffi/{source-id}` and publishes normalized friendly-force tracks. It contains no receiver socket, endpoint, framing, or vendor connection logic. NFFI friendly-force interoperability is specified by ADatP-36 / STANAG 5527; STANAG 4677 is the separate dismounted-soldier interoperability family and requires its own exact JDSSDM/NFFI profile implementation.

**`nvg_bridge.py` (outbound, HQ)** — maintains a bounded snapshot of live EFDI tracks and serves one NVG 2.0.2 document for HQ's **NVG Import Subscription** manager to poll. The endpoint supports GET/HEAD only, requires dedicated HTTP Basic credentials unless anonymous access is explicitly enabled, expires stale tracks from its snapshot, includes an NVG `TimeSpan` on every published object, and attaches standard symbol modifiers plus bounded `ExtendedData`. Its authenticated `/healthz` response records bounded successful/unauthorized pull counters and timestamps without retaining credentials or payloads. NVG data documents do not carry a per-object delete operation, so omission from a later snapshot does not remove an object that HQ already imported. Objects imported before EFDI began publishing `TimeSpan/end` require a one-time replacement of the target NVG layer; see `INSTALL.md`. Its Attributes view reuses the same domain-aware stat-card formatter as CoT/TAK, with clean Identity, IFF, Kinematics, Radar, Flight Plan, Status, Altitude, Guidance, and ADS-B Quality sections instead of raw Python keys. Aircraft include distinct barometric/geometric altitude readings, primary altitude in metres/feet/flight level, climb/descent rate, selected/target altitude, speed/heading, identity, route, emergency/autopilot state, ADS-B quality, and other safe scalar source fields available in the track. SitaWare uses the same RU/BY ICAO and MMSI hostile-affiliation classifiers as CoT rather than assigning every ADS-B/AIS topic one static affiliation. Because HQ 6.22 renders the standards-native METOC scheme as Unknown, weather stations use its supported neutral emplaced-sensor SIDC, distinct from the generic neutral sensor used by dronuradaras.lt. The dronuradaras bridge publishes only devices whose latest API status is `is_online=true`; an offline transition immediately removes that device from this snapshot and from the CoT caches. XML-illegal upstream characters are removed and a malformed cached record cannot break the complete feed. The endpoint refuses non-loopback cleartext HTTP unless the lab-only override is set. Prefer HTTPS with a certificate trusted by the HQ Windows host. This native Python service is enabled with `SITAWARE_HQ_NVG_ENABLE=1`; it does not add a bridge container.

ADS-B emitter categories `C1` and `C2` are surface emergency/service vehicles, not aircraft. CoT and both SitaWare NVG paths classify them as neutral ground vehicles (`a-n-G-E-V` / `SNGPEV----*****`). The ordinary `on_ground` flag alone is not used for this decision because taxiing aircraft must remain aircraft.

SitaWare inbound records are written under this pod's normal
`{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/...` namespace. They therefore become
ordinary Zenoh data for authorized subscribers and federated partner routers;
the Zenoh ACL and federation policy—not the C2 adapter—decide which other pods
may receive them. Enabling an outbound TAK/NVG layer does not enable the reverse
path: select and configure `sitaware` or `sitaware-hq-nvg` separately.

`cot_bridge.py` is the Zenoh-side CoT entrypoint: it stays on the fabric and
delegates to the CoT output layer instead of opening a direct TAK socket.

### C2 activation summary

| Direction | Select | Required runtime contract |
|---|---|---|
| Zenoh → TAK Server | `cot-bridge` | `TAK_HOST/PORT`; add `TAK_TLS=1` and TAK-issued client credentials for mTLS |
| Zenoh → SitaWare HQ | `sitaware-hq-nvg` | Enable the authenticated NVG feed and create an HQ NVG Import Subscription |
| SitaWare HQ → Zenoh | `sitaware` | Deployment-documented REST resource, schema, URL and credentials |

TAK certificates are issued by TAK, not by the Zenoh CA. SitaWare endpoint paths
must come from that deployment's licensed documentation; `/rest/v2/units` is
not assumed. Full commands and verification steps are in
[INSTALL.md](INSTALL.md#c2--zenoh-bidirectional-runbook).

The operator-side setup is included there as well: the HQ **SitaWare
Communication → NVG → NVG Import Subscriptions** fields and the values to enter
in a licensed SitaWare integration. Where Systematic does not publish stable
menu names, the guide explicitly requires the installed release's administration
manual rather than guessing a screen.

### Test personas

The current test exercise supports three distinct operational clients: a C2
operator using the configured CoT output, a sensor publisher that writes complete
protocol data to a raw Zenoh topic, and a fabric admin who manages the EFDI panel
only.
The full client setup and acceptance checks are in
[INSTALL.md](INSTALL.md#8-operational-persona-test-exercise). These personas
are not yet a Zenoh per-client permission boundary: the current router ACL is
namespace-scoped.

---

## Networking

Every pod dials a single fabric endpoint over mutual TLS — the pod writes only within its assigned namespace (`<slot_id>/**`), and publishes outside it are silently denied by the router ACL. Where a partner's deployment routes its whole subnet over a VPN mesh (e.g. NetBird), no separate mesh-specific address is needed; the mTLS connection is the actual security boundary, not the transport underneath it.

---

## Ports

| Port / address | Direction | Purpose |
|---|---|---|
| TCP 7448 | localhost | Local Zenoh router (plaintext, bridges + zenoh-admin only) |
| TCP 7447 TLS | outbound | Remote/fabric Zenoh router (mTLS) |
| UDP 50010 / 50020 / 50021 / 50034 / 50048 / 50062 | inbound | Category-specific ASTERIX listener conventions (`CATNN_PORT`) |
| UDP 50000 | inbound | Optional EFDI mixed ASTERIX ingress convention |
| UDP multicast `239.2.3.1:6969` | outbound | CoT delivery to ATAK |
| UDP `<TAK_UDP_PORT>` (default 8087) | outbound | Optional direct CoT unicast to WinTAK/ATAK |
| HTTP 8890 | inbound | zenoh-admin panel (web UI) |
| TCP `<SITAWARE_HQ_NVG_PORT>` (default 8088) | inbound | SitaWare HQ polls the EFDI NVG feed |
| HTTPS | outbound | SitaWare HQ, dronuradaras.lt, and any explicitly authorized UTM export API |

See [INSTALL.md](INSTALL.md) for the full network prerequisites table.

---

## Bundle contents

| Component | Status | Notes |
|---|---|---|
| Zenoh router | ✅ | `eclipse/zenoh:1.9.0`, digest-pinned, mTLS, ACL-scoped |
| ASTERIX bridge | ✅ | Mixed UDP demultiplexing for configured categories; independent CAT-010/020/021/034/048/062 translators currently normalize supported feeds |
| dronuradaras bridge | ✅ | REST polling, acoustic sensors + drone detection events |
| Oro navigacija UTM bridge | ✅ (endpoint required) | Declared/planned UAV flights from an authorized JSON/GeoJSON export; does not scrape the public map or claim Remote ID |
| ADSB.lol bridge | ✅ | ODbL open aircraft data from the public v2 API |
| SitaWare HQ bridge | ✅ | Friendly-force REST polling (inbound) |
| NATO NFFI protocol | ✅ | ADatP-36 / STANAG 5527 XML published through Zenoh |
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
│   ├── bridges/                  source/socket bridge scripts
│   ├── protocols/                protocol translators and .proto contracts
│   └── layers/                   output layer scripts
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
./dev.sh up              # live zenoh-admin preview — disposable Postgres + API + Vite UI, no router/certs
./dev.sh down             # tear down the dev preview stack and Vite process
docker compose logs -f zenoh-router   # follow the router's logs
```

`start.sh` lists all retained infrastructure, bridge, protocol-adapter, and
output services. It restores the
previous selection, merges in processes already running from `run.sh`, displays
the complete result, and auto-starts it after a five-second window. Press `c`
during that window to change the selection. Endpoint addresses are remembered
in the mode-600 runtime state file; passwords and API keys are never written
there. Runtime Control writes credentials only to the gitignored `compose/.env`
with mode 600 when an administrator explicitly saves them.

The admin panel's **Runtime Control** page can start, stop, restart, and inspect
logs for those host-managed processes. `start.sh` and `run.sh all` keep a
localhost `admin-control` process available on port 18896; the panel uses it to
update the deployment `.env` as data and delegate lifecycle actions to the same
launcher scripts used from a terminal. Secret values are write-only. Setting
`EFDI_CONTROL_TOKEN` is recommended in `compose/.env`; it authenticates both
the localhost control API and the fixed-target Zenoh-router shell helper. If it
is left empty, both sides derive a local token from the required admin secret.

The panel also provides managed-router operation for a hierarchy of HQ and
branch routers:

- **Network** shows direct children and observed descendants, link freshness,
  management authority, and last-known state. A branch keeps its local router,
  database, WebUI, and child-management capability when its parent is offline.
- **Zenoh Config** validates a candidate with the pinned Zenoh binary before it
  touches the active file. Activation waits for router health and restores the
  last-known-good configuration automatically if restart or health checks fail.
  Remote editing starts from the selected router's reported snapshot; identity,
  listener, CA-profile, and control-prefix fields remain local-only, and a
  branch push is rolled back unless a remote router link also recovers.
- **Changes** records local and relayed revisions, target, hash, and final
  applied/rejected/rolled-back state without storing configuration bodies.
- **Certificate Authority** creates single-use child invitations. The child
  generates router-CA, transport, and non-CA policy-signer keys locally,
  submits only CSRs, and receives a cryptographically bounded delegation chain.
  An optional step-ca intermediate issues and renews short-lived transport
  certificates without exposing a router-CA key to the WebUI.

Parent-to-descendant changes travel one authenticated hop at a time: each
router accepts control only from its configured parent and re-signs a bounded
relay for a direct child. A root router therefore does not need every
descendant's private identity or direct endpoint. Signed topology and status
facts carry the public delegation proof, allowing the root to independently
verify grandchildren and deeper descendants after restart.

---

## EFDI sandbox notes

- **Enrollment** — a NetBird setup-key issued at onboarding. No silent control plane on the sandbox.
- **Zenoh identity** — a per-UUID slot assigned by the portal. Your cert CN, topic prefix, and write namespace all anchor to that slot.
- **Write namespace** — `<slot_id>/**`. Publishes outside this prefix are silently denied by the router ACL.

---

## License

Apache-2.0. See [`LICENSE`](LICENSE).
