# 08 — Integrations

## Integrations

> **Want to connect a new sensor?** This page is the reference for what's
> already wired. For the step-by-step "how do I add one" walkthrough, see
> [Adding a New Sensor or Protocol](10-adding-a-sensor.md) below.

EFDI separates source-specific collectors, reusable protocol translators, and
TAK/SitaWare output layers:

- `compose/bridges/` polls or connects to a named product/service.
- `compose/protocols/` contains one independently launched wire/API protocol per
  file. ASTERIX categories are separate because their UAPs and editions differ.
- `compose/layers/` connects normalized Zenoh data to TAK/CoT and SitaWare/NVG.

Once an inbound script publishes a normalized topic, the running CoT and NVG
layers subscribe automatically. Receiver and detection systems normally attach
to a nearby Zenoh router and publish there; the router relays their data. Most
router hosts therefore need no receiver hardware or vendor driver.

### Protocol connection requirements

ASTERIX category numbers do not define TCP or UDP port numbers. The radar or
surveillance gateway management interface must be configured with the EFDI
host as its destination and with the same transport/port selected below. EFDI
uses UDP 50034 for CAT-034 and UDP 50048 for CAT-048 as deterministic local
conventions; these are not EUROCONTROL or Saab defaults. UDP 50000 is the
generic raw ingress. `udp_ingress_bridge.py` preserves every datagram and safely
publishes complete ASTERIX frames unchanged to `…/raw/asterix/catNN`; every
category translator remains a separate process and subscribes only to its own
topic. `ASTERIX_CATEGORIES` selects which categories are auto-dispatched.
Dedicated UDP/TCP inputs remain active at the same time.

When the radar-side laptop already publishes complete frames through another
Zenoh router, set `ASTERIX_ZENOH_UPSTREAM_ENDPOINT` and optionally
`ASTERIX_ZENOH_UPSTREAM_ROOT`. `asterix_bridge.py` subscribes to every
`…/raw/asterix/catN` topic at that router, verifies that the topic category and
ASTERIX header agree, and republishes the unchanged frame locally. The same
category translators then decode it; the bridge itself does not interpret a
UAP. Plaintext `tcp/...:7448` is for isolated testing only.

The mixed bridge also supports `ASTERIX_BIND`, `ASTERIX_MULTICAST_GROUP`,
`ASTERIX_MULTICAST_INTERFACE`, and an IPv4/CIDR `ASTERIX_ALLOW_SOURCE` filter.
Before configuring a new feed, observe it without Zenoh publication:

```bash
python3 tools/asterix_probe.py --port 30001
```

The probe reports sender IP, destination port, category, first-FRN SAC/SIC when
present, frame counts, and rate. For a multicast feed, add `--multicast-group`
and `--multicast-interface`.

`asterix_probe.py` lives in `tools/`, alongside `asterix_relay.py`, rather than
in `compose/` — nothing in `tools/` is imported by the pod, started by
`start.sh`/`run.sh`, or built into any image. These are standalone operator
utilities run by hand, usually from a laptop or the PC wired to a radar, while
commissioning a feed: `compose/bridges/` and `compose/protocols/` are the
running data plane, `tools/` is field tooling. `asterix_relay.py` forwards
ASTERIX UDP datagrams unchanged, byte-for-byte, from a local port to a remote
`IP:PORT` — run it on the machine connected to the radar when that machine is
on the NetBird mesh but the radar itself is not routable from the pod:

```bash
python3 tools/asterix_relay.py --dest 100.x.y.z:30048
```

`tests/test_asterix_raw_pipeline.py` exercises the framing logic these tools
share with the decoders, so changes here are caught by the normal test run.

