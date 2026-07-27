# EFDI integration matrix

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

## Protocol connection requirements

ASTERIX category numbers do not define TCP or UDP port numbers. The radar or
surveillance gateway management interface must be configured with the EFDI
host as its destination and with the same transport/port selected below. EFDI
uses UDP 50034 for CAT-034 and UDP 50048 for CAT-048 as deterministic local
conventions; these are not EUROCONTROL or Saab defaults. If a producer combines
multiple categories on one stream, set `ASTERIX_PORT` (EFDI convention: UDP
50000). `asterix_udp_bridge.py` validates each complete frame and publishes it
unchanged to `…/raw/asterix/catNN`; every category translator remains a separate
process and subscribes only to its own topic. `ASTERIX_CATEGORIES` selects which
categories enter this path. Unlisted categories can still use independent
UDP/TCP inputs.

The mixed bridge also supports `ASTERIX_BIND`, `ASTERIX_MULTICAST_GROUP`,
`ASTERIX_MULTICAST_INTERFACE`, and an IPv4/CIDR `ASTERIX_ALLOW_SOURCE` filter.
Before configuring a new feed, observe it without Zenoh publication:

```bash
python3 tools/asterix_probe.py --port 30001
```

The probe reports sender IP, destination port, category, first-FRN SAC/SIC when
present, frame counts, and rate. For a multicast feed, add `--multicast-group`
and `--multicast-interface`.

| Protocol script | Transport role | Required partner/runtime configuration | Current contract |
|---|---|---|---|
| `vendors/asterix/cat.py --category 10` | UDP listener or TCP server | Producer sends to `CAT10_PORT`; set airport reference coordinates if reports use only local X/Y or polar positions | EUROCONTROL CAT-010 Ed.1.1, airport surface targets/status |
| `vendors/asterix/cat.py --category 20` | UDP listener or TCP server | Producer sends to `CAT20_PORT` and confirms Edition 1.11 | EUROCONTROL CAT-020 Ed.1.11 MLAT reports |
| `vendors/asterix/cat.py --category 21` | UDP listener or TCP server | ADS-B gateway sends to `CAT21_PORT` and confirms Edition 2.7 | EUROCONTROL CAT-021 Ed.2.7 ADS-B reports |
| `vendors/asterix/cat.py --category 34` | UDP listener or TCP server | Radar sends CAT-034 alone to `CAT34_PORT` (EFDI convention: UDP 50034) | EUROCONTROL CAT-034 Ed.1.29 radar service messages |
| `vendors/asterix/cat.py --category 48` | UDP listener or TCP server | Radar sends CAT-048 alone to `CAT48_PORT` (EFDI convention: UDP 50048); local polar positions require `CAT48_RADAR_LAT/LON` | EUROCONTROL CAT-048 Ed.1.32 targets |
| `vendors/asterix/cat.py --category 62` | TCP client or UDP listener | Set `CAT62_HOST/PORT`, or `CAT62_UDP=1`; confirm Edition 1.21 | EUROCONTROL CAT-062 Ed.1.21 system tracks |
| `mavlink.py` | UDP listener or TCP server | Autopilot, GCS, or receiver sends MAVLink to `MAVLINK_PORT` | MAVLink 1/2 position plus MAVLink `OPEN_DRONE_ID_*` aggregation |
| `opendroneid.py` | Zenoh subscriber/translator | Receiver publishes an exact 25-byte message or bounded Message Pack under `…/raw/opendroneid/{receiver}/{transmitter}` | ASTM F3411 / ASD-STAN Basic ID, Location, Auth, Self ID, System, and Operator ID |
| `vendors/sapient/flex335.py` | TCP listener or client | Edge node connects to `SAPIENT_LISTEN_PORT`, or set middleware `SAPIENT_HOST/PORT`; remote listeners require an allowed source CIDR | BSI FLEX 335 v2 framing and public SAPIENT protobuf subset |
| `5516.py` | UDP listener | A Link 16/JREAP-C gateway sends its documented UDP profile to `STANAG5516_PORT` | Gateway data only; no direct radio interface and no guessed TCP framing |
| `vmf.py` | UDP listener or TCP server | VMF gateway sends to `VMF_PORT` and confirms MIL-STD-47001C profile | Implemented message subset |
| `nffi.py` | Zenoh subscriber/translator | Publisher writes one complete XML document under `…/raw/nffi/{source-id}` | NATO NFFI / ADatP-36 (STANAG 5527) XML subset |
| `vendors/stanag/4586.py` | TCP client | Set CUCS/VSM `STANAG4586_HOST/PORT`; validate the VSM ICD before selecting `STANAG4586_PROFILE=legacy_ed3_approx` | Historical deployment layout, disabled by default; not claimed as a generic STANAG 4586 decoder |
| `vendors/stanag/4609.py` | SRT/KLV input | Set `STANAG4609_SRT_URL` for the motion-imagery metadata stream | MISB ST 0601 KLV local-set subset over STANAG 4609 motion imagery; SRT is the configured transport, not part of the KLV schema |

All six ASTERIX translators also accept `--zenoh-raw` (or their corresponding
`CATNN_ZENOH_RAW=1`) for an exact complete frame on `…/raw/asterix/catNN`.
Launchers select that mode automatically for categories listed in
`ASTERIX_CATEGORIES` whenever `ASTERIX_PORT` is configured.

ASTERIX is a bit-level surveillance exchange family; category and edition must
match the producer. EUROCONTROL publishes CAT-010 for surface movement,
CAT-021 for ADS-B target reports, CAT-062 for system tracks, and CAT-240 for raw
radar video. CAT-240 is not a map-track feed and needs radar-video processing
before TAK/SitaWare publication. See the [EUROCONTROL ASTERIX catalogue](https://www.eurocontrol.int/asterix),
[CAT-010 Ed.1.1 specification](https://www.eurocontrol.int/sites/default/files/service/content/documents/nm/asterix/cat010-asterix-monoradar-surface-movement-data-part-7.pdf),
[CAT-021](https://www.eurocontrol.int/publication/cat021-eurocontrol-specification-surveillance-data-exchange-asterix-part-12-category-21),
[CAT-062](https://www.eurocontrol.int/publication/cat062-eurocontrol-specification-surveillance-data-exchange-asterix-part-9-category-062),
and [CAT-240](https://www.eurocontrol.int/publication/cat240-eurocontrol-specification-surveillance-data-exchange-asterix).

## Egress topic views: `/sapient`, `/json`, `/proto`, `/raw`

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

The `RawEnvelope` (`compose/protocols/random/raw_envelope.proto`) carries
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

### External catalog compatibility

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

## Source-specific bridges

| Bridge | Endpoint behavior | Configuration needed |
|---|---|---|
| Mixed ASTERIX UDP | Receives one unicast or multicast UDP stream and publishes exact frames by category | `ASTERIX_PORT`, categories, and optional bind/multicast/source filter |
| Airplanes.live | Polls its fixed HTTPS API | None; optional runtime bounds/rate |
| ADSB.lol | Polls its free/open-data v2 point API; source code BSD-3-Clause, public data ODbL 1.0 | None for public API; coordinate production use or feed data if required by the operator |
| APRS-IS | Connects to APRS-IS | Runtime area/range; upstream connection is built in |
| dronuradaras.lt | Polls its fixed public HTTPS API | None |
| Oro navigacija UTM (`utm.ans.lt`) | Polls an explicitly authorized JSON/GeoJSON export or API | `UTM_ANS_API_URL`; optional bearer `UTM_ANS_API_TOKEN` |
| DJI Cloud API | Connects to a deployment's DJI MQTT 5 broker and selects aircraft OSD messages | `DJI_MQTT_HOST`, broker credentials/TLS, and DJI Pilot 2 or Dock enrollment |
| meteo.lt | Polls the fixed public HTTPS API | Optional places/rate |
| SitaWare HQ REST inbound | Polls deployment-specific resource | URL, credentials, and the real `SITAWARE_API_PATH`; there is no universal units URL |
| Track fusion | Subscribes to local Zenoh topics | No external endpoint; starts working when normalized tracks arrive |

### Oro navigacija UTM and Lithuanian civilian UAV data

The public [`utm.ans.lt` map](https://utm.ans.lt/) is a U-space/UTM user
interface for declared flight planning and coordination. Oro navigacija's
[UAV/UTM information](https://www.ans.lt/en/services/unmanned-aerial-vehicles-drones)
and [UTM user rules](https://www.ans.lt/en/services/unmanned-aerial-vehicles-drones/utm-system-user-rules)
do not
advertise an anonymous JSON endpoint or a national live ASTM F3411/OpenDroneID
feed. The bridge therefore refuses to guess private web endpoints or scrape map
HTML. It starts only when `UTM_ANS_API_URL` points to a JSON/GeoJSON export/API
provided by Oro navigacija or an authorized institutional integration:

```dotenv
UTM_ANS_API_URL=https://approved.example/utm/export
UTM_ANS_API_TOKEN=<runtime-only-token>
UTM_ANS_POLL_S=15
UTM_ANS_TLS_VERIFY=1
```

These records are marked `source_kind=declared_utm_flight` and
`utm_remote_id_observed=false`. For actual broadcast Remote ID, attach a
receiver/detection node and publish ASTM F3411/OpenDroneID messages to Zenoh;
the `protocols/random/opendroneid.py` translator handles that path. Other lawful
options are an institutional U-space/CIS or network-identification service
agreement with Oro navigacija, an approved DroneAware API account, or a
contracted airport detection system. None is a guaranteed free public feed.

There is no documented self-service API-token screen. Request integration
access from Oro navigacija at [bepilociai@ans.lt](mailto:bepilociai@ans.lt) or
[info@ans.lt](mailto:info@ans.lt), explaining that you need an authorized
machine-to-machine JSON/GeoJSON feed for a Zenoh-to-TAK/SitaWare integration.
Ask them to confirm the endpoint, authentication/token procedure, TLS trust
chain, schema, permitted fields, polling/rate limits, and redistribution terms.
The published Common Information Service is a regulated service with a 2026
listed fee, so commercial/institutional access may require an agreement rather
than a free token.

## Output layers

| Layer | Automatic input | External configuration |
|---|---|---|
| CoT/TAK output | Subscribes to matching normalized Zenoh topics | TAK TCP/mTLS host, or ATAK/WinTAK UDP destination |
| CoT receiver | Converts an attached TAK or SitaWare CoT stream into Zenoh | Listen port or remote host; TAK uses TAK-issued mTLS credentials |
| SitaWare HQ NVG | Maintains an automatic normalized-track snapshot | HQ is configured to poll the EFDI URL; TLS and dedicated credentials are required outside an isolated lab |

## C2 to Zenoh and back

Output and input are separate services. Enabling a TAK or SitaWare output does
not silently enable the reverse path.

### TAK Server

For Zenoh → TAK, configure `TAK_HOST/TAK_PORT` and select `cot_layer`
(`layers/cot_layer.py`). It subscribes to normalized Zenoh topics and emits CoT
two ways: UDP multicast to `239.2.3.1:6969` for LAN ATAK clients, and TCP/mTLS to
a TAK Server. TAK-issued client credentials are required when `TAK_TLS=1`. For
TAK → Zenoh, select `tak-bridge` (`bridges/tak_bridge.py`), which normalizes an
inbound CoT stream back onto the fabric.

### SitaWare

For Zenoh → SitaWare HQ, select `nvg_layer` (`layers/nvg_layer.py`) and configure
an HQ NVG Import Subscription to poll the authenticated NVG 2.0.2 feed it serves.
For SitaWare HQ → Zenoh over NVG, select `nvg_bridge` (`bridges/nvg_bridge.py`),
which polls an HQ NVG Export Endpoint and normalizes it onto the fabric.

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
[bidirectional runbook](INSTALL.md#c2--zenoh-bidirectional-runbook). In brief,
TAK Server requires a dedicated client identity, the correct IN/OUT groups and
a TAK-issued certificate; SitaWare HQ NVG input is created under **SitaWare
Communication → NVG → NVG Import Subscriptions**; and a licensed SitaWare CoT
Gateway must be given one TCP role, the EFDI endpoint, an approved export-layer
set, and an explicit exclusion for `EFDI Live Tracks`. Product screens not
present in the installed license/release cannot be substituted with a guessed
REST path.

## DJI and Mavic reality

DJI aircraft are not generic MAVLink publishers. DJI's Cloud API uses HTTPS,
WebSocket, and MQTT, with Pilot 2 or a Dock acting as the gateway. DJI currently
lists Mavic 3 Enterprise models among supported Cloud API products and explicitly
excludes several older enterprise models. See DJI's [Cloud API architecture](https://developer.dji.com/doc/cloud-api-tutorial/en/overview/product-architecture.html),
[MQTT model](https://developer.dji.com/doc/cloud-api-tutorial/en/overview/basic-concept/mqtt.html),
[supported products](https://developer.dji.com/doc/cloud-api-tutorial/en/overview/product-support.html),
and [aircraft OSD fields](https://developer.dji.com/doc/cloud-api-tutorial/en/api-reference/pilot-to-cloud/mqtt/aircraft/properties.html).

The Cloud API bridge remains available for a deployment that already operates
its own Pilot 2/Dock broker. It is independent of the OpenDroneID feed below.
MSDK is intentionally not an EFDI runtime path: DJI defines it as a library in a
mobile application connected through the controller, not a headless network
protocol. See DJI's [Mobile SDK architecture](https://developer.dji.com/doc/mobile-sdk-tutorial/en/basic-introduction/msdk-introduction.html).

### Receiver → Zenoh → Open Drone ID translation

The standard path is:

`receiver/detection system → local Zenoh router → federated Zenoh routers → opendroneid.py → normalized Zenoh track → TAK/SitaWare layers`

The receiver publishes to:

`{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/raw/opendroneid/{receiver_id}/{transmitter_id}`

The payload can be the exact binary 25-byte ASTM/ASD-STAN message or an exact
Message Pack of at most nine messages. When receiver metadata is available, it
can instead publish this bounded JSON envelope:

```json
{
  "payload_b64": "base64-encoded ASTM/ASD-STAN bytes",
  "source_id": "optional transmitter identifier",
  "transport": "ble-or-vendor-transport",
  "rssi_dbm": -52
}
```

`payload_hex` may replace `payload_b64`, but both must not be present. Receiver
and transmitter topic segments are limited to deployment-safe identifiers; the
envelope is limited to 4096 bytes and the decoded protocol payload to 228 bytes.
The translator then publishes only to:

`{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/air/opendroneid/passive_rf/{affiliation}/uav/{type}/{id}/sapient`

Configure runtime-only `OPENDRONEID_FRIENDLY_IDS` to classify own UAS IDs as
friendly; all others remain unknown. The ordinary router launcher starts the
idle-safe translator, not a radio receiver. OpenDroneID carries public identity,
position, altitude, movement, status, and optional operator/takeoff information.
It does not carry DJI's proprietary battery, camera/video, payload, or detailed
health telemetry.

EFDI intentionally contains no BLE/Wi-Fi receiver implementation. A partner's
receiver or detection system is responsible for publishing the raw contract to
its attached Zenoh router; `opendroneid.py` only translates traffic already in
Zenoh.

## Next protocol candidates

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

## Hackathon partner intake checklist

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