| Protocol script | Transport role | Required partner/runtime configuration | Current contract |
|---|---|---|---|
| `vendors/asterix/cat.py --category 1` | UDP listener or TCP server | Producer sends to `CAT1_PORT`; set `CAT1_RADAR_LAT/LON` to georeference polar/cartesian-only plots/tracks | EUROCONTROL CAT-001 Ed.1.4 monoradar plot/track reports (legacy, superseded by CAT-048 for most modern radars) |
| `vendors/asterix/cat.py --category 2` | UDP listener or TCP server | Producer sends to `CAT2_PORT` | EUROCONTROL CAT-002 Ed.1.2 monoradar service messages (north marker, sector crossing, station status) |
| `vendors/asterix/cat.py --category 4` | UDP listener or TCP server | Producer sends to `CAT4_PORT` | EUROCONTROL CAT-004 Ed.1.13 safety net alerts (STCA/MSAW/APW/RIMCA/...) |
| `vendors/asterix/cat.py --category 7` | UDP listener or TCP server | Producer sends to `CAT7_PORT`; set `CAT7_RADAR_LAT/LON` to georeference polar/cartesian-only reports | EUROCONTROL CAT-007 Ed.1.12 directed interrogation messages (military Mode 4/5/S interrogation control; downlink and uplink UAPs) |
| `vendors/asterix/cat.py --category 8` | UDP listener or TCP server | Producer sends to `CAT8_PORT` | EUROCONTROL CAT-008 Ed.1.3 monoradar derived weather information (weather-image vectors/contours) |
| `vendors/asterix/cat.py --category 9` | UDP listener or TCP server | Producer sends to `CAT9_PORT` | EUROCONTROL CAT-009 Ed.2.1 composite weather reports (merged multi-radar weather picture) |
| `vendors/asterix/cat.py --category 10` | UDP listener or TCP server | Producer sends to `CAT10_PORT`; set airport reference coordinates if reports use only local X/Y or polar positions | EUROCONTROL CAT-010 Ed.1.1, airport surface targets/status |
| `vendors/asterix/cat.py --category 11` | UDP listener or TCP server | Producer sends to `CAT11_PORT`; set `CAT11_SITE_LAT/LON` to georeference cartesian-only reports | EUROCONTROL CAT-011 Ed.1.3 A-SMGCS system tracks (fused airport-surface aircraft + vehicles with flight-plan correlation) |
| `vendors/asterix/cat.py --category 15` | UDP listener or TCP server | Producer sends to `CAT15_PORT`; set `CAT15_SITE_LAT/LON` to georeference range/azimuth-only reports | EUROCONTROL CAT-015 Ed.1.2 independent non-cooperative surveillance (passive/multi-static) target reports |
| `vendors/asterix/cat.py --category 16` | UDP listener or TCP server | Producer sends to `CAT16_PORT` | EUROCONTROL CAT-016 Ed.1.0 independent non-cooperative surveillance system configuration reports (the INCS ground system's own site position/transmitter/receiver config, sister status category to CAT-015) |
| `vendors/asterix/cat.py --category 17` | UDP listener or TCP server | Producer sends to `CAT17_PORT` | EUROCONTROL CAT-017 Ed.1.3 Mode S Surveillance Coordination Function messages (legacy inter-radar cluster/hand-over protocol; "Track Data" messages carry a position, network-management messages don't) |
| `vendors/asterix/cat.py --category 18` | UDP listener or TCP server | Producer sends to `CAT18_PORT`; set `CAT18_SITE_LAT/LON` to georeference the local polar/cartesian-only position items | EUROCONTROL CAT-018 Ed.1.8 Mode S Datalink Function messages (GDLP/interrogator uplink-downlink coordination: aircraft reports, uplink packet/broadcast/GICB-extraction requests and acknowledgements) |
| `vendors/asterix/cat.py --category 19` | UDP listener or TCP server | Producer sends to `CAT19_PORT` | EUROCONTROL CAT-019 Ed.1.3 MLT system status |
| `vendors/asterix/cat.py --category 20` | UDP listener or TCP server | Producer sends to `CAT20_PORT` and confirms Edition 1.11 | EUROCONTROL CAT-020 Ed.1.11 MLAT reports |
| `vendors/asterix/cat.py --category 21` | UDP listener or TCP server | ADS-B gateway sends to `CAT21_PORT` and confirms Edition 2.7 | EUROCONTROL CAT-021 Ed.2.7 ADS-B reports |
| `vendors/asterix/cat.py --category 23` | UDP listener or TCP server | Producer sends to `CAT23_PORT` | EUROCONTROL CAT-023 Ed.1.3 CNS/ATM ground station service messages (ADS-B/TIS-B/FIS-B/GRAS/MLT station status) |
| `vendors/asterix/cat.py --category 25` | UDP listener or TCP server | Producer sends to `CAT25_PORT` | EUROCONTROL CAT-025 Ed.1.6 CNS/ATM ground system status reports (successor/companion to CAT-023: split system/service status, per-component status list, service statistics, site position) |
| `vendors/asterix/cat.py --category 32` | UDP listener or TCP server | Producer sends to `CAT32_PORT` | EUROCONTROL CAT-032 Ed.1.2 Miniplan Reports to an SDPS (FPPS/SDPS flight-plan-to-track-number correlation; no position field exists in this category) |
| `vendors/asterix/cat.py --category 34` | UDP listener or TCP server | Radar sends CAT-034 alone to `CAT34_PORT` (EFDI convention: UDP 50034) | EUROCONTROL CAT-034 Ed.1.29 radar service messages |
| `vendors/asterix/cat.py --category 48` | UDP listener or TCP server | Radar sends CAT-048 alone to `CAT48_PORT` (EFDI convention: UDP 50048); local polar positions require `CAT48_RADAR_LAT/LON` | EUROCONTROL CAT-048 Ed.1.32 targets |
| `vendors/asterix/cat.py --category 62` | TCP client or UDP listener | Set `CAT62_HOST/PORT`, or `CAT62_UDP=1`; confirm Edition 1.21 | EUROCONTROL CAT-062 Ed.1.21 system tracks |
| `vendors/asterix/cat.py --category 63` | UDP listener or TCP server | Producer sends to `CAT63_PORT` | EUROCONTROL CAT-063 Ed.1.7 sensor status reports (the sensors feeding a CAT-062 tracker) |
| `vendors/asterix/cat.py --category 65` | UDP listener or TCP server | Producer sends to `CAT65_PORT` | EUROCONTROL CAT-065 Ed.1.6 SDPS service status reports (the SDPS-side companion to CAT-062, same relationship CAT-019 has to CAT-020) |
| `vendors/asterix/cat.py --category 150` | UDP listener or TCP server | Producer sends to `CAT150_PORT` | EUROCONTROL CAT-150 Ed.3.0 MADAP Plan Server Flight Data Message (Maastricht UAC legacy flight-plan distribution/correlation/conflict data; no position field in this edition) |
| `vendors/asterix/cat.py --category 205` | UDP listener or TCP server | Producer sends to `CAT205_PORT`; set `CAT205_SITE_LAT/LON` to georeference cartesian-only reports | EUROCONTROL CAT-205 Ed.1.0 Radio Direction Finder reports (RDF network triangulating a radio transmitter's position, typically an aircraft's VHF radio) |
| `vendors/asterix/cat.py --category 240` | UDP listener or TCP server | Producer sends to `CAT240_PORT` | EUROCONTROL CAT-240 Ed.1.3 Radar Video Transmission (raw pre-plot-extraction signal-level video, not a target report; messages can carry up to ~64KB of video data) |
| `vendors/asterix/cat.py --category 247` | UDP listener or TCP server | Producer sends to `CAT247_PORT` | EUROCONTROL CAT-247 Ed.1.3 Version Number Exchange (a source reports which edition of each ASTERIX category it transmits) |
| `vendors/sapient/flex335.py` | TCP listener or client | Edge node connects to `SAPIENT_LISTEN_PORT`, or set middleware `SAPIENT_HOST/PORT`; remote listeners require an allowed source CIDR | BSI FLEX 335 v2 framing and public SAPIENT protobuf subset |
| `nffi.py` | Zenoh subscriber/translator | Publisher writes one complete XML document under `…/raw/nffi/{source-id}` | NATO NFFI / ADatP-36 (STANAG 5527) XML subset |
| `vendors/stanag/stanag.py --proto 4586` | TCP client | Set CUCS/VSM `STANAG4586_HOST/PORT`; validate the VSM ICD before selecting `STANAG4586_PROFILE=legacy_ed3_approx` | Historical deployment layout, disabled by default; not claimed as a generic STANAG 4586 decoder |
| `vendors/stanag/stanag.py --proto 4607` | Zenoh raw subscriber | A bridge places complete packets on `…/raw/stanag_4607/**`; the STANAG defines the message, not the bearer | NATO GMTI (Ground Moving Target Indicator) Format — Mission/Dwell/Job Definition/Platform Location segments, one track per Target Report |
| `vendors/stanag/stanag.py --proto 4609` | SRT/KLV input | Set `STANAG4609_SRT_URL` for the motion-imagery metadata stream | MISB ST 0601 KLV local-set subset over STANAG 4609 motion imagery; SRT is the configured transport, not part of the KLV schema |
| `vendors/stanag/stanag.py --proto 5516` | UDP listener | Set `STANAG5516_PORT` (default 3010); gateway sends JREAP-C-encapsulated Link 16 J-series | MIL-STD-6016F / STANAG 5516 Ed.5 J2.2/J2.5/J3.2/J3.5/J3.7 subset over JREAP-C (MIL-STD-3011) |

`stanag.py` merges all four STANAG variants EFDI speaks into one file (decode
and, where applicable, encode together) — `--proto {4586,4607,4609,5516}` selects
which one a given process runs; see `proto/stanag.proto` for the wire
message shapes.

All twenty-seven ASTERIX translators also accept `--zenoh-raw` (or their corresponding
`CATNN_ZENOH_RAW=1`) for an exact complete frame on `…/raw/asterix/catNN`.
Launchers select that mode automatically for categories listed in
`ASTERIX_CATEGORIES` whenever generic UDP ingress or the upstream Zenoh
ASTERIX bridge is configured.

VERA-NG passive sensors that provide CAT-34 and CAT-48 use this same raw
ASTERIX path; they do not need a VERA-specific bridge. Give every reporting
source a unique SAC/SIC pair and leave `CAT34_RADAR_NAME` blank when Giraffe
and VERA sources share one feed, so their site, status, coverage, and target
state remain independently identified as `RADAR SACx/SICy`. Prefer the live
CAT-34 I034/120 site position and I034/100 coverage values. Before operational
use, capture representative frames and confirm the producer's CAT-34/CAT-48
editions and UAP against the configured Ed.1.29/Ed.1.32 decoders. A passive
sensor must not be given a synthetic rotating sweep: EFDI only renders sweep
motion when the source actually sends the applicable CAT-34 timing messages.

ASTERIX is a bit-level surveillance exchange family; category and edition must
match the producer. EUROCONTROL publishes CAT-010 for surface movement,
CAT-021 for ADS-B target reports, CAT-062 for system tracks, and CAT-240 for raw
radar video. CAT-240 is not a map-track feed and needs radar-video processing
before TAK/SitaWare publication. See the [EUROCONTROL ASTERIX catalogue](https://www.eurocontrol.int/asterix),
[CAT-010 Ed.1.1 specification](https://www.eurocontrol.int/sites/default/files/service/content/documents/nm/asterix/cat010-asterix-monoradar-surface-movement-data-part-7.pdf),
[CAT-021](https://www.eurocontrol.int/publication/cat021-eurocontrol-specification-surveillance-data-exchange-asterix-part-12-category-21),
[CAT-062](https://www.eurocontrol.int/publication/cat062-eurocontrol-specification-surveillance-data-exchange-asterix-part-9-category-062),
and [CAT-240](https://www.eurocontrol.int/publication/cat240-eurocontrol-specification-surveillance-data-exchange-asterix).

### Egress topic views: `/sapient`, `/json`, `/proto`, `/raw`

Every decoded track is published on one object key with four sibling views.
They carry the same event at different fidelity, so a consumer subscribes to
exactly one view and ignores the rest. Nothing is implicit — a consumer reading
a key always knows what the bytes are.

```
{prefix}/{pod}/{domain}/{source}/{modality}/{affiliation}/{entity}/{type}/{id}/{view}
```

| View | Topic suffix | Zenoh encoding | Payload |
|---|---|---|---|
| SAPIENT | `…/{id}/sapient` | `application/protobuf` | BSI Flex 335 v2 `SapientMessage`. **The fabric contract.** |
| JSON | `…/{id}/json` | `application/json` | Flat JSON object. Only the fields the decoder models. |
| Protobuf | `…/{id}/proto` | `application/protobuf` | Typed message from the protocol's `.proto` (`compose/protocols/`). |
| Raw | `…/{id}/raw` | `application/protobuf` | `RawEnvelope` wrapping the **original wire bytes**, unmodified. |

**Which view to use.** Default to `/sapient`: it is the agreed contract for data
leaving the fabric. Use `/proto` when you need full per-protocol sensor detail
SAPIENT does not model, and `/raw` when you need a field EFDI does not decode at
all, or want to run the vendor's own decoder over the exact bytes. `/json` is
for humans and for consumers that cannot link a protobuf runtime.

**Native payloads are byte-exact.** They are re-wrapped, never re-encoded:

- ASTERIX — one standalone data block per record: the CAT byte and 2-byte
  length header are re-added, so any off-the-shelf ASTERIX decoder reads it.
- SAPIENT — the original BSI Flex 335 v2 `SapientMessage`, with the 32-bit
  length prefix already stripped.
- STANAG 4609 — the raw MISB KLV packet.

The `RawEnvelope` (`../compose/protocols/proto/raw_envelope.proto`) carries
`protocol`, `profile` (e.g. `cat048`, `misb-st0601`), `content_type`, and the
`payload` bytes.

**Fidelity caveat.** `/sapient`, `/json` and `/proto` are only as complete as
the decoder. When a value cannot be represented in the target contract it is
dropped from that view and a `protobuf encode failed …` line is logged — one
view failing never blocks the others. `/raw` is the only view that can never
lose a field.

All four views sit under the pod's first-party publish prefix, so the existing
`${DATA_TOPIC_ROOT}/**` router ACL already covers them — adding a view needs no
ACL change.

#### External catalog compatibility

Some upstream portals distinguish the certificate-backed slot from a
human-readable vendor alias. Treat the authenticated `whoami`/identity response
as authoritative: the alias is display metadata and must not replace the
certificate slot in Zenoh keys unless the upstream ACL explicitly says so.

The trial portal registry accepts exact topic keys, not Zenoh `*` or `**`
expressions. High-rate collection publishers therefore keep object identity in
the payload and use stable sibling keys:

```text
.../aircraft/tracks/v1          JSON
.../aircraft/sapient/tracks/v1  BSI FLEX 335 v2 SAPIENT protobuf
.../aircraft/proto/tracks/v1    source-specific protobuf
```

`publish_collection()` enforces one encoding per exact key for ADS-B and fused
aircraft collections. Per-object topics remain useful inside fabrics whose
catalog supports patterns, but must not be the only output when the external
catalog requires exact registration.

### Vendored third-party schemas

`compose/protocols/vendors/sapient/sapient_msg/` carries the BSI Flex 335 v2.0 (SAPIENT) `.proto`
schemas verbatim from [github.com/dstl/SAPIENT-Proto-Files](https://github.com/dstl/SAPIENT-Proto-Files)
(`bsi_flex_335_v2_0/`) — **do not edit these files**; they are upstream
contracts, and a local edit would silently diverge EFDI's wire format from the
standard it claims to speak. Re-vendor from upstream instead. Licensed Apache
License 2.0 (see `sapient_msg/LICENCE.txt`, which permits use, modification,
and redistribution including commercial/defence use as long as the licence and
copyright notices are retained); the British Standards Institution retains
ownership and copyright of BSI Flex 335, with publication rights held by BSI
Standards Ltd.

It lives under `compose/protocols/vendors/sapient/` rather than directly under
`compose/protocols/proto/` because that directory holds EFDI's *own*
contracts, while this is someone else's — it carries its own package
(`sapient_msg.bsi_flex_335_v2_0`) and internal import paths of the form
`sapient_msg/bsi_flex_335_v2_0/<file>.proto` that only resolve if this
directory is its own protoc include root, so `scripts/generate-protobuf.sh`
passes it as a second `-I` root alongside `-I compose`. EFDI both reads and
writes SAPIENT: `compose/protocols/vendors/sapient/flex335.py` decodes
incoming SAPIENT with a hand-written protobuf reader (field numbers verified
against these files) and encodes outbound tracks into a real
`SapientMessage`/`DetectionReport` in the same file, so a consumer needs to
understand only SAPIENT rather than every source protocol.

### Source-specific bridges

| Bridge | Endpoint behavior | Configuration needed |
|---|---|---|
| Generic UDP | Preserves every datagram and safely auto-dispatches complete ASTERIX frames | `UDP_INGRESS_PORT`, optional bind/multicast/source filter, and ASTERIX dispatch categories |
| dronuradaras.lt | Polls its fixed public HTTPS API | None |
| meteo.lt | Polls the fixed public HTTPS API | Optional places/rate |
| SitaWare HQ REST inbound | Polls deployment-specific resource | URL, credentials, and the real `SITAWARE_API_PATH`; there is no universal units URL |
| Track fusion | Subscribes to local Zenoh topics | No external endpoint; starts working when normalized tracks arrive |

#### Radar operator UDP relay

Copy `scripts/radar_udp_relay.py` to the radar operator's Windows computer.
It has no third-party dependencies. If the radar sends UDP to local port
50048, for example, run:

```powershell
py .\radar_udp_relay.py --listen-port 50048
```

The relay forwards every datagram unchanged to `asusrog.efdi.ltu:50000`.
Override `--destination-host` when mesh DNS is unavailable. Configure this
router with `UDP_INGRESS_PORT=50000`. The generic receiver preserves every
datagram on its raw Zenoh topic and only auto-dispatches protocols whose framing
is unambiguous. UDP 50034 and 50048 remain separate deterministic CAT-034 and
CAT-048 listeners.

On the EFDI laptop, inspect traffic without taking ownership of the UDP socket:

```bash
./scripts/capture-radar-udp.sh
./scripts/capture-radar-udp.sh any giraffe-50000.pcap
```

The first command displays packet bytes; the second saves a full packet capture
for offline decoder work. Both use tcpdump and can run while the generic UDP
ingress is bound to port 50000.

### Output layers

| Layer | Automatic input | External configuration |
|---|---|---|
| CoT/TAK output | Subscribes to matching normalized Zenoh topics | TAK TCP/mTLS host, or ATAK/WinTAK UDP destination |
| CoT receiver | Converts an attached TAK or SitaWare CoT stream into Zenoh | Listen port or remote host; TAK uses TAK-issued mTLS credentials |
| SitaWare HQ NVG | Maintains an automatic normalized-track snapshot | HQ is configured to poll the EFDI URL; TLS and dedicated credentials are required outside an isolated lab |

### C2 to Zenoh and back

Output and input are separate services. Enabling a TAK or SitaWare output does
not silently enable the reverse path.

#### TAK Server

For Zenoh → TAK, configure `TAK_HOST/TAK_PORT` and select `tak_layer`
(`layers/tak_layer.py`). It subscribes to normalized Zenoh topics and emits CoT
two ways: UDP multicast to `239.2.3.1:6969` for LAN ATAK clients, and TCP/mTLS to
a TAK Server. TAK-issued client credentials are required when `TAK_TLS=1`. For
TAK → Zenoh, select `tak-bridge` (`bridges/tak_bridge.py`), which normalizes an
inbound CoT stream back onto the fabric. Prefer a stable DNS `TAK_HOST`; if the
TAK server certificate has a different legacy DNS SAN, set
`TAK_TLS_SERVER_NAME` to that SAN so hostname verification remains enabled.

#### SitaWare

For Zenoh → SitaWare HQ, select `sitaware_layer` (`layers/sitaware_layer.py`) and configure
an HQ NVG Import Subscription to poll the authenticated NVG 2.0.2 feed it serves.

For SitaWare HQ → Zenoh, obtain the real REST resource from the deployment ICD:

```dotenv
SITAWARE_URL=https://sitaware.example
SITAWARE_USER=<runtime-user>
SITAWARE_PASS=<runtime-secret>
SITAWARE_API_PATH=/<documented-resource>
SITAWARE_TLS_VERIFY=1
```

Select `sitaware`; it publishes normalized units below
`…/{domain}/sitaware/c2/{affiliation}/{entity}/{type}/{id}/sapient`.

The current runtime keeps the SitaWare HQ REST and NVG paths separate. If the
deployment exports NFFI instead, publish complete NFFI XML documents under
`…/raw/nffi/{source-id}` and run the independent `nffi` translator.

All resulting records stay in the producing pod's namespace. Authorized
federation routes may relay that namespace to other partner routers, whose TAK
and SitaWare output layers consume the normalized topics automatically. An
adapter must never write directly into another partner's namespace.

Operator-side configuration is documented step-by-step in the
[C2 ↔ Zenoh bidirectional runbook](09-c2-zenoh-runbook.md). In brief,
TAK Server requires a dedicated client identity, the correct IN/OUT groups and
a TAK-issued certificate; SitaWare HQ NVG input is created under **SitaWare
Communication → NVG → NVG Import Subscriptions**; and a licensed SitaWare CoT
Gateway must be given one TCP role, the EFDI endpoint, an approved export-layer
set, and an explicit exclusion for `EFDI Live Tracks`. Product screens not
present in the installed license/release cannot be substituted with a guessed
REST path.

### Client SDKs — connecting to the pod (`clients/`)

This section is for the people **consuming** a pod: publishing data to the
EFDI fabric and receiving data from it, in their own language and tooling —
partners integrating against your pod, not sensors/protocols wired into it
(that's the rest of this document). The code lives in `clients/`:

```text
clients/
├── connect/             minimal "cert bundle -> Zenoh session" helper per language
├── examples/
│   ├── modern/          idiomatic pub/sub/request-reply per language
│   ├── military-legacy/ older toolchains, offline/air-gapped, file/HTTP fallbacks
│   └── bridges/         use a protocol you already speak — no Zenoh code in your app
└── README.md
```

| You are… | Use |
|---|---|
| A modern dev (Python/TS/Go/Rust/Java/C++) | `examples/modern/<lang>/` |
| On an older / less-common stack (C, Java 8, .NET Framework, MATLAB) | `examples/military-legacy/` |
| Speaking a protocol you already have (HTTP, files) — no Zenoh code | `examples/bridges/` |
| Just want the minimal connect snippet | `connect/<lang>/` |

#### The model in 30 seconds

The pod runs a **Zenoh router**; a client talks to it as a **Zenoh client over
mTLS**. Three operations, that's the whole API:

1. **Publish** (`put`) to keys under **your namespace** — e.g. `release/<you>/sensors/temp`.
2. **Subscribe** (`sub`) to keys you're allowed to read — your own, plus
   `release/<partner>/**` for data a partner sends you (bilateral relationships).
3. **Query** (`get`) for the latest/historical value of a key (optional).

Keys are slash-paths (`a/b/c`); subscriptions use `*` (one segment) and `**` (any depth).

Every example reads the same five things from **environment variables**, so
credentials are never hardcoded:

| Env var | What it is | Example |
|---|---|---|
| `EFDI_ROUTER` | the pod's Zenoh endpoint | `tls/127.0.0.1:7447` (pod on your box) |
| `EFDI_CERT` | your mTLS client certificate (PEM) | `/etc/efdi/mycert.pem` |
| `EFDI_KEY` | your mTLS private key (PEM) | `/etc/efdi/mykey.pem` |
| `EFDI_CA` | the CA root that signs the router (PEM) | `/etc/efdi/ca-root.pem` |
| `PARTNER_NAMESPACE` | the prefix you own (publish under this) | `release/acme` |

`scripts/gen-certs.sh <namespace>` writes these into `compose/certs/`
(`<namespace>-cert.pem`, `<namespace>-key.pem`, `efdi-ca-root.pem`); for a
downstream consumer the EFDI administrator hands over a copy of the same three
files out-of-band. If the pod is on the consumer's own machine, `EFDI_ROUTER`
is `tls/127.0.0.1:7447`; over the mesh, it's that host's mesh IP.

Targets **Zenoh 1.9.0** everywhere (the fleet-pinned version — see
`compose/docker-compose.yml`); use the matching-major client library
(`eclipse-zenoh`/`zenoh-c`/`zenoh-cpp`/`zenoh-go`/`zenoh-java`/`zenoh`
crate/`zenoh-ts`, all 1.x).

#### The one connection gotcha (every native binding hits this)

Zenoh's TLS config must be inserted as **one whole block** at
`transport/link/tls`, with **`enable_mtls: true`**. Setting the sub-keys one
at a time (`transport/link/tls/connect_certificate`, etc.) silently does
**not** turn on the client-cert send path on Zenoh 1.x — the session opens but
the router rejects the client, or it connects read-only. Every `connect/`
helper builds the *entire* block (`root_ca_certificate` / `connect_certificate`
/ `connect_private_key` / `enable_mtls` / `verify_name_on_connect`) as one
document and applies it in a single call — the language-specific mechanism
differs (`zc_config_from_str` in C, `Config::from_str` in C++,
`InsertJson5("transport/link/tls", …)` in Go, `Config.fromJson5` in Java,
`insert_json5(...)` in Rust, one `conf.insert_json5("transport/link/tls", …)`
in Python) but the rule is the same everywhere.

Also: when the router cert's SAN binds an **IP/mesh address** rather than the
DNS name being dialed, set `verify_name_on_connect`/`EFDI_VERIFY_NAME` to
`false` (the pod's local router at `127.0.0.1` needs this; a DNS-named remote
router keeps it `true`).

#### Bridges — talk to the pod in a protocol you already speak

A **bridge** is a small process that is itself a Zenoh mTLS client but exposes
a **different protocol** to the consumer's application — HTTP, a watched
directory. The application never links a Zenoh library and contains no Zenoh
code; it speaks the protocol it already knows, and the bridge does the Zenoh
part. This is the path for legacy/defense shops that cannot or will not link
`eclipse-zenoh`: MATLAB, PLCs, old .NET Framework, Java 8, air-gapped systems —
anything that can make an HTTP request or write a file.

| Use a **native client** (`connect/` + `examples/modern/`) | Use a **bridge** |
|---|---|
| Can link a Zenoh client (Python/Go/Rust/Java/C++) | Can't link one (toolchain, policy, certification) |
| Want lowest latency, full pub/sub/query | Want zero Zenoh code in the app |
| Modern language, controls the build | MATLAB / PLC / old .NET / air-gapped / file-only |
| Long-lived in-process subscriptions | "fire an HTTP call" or "drop a file" is all there is |

A bridge holds the consumer's mTLS client identity, so its plaintext side
(HTTP, a watched directory) is an unauthenticated door into the fabric — run
it **co-located with the consuming app, bound to `127.0.0.1` only**. If the
app is on another host, put the bridge next to *that* app instead, pointed at
the pod's mesh IP — the trust boundary moves to the bridge↔pod link (still
mTLS), but the plaintext side must never be exposed to an untrusted network.
Both bridges below are stdlib + `eclipse-zenoh` only (no web framework, no
file-watcher library) and ship an optional `Dockerfile` to run as a compose
sidecar.

**`bridges/file-drop/`** — exchange data as files in a directory: the most
universal path, for MATLAB, PLCs/SCADA, legacy .NET, shell pipelines, and
fully air-gapped edges. A file written under `OUTBOX_DIR` is published under a
key formed from its path relative to the outbox (`OUTBOX_DIR/sensors/temp` →
`<namespace>/sensors/temp`); the file then moves to `OUTBOX_DIR/.sent/`.
Inbound samples matching `SUB_KEYEXPR` are written into `INBOX_DIR` as files
named by their key (slashes → `__`) plus a millisecond timestamp, written
atomically (temp name, then rename) so a poller never reads a half-written
file. Poll-based, stdlib only (no inotify dependency); tune `POLL_SECONDS`
(default 1s); leave `SUB_KEYEXPR` empty to disable the inbound half.

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
export OUTBOX_DIR=./outbox INBOX_DIR=./inbox SUB_KEYEXPR='release/<partner>/**'
python3 bridge.py
```

**`bridges/rest-http/`** — plain HTTP, for `curl`, MATLAB (`webwrite`), old
.NET (`HttpClient`), Java 8 (`HttpURLConnection`), shell scripts, or a PLC's
HTTP block. Binds `127.0.0.1` only by default (`BRIDGE_BIND`/`BRIDGE_PORT`).

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
python3 bridge.py                 # serves on http://127.0.0.1:8080

curl -X POST http://127.0.0.1:8080/pub/sensors/temp -d '21.5'                 # publish
curl 'http://127.0.0.1:8080/sub/sensors/temp?count=3'                          # receive N (blocks)
curl -N http://127.0.0.1:8080/stream/sensors/temp                              # SSE stream
WEBHOOK_URL=https://my-system.local/ingest WEBHOOK_KEYEXPR='release/<partner>/**' python3 bridge.py  # outbound webhook
```

A bare path (`sensors/temp`) is scoped under the caller's namespace; a full
key the caller has read rights to (e.g. `release/<partner>/...`) passes
through as-is. Received text comes back as `"text"` in the JSON response, or
`"b64"` if the bytes aren't valid UTF-8.

#### Military / legacy / less-common stacks (`examples/military-legacy/`)

For pinned JDK 8, .NET Framework 4.x, MATLAB, C89/C99, and **air-gapped**
shops that can't reach the internet, can't `pip install`, and often can't link
a native Zenoh client at all. Work top to bottom, stop at the first row that's true:

| If… | Use | Why |
|---|---|---|
| A native Zenoh binding builds and links (have `zenoh-c`, a C compiler, policy allows it) | **native** — `c99/` | lowest latency, full pub/sub/query, no extra process |
| Can make an **HTTP request** (any language) | **REST bridge** — `bridges/rest-http/` + the `java8/`, `dotnet-framework/`, `matlab/` examples | zero Zenoh code; works on any stack with an HTTP client |
| Can only **read/write files** (locked-down box, SCADA/PLC, shell pipeline) | **file-drop bridge** — `bridges/file-drop/` + `matlab/receive_filedrop.m` | the most universal path — if it can write a file, it can publish |

```text
legacy app  ──HTTP / files──▶  bridge (localhost, holds mTLS)  ──Zenoh mTLS──▶  pod router
```

The Java/.NET/MATLAB examples are **not** Zenoh clients — ~80-line programs
using nothing but the language's stdlib against the local bridge; they compile
with tools already on the box (`javac`, `csc.exe`, MATLAB's editor), no Maven,
NuGet, or Gradle.

**Offline / air-gapped, once, for all four stacks:**

1. Get the pieces in over sneakernet: the pod itself (handed over by the
   operator), the mTLS cert bundle (`mtls.cert.pem`, `mtls.key.pem`,
   `ca-roots.pem`, namespace), and — only for a bridge's Python runtime — a
   vendored `eclipse-zenoh` wheelhouse:
   ```sh
   # on a CONNECTED machine matching the air-gapped box's OS/arch/python:
   pip download eclipse-zenoh==1.9.0 -d zenoh-wheelhouse/
   # carry zenoh-wheelhouse/ across, then on the AIR-GAPPED box:
   pip install --no-index --find-links zenoh-wheelhouse/ eclipse-zenoh==1.9.0
   ```
   The legacy app itself needs nothing vendored — that's the point of going
   through a bridge.
2. Everything is localhost: the pod, the bridge, and the app all run on one
   box. No DNS, no proxy, no internet; the only hop is bridge→pod
   (`tls/127.0.0.1:7447`).
3. **Clock sync is the silent killer.** mTLS rejects certificates whose
   validity window doesn't contain *now*. A box with a dead RTC or no NTP will
   drift, and the **bridge's** session to the pod fails handshake with a
   confusing "certificate not yet valid/expired" — even though the
   app→bridge HTTP call looks fine. Symptom: the bridge logs a TLS error on
   startup and never prints `bridge on http://…`. Fix the clock first
   (`date`; `sudo date -s '2026-06-02 14:30:00'` on Linux, `w32tm /resync` or
   manual on Windows) before debugging anything else — one fix covers pod +
   bridge since they share the box.

**`military-legacy/c99/`** — pure C99, just libc + `libzenohc` + a `Makefile`
(no CMake required for the examples themselves, though building `zenoh-c`
needs it), against [`zenoh-c`](https://github.com/eclipse-zenoh/zenoh-c)
1.9.0 directly (no bridge — this is a native client). Each example is a
single self-contained `.c` with the connect logic inlined. Requires
`zenoh-c` built with `-DZENOHC_BUILD_WITH_UNSTABLE_API=ON` (the
`zc_config_from_str` entry point the one-block mTLS config needs is gated
behind the unstable API) — built from source, from a prebuilt GitHub Releases
artifact, or fully vendored (`cargo vendor`) for offline builds.

```sh
make                        # dynamic link
make static                 # static-link libzenohc.a -> single self-contained binary
                             # (macOS static linking also needs -framework Security -framework CoreFoundation)
./publish                   # one JSON sample; ./publish 50 200 for 50 samples, 200ms apart
./subscribe                 # everything under your namespace; ./subscribe 'release/<partner>/**'
```

**`military-legacy/java8/`** — JDK 8 (the modern `zenoh-java` binding needs
JDK 17+), via the REST bridge over `java.net.HttpURLConnection` only. No
Maven/Gradle/jars.

```sh
javac Publish.java Subscribe.java
java Publish sensors/temp '{"temp_c":21.5}'
java Subscribe sensors/temp stream          # follow continuously
```

**`military-legacy/dotnet-framework/`** — .NET Framework 4.x (4.5–4.8, not
modern .NET), via the REST bridge over `System.Net.HttpWebRequest` (present
since Framework 2.0; more predictable than `HttpClient` for the open-ended SSE
stream). Build with `csc.exe` directly, or the included classic (non-SDK)
`.csproj` via `msbuild` — neither touches NuGet.

```bat
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /out:EfdiBridgeClient.exe Program.cs
EfdiBridgeClient.exe pub sensors/temp {"temp_c":21.5}
EfdiBridgeClient.exe stream sensors/temp
```

**`military-legacy/matlab/`** — MATLAB via `webwrite`/`webread` (REST bridge,
`publish.m`/`receive_rest.m`) or plain file I/O (file-drop bridge,
`receive_filedrop.m`) for the most locked-down boxes — no toolbox, no MEX, no
network call at all in the file-drop path.

```matlab
publish('sensors/temp', '{"temp_c":21.5}')
s = receive_rest('sensors/temp', 'Count', 5, 'TimeoutSec', 60);
receive_filedrop('./inbox', 'Callback', @(key,bytes) disp(key))
```

#### Modern language bindings (`examples/modern/`)

Idiomatic pub/sub/request-reply per language, each pairing the official Zenoh
binding with a small `connect/<lang>/` helper that applies the one-block mTLS
config above. All target Zenoh 1.9.0; if a symbol doesn't resolve on a
different pinned minor, re-check that tag's own upstream examples — that's
true for every language below and isn't repeated per-entry.

**`modern/python/`** — official `eclipse-zenoh`. `pip install eclipse-zenoh`,
then `python3 publish.py` / `python3 subscribe.py` / `python3 request_reply.py
{serve,get}`. On Windows use `python` (not `python3`) inside the venv or it
hits the system interpreter and raises `ModuleNotFoundError`.

**`modern/cpp/`** — official [`zenoh-cpp`](https://github.com/eclipse-zenoh/zenoh-cpp),
a **header-only wrapper over `zenoh-c`** — install `zenoh-c` 1.9.0 first
(unstable API on), then `zenoh-cpp` 1.9.0, then CMake-build the examples.
`find_package(zenohcxx)` failing means zenoh-cpp isn't on
`CMAKE_PREFIX_PATH`; linking failing on `libzenohc` means zenoh-c isn't
installed — neither is an API problem.

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build
./build/publish; ./build/subscribe
```

**`modern/go/`** — the official binding (landed with Zenoh 1.9.x
"Longwang", April 2026) is a **cgo wrapper over `zenoh-c`**, not pure Go —
install `zenoh-c` 1.9.0 first (unstable API on), `CGO_ENABLED=1` required,
cross-compiling is awkward. Import path is
`github.com/eclipse-zenoh/zenoh-go/zenoh` (the old top-level/`zenoh-net`
0.4.x API is abandoned since 2020 — do not use it). No pure-Go/no-cgo option
exists today; use a bridge instead if that's a hard requirement.

```sh
go run publish.go; go run subscribe.go 'release/<partner>/**'
```

**`modern/java/`** — official `zenoh-java` (JDK 17+ — a Kotlin/JVM binding).
Unlike Go, the published `zenoh-java-jvm` artifact **bundles the native
library as a JAR resource**, so a normal Gradle dependency
(`org.eclipse.zenoh:zenoh-java-jvm:1.9.0`) is enough — no separate `zenoh-c`
install. Generate the wrapper once with `gradle wrapper`, then:

```sh
./gradlew run -Pmain=Publish
./gradlew run -Pmain=Subscribe --args="release/<partner>/**"
```

**`modern/rust/`** — the official [`zenoh`](https://crates.io/crates/zenoh)
crate — **pure Rust** (the reference implementation, no C library to
install), async (tokio). `cargo build` pulls `zenoh =1.9.0` + tokio.

```sh
cargo run --bin publish; cargo run --bin subscribe -- 'release/<partner>/**'
```

**`modern/typescript/`** — official `@eclipse-zenoh/zenoh-ts`. **This one is
architecturally different from the rest:** it does not open a direct Zenoh
session over mTLS. It talks to a `zenoh-plugin-remote-api` loaded inside
`zenohd` over **WebSocket** (`ws://`/`wss://`); `Config` takes only a locator
string, with no client-cert/TLS block on the TS side at all. The mesh-side
mTLS is configured in the **pod's `zenohd`**, on the links the router itself
makes — the plugin is the trust boundary between the WebSocket client and the
mesh. The pod operator must enable the plugin (`plugins.remote_api.websocket_port`
in the `zenohd` config; not on by default); front it with `wss://` for
anything but loopback. If a typed native API isn't specifically needed, the
REST/WebSocket bridge above is often the simpler path for Node/browser
consumers.

Env vars differ from the native bindings: `EFDI_WS` (preferred; e.g.
`ws/127.0.0.1:10000`) or `EFDI_ROUTER` as a fallback (host reused with port
`10000`); `EFDI_CERT`/`EFDI_KEY`/`EFDI_CA` are unused unless the plugin is
fronted with `wss://` on a private CA, in which case `NODE_EXTRA_CA_CERTS`
(Node) trusts it (browsers need the CA in the OS/browser trust store).

zenoh-ts targets browser/Deno first; under **Node** it needs a global
`WebSocket`, shimmed via the `ws` package (Node 22+ ships one natively, making
the shim a no-op; Node 18/20 need it) and loaded before the example via
`tsx`. Deno needs no polyfill (`deno run --allow-net --allow-env --allow-read
subscribe.ts`) and is the upstream-blessed runtime if Node shimming proves
fragile. Key resolution goes through a **WASM** module — ensure the bundler/
runtime can load `.wasm` (tsx/Deno handle this by default).

```sh
npm install
npm run publish; npm run subscribe -- 'release/<partner>/**'
```

### Next protocol candidates

| Priority | Protocol | Use | Gate before implementation |
|---|---|---|---|
| High | ONVIF Profile M | Camera analytics objects, metadata, geolocation, and events | Device profile, discovery/auth method, sample metadata stream. [ONVIF Profile M](https://www.onvif.org/profiles/profile-m/) |
| Medium | VITA 49.2 | Raw RF/spectrum observations | DSP/geolocation stage that converts samples into map-ready bearings/positions. [VITA Radio Transport](https://www.vita.com/page-1855484) |
| Medium | STANAG 4607 / 4676 | GMTI and NATO track exchange | Licensed ICD/profile and representative messages; do not infer layouts |
| Vendor-specific | Acoustic/RF counter-UAS API | Bearings, classifications, tracks, sensor health | Vendor ICD/API schema, coordinate frame, time base, lifecycle, and authentication |

SAPIENT is the preferred public, vendor-neutral counter-UAS sensor interface:
the MOD-owned architecture is standardized as BSI FLEX 335 and publishes its
protobuf schemas. See the [official SAPIENT guidance](https://www.gov.uk/guidance/sapient-autonomous-sensor-system)
and [Dstl schemas](https://github.com/dstl/SAPIENT-Proto-Files). Its TCP
framing is the four-byte little-endian protobuf length used by the
[official BSI FLEX 335 v2 test harness](https://github.com/dstl/BSI-Flex-335-v2-Test-Harness/blob/main/SAPIENTMessageProcessor/ByteDataMessageBuilder.cs).

### Hackathon partner intake checklist

Before connecting a feed, obtain:

- protocol, category, edition/profile, transport, and stream framing;
- producer IP/port or URL/broker plus who initiates the connection;
- authentication/TLS method without committing any credentials;
- representative messages or a sanitized PCAP covering create/update/delete;
- coordinate reference, origin/datum, altitude reference, angles, and units;
- timestamps/time zone, update rate, stable identifiers, and stale/delete rules;
- classification/affiliation semantics and confidence scale;
- expected maximum message size, object count, and rate.

If any of category/edition, framing, or coordinate reference is unknown, the
translator must reject or quarantine the feed rather than silently guess.

---
