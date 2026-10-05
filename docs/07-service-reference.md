# 07 — Service Reference

## Service Reference

> **Topic tiers.** The `…/tracks/v1` paths below are the JSON tier. Each one has
> two protobuf siblings carrying the same event: `…/tracks/v2` (typed message
> from the protocol's `.proto`) and `…/tracks/native/v1` (a `RawEnvelope`
> wrapping the original wire bytes, byte-exact). Prefer `/v2`; use `/native/v1`
> when you need a field EFDI does not decode. `/v1` is legacy and will be
> retired. Full explanation: [Integrations → Egress topic views](08-integrations.md#egress-topic-views-sapient-json-proto-raw).

| Service | Script | Zenoh topic (abbreviated) | Trigger |
| --- | --- | --- | --- |
| `asterix` | `protocols/vendors/asterix/cat.py` | `…/raw/asterix/catNN` and category-specific normalized ASTERIX topics | ASTERIX vendor's CAT protocol bundle: mixed UDP ingress plus per-category translators |
| `dronuradaras` | `bridges/vendors/mainline/dronuradaras_bridge.py` | `…/land/mainline_dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | 60 s online-only device poll with offline eviction / 10 s detection poll |
| `dangausakis` | `bridges/vendors/dangausakis/dangausakis_bridge.py` | `…/land/dangausakis/alert/neutral/zone/status` | 60 s poll of dangausakis.lt region alerts (LT/LV/PL/EE/UA); alerts older than 6 h dropped; draws active alert regions as red/orange/yellow polygons (LT/LV/PL/EE/UA, JSON-only zone tracks under `…/land/dangausakis/airzone/…`); also publishes adsb.lol aircraft as `…/air/dangausakis/{adsb,mlat}/civ/aircraft` tracks keyed by icao24; `tak_alert_layer` turns each into a TAK GeoChat; also runs the NEPTUN threats feed (`bridges/vendors/dangausakis/neptun.py`): live Ukraine drones/missiles as hostile tracks under `…/air/neptun/…`, 10 s poll, area-only records skipped, marker held back when `track-fusion` matches it to a nearby non-cooperative radar track, visible NEPTUN credit per its terms Alerts for UA come straight from NEPTUN's district alerts and for LT from the LT72 warnings RSS (nationwide, all counties coloured, one popup); Poland's RSO notices and Latvia's 112.lv alerts feed are only added to the site's alerts, and only when they name an air raid; EE has no machine-readable source and comes from the site's feed, and a country whose direct source is down falls back to it. NEPTUN names are shown in English (romanised) and NEPTUN's alerts have no age cut-off, so long-standing ones such as occupied regions stay drawn. Community drone reports and incident markers from the site are published as tracks (UAV and ground marker, expiring with the report), and tak_alert_layer sends GeoChat for regional status changes (raised/changed/cleared, one message per country per poll), for a data source that is down or frozen (and its recovery), and for new drone reports and incidents. |
| `sitaware` | `bridges/vendors/sitaware/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Configurable REST poll |
| `nffi` | `protocols/vendors/nffi/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Complete XML documents under `…/raw/nffi/*` in Zenoh |
| `stanag` | `protocols/vendors/stanag/stanag.py --proto {4586,4607,4609,5516}` | `…/raw/stanag_4609/klv`, `…/air/stanag_4609/camera/unknown/uav`, STANAG 4586 track topics, and `…/{air,sea,land}/stanag_5516/c2/**` | Launcher starts each configured `--proto` directly |
| `sapient-raw`, `stanag4586-raw`, `stanag5516-raw` | `bridges/vendors/{sapient,milspec}/*_bridge.py` | `…/raw/<protocol>/<source>` | Optional socket ingress; matching protocol runs with `*_ZENOH_RAW=1` |
| `cap` | `protocols/vendors/random/cap.py` | `…/land/cap/c2/neutral/sensor/{type}/{id}/sapient` | Complete CAP 1.2 XML on `…/raw/cap/**` |
| `mqtt` | `protocols/vendors/random/mqtt_json.py` | `…/land/mqtt/iot/unknown/sensor/{type}/{id}/sapient` | Vendor JSON on `…/raw/mqtt/**` (bridge forwards any payload verbatim) |
| `sparkplug` | `protocols/vendors/sparkplug/sparkplug.py` | `…/land/sparkplug/iot/unknown/sensor/{type}/{id}/sapient` | Sparkplug B protobuf on `…/raw/mqtt/spBv1.0/**` |
| `sensor-health` / `mission-route` | Matching `protocols/vendors/random/*.py` | `…/land/health/**`, `…/air/mission/**` | JSON on their `…/raw/**` topics |
| `tak_layer` | `layers/vendors/tak/tak_layer.py` | Subscriber — all topics | Event-driven |
| `tak-bridge` | `bridges/vendors/tak/tak_bridge.py` | Subscriber — all topics | TAK-visible CoT ingress |
| `sitaware-hq-nvg` | `layers/vendors/tak/systematic/sitaware_layer.py` | Subscriber — all track topics | Pull-based NVG snapshot |
| `intcore_layer` | `layers/vendors/tak/random/intcore_layer.py` | Subscriber — all track topics | Event-driven; POSTs NVG 2.0.2 to INT-CORE's `Topic/SaveItems` |
| `intcore-bridge` | `bridges/vendors/intcore/intcore_bridge.py` | `…/{domain}/intcore/c2/{affiliation}/{entity}/{type}/{id}/sapient` | HTTP Post ADT listener — INT-CORE's own Dissemination service pushes here |
| `track-fusion` | `protocols/fusion.py` | CAT-48 + CAT-21 subscriber; also matches NEPTUN threats to non-cooperative radar tracks (`FUSION_NEPTUN_MIN_NM`/`MAX_NM`) | Event-driven |

### TAK users and external CoT sources

### Zenoh-native raw ingress

For a receiver host that should own the network socket, select the matching
`*-raw` bridge and set its raw port. Select the protocol translator separately
with its `*_ZENOH_RAW=1` setting. For example:

The raw bridge publishes octets only; it does not classify or alter them. The
SAPIENT/FLEX 335 and STANAG 4586 translators consume those Zenoh topics and
publish normalized JSON. SAPIENT ingress
uses the public BSI Flex 335 v2 protobuf contract. The retained STANAG 4586
binary layout is a historical deployment approximation, not a generic standard
profile: it stays disabled unless `STANAG4586_PROFILE=legacy_ed3_approx` is
explicitly set after validating the layout against the deployed VSM ICD.

CAP, health, and route translators are idle-safe
Zenoh subscribers. A partner publishes complete JSON/XML/NMEA payloads below
the corresponding `raw/**` topic; no internet URL or receiver is embedded in
the translator.

`mqtt` is a generic MQTT sensor JSON translator, reused for any MQTT-shaped
feed that doesn't have its own named vendor integration — for example, a
JSON drone-detection feed with `latitude`/`longitude`/`altitude`/`heading`
fields is a direct fit: point the feed at the `mqtt` translator's input
topic (or override `MQTT_INPUT_TOPIC`) and it needs no new code.

CoT and SitaWare HQ NVG outputs apply the same scenario affiliation policy:
aircraft in the configured RU/BY ICAO address ranges and vessels with RU/BY
MMSI MIDs are hostile; other partner-provided air/sea contacts remain neutral. An
origin-country label alone does not override an invalid or missing transponder
identifier.

`tak-bridge` is the inverse CoT path: it connects to a TAK-visible CoT feed
over the documented TCP/TLS session, extracts complete `<event>...</event>`
frames, and republishes normalized JSON into Zenoh. It does not replace the
CoT output layer and it does not use Zenoh as the TAK wire transport.

### Video streaming (mediamtx)

`mediamtx` (`bridges/vendors/mediamtx/mediamtx`, config at
`compose/bridges/vendors/mediamtx/mediamtx.yml`) is a separate video pipeline —
it never touches Zenoh or a topic. It ingests one drone's video, then fans
that same path out to TAK, SitaWare, and the WebUI's Streams tab. Currently
enabled in `mediamtx.yml`:

| Direction | Protocol | Port | Used for |
| --- | --- | --- | --- |
| Ingest (push in) | RTMP | `1935` | FreeFlight's stream URL |
| Egress (pull out) | RTSP (TCP only — see [11 — Troubleshooting](11-troubleshooting.md)) | `8554` | TAK, SitaWare |
| Egress (pull out) | WebRTC (WHEP) | `8889` | WebUI Streams tab, live tiles |
| Recording | fMP4 segments, 1-minute, rolling 10-minute retention | — | WebUI Streams tab, scrub-back bar |

mediamtx itself also supports HLS and SRT (both ingest and egress) — neither
is enabled here yet. SRT in particular is worth enabling as an alternate
ingest path if a drone/GCS ever offers it instead of RTMP: unlike RTMP's
plain TCP, SRT has built-in packet-loss retransmission (ARQ), designed for
exactly the lossy-link case that already causes the relayed-NetBird RTMP
issue in the troubleshooting doc. Not needed until a real source actually
asks for it.

**STANAG 4609 video, not just drone RTMP.** `bridges/vendors/milspec/4609_bridge.py` owns
the SRT connection carrying STANAG 4609 MPEG-TS+KLV as its *listener* (the
sensor/GCS connects in) purely to extract KLV metadata — mediamtx cannot
also bind that port, and routing the feed through mediamtx first isn't an
option either: MediaMTX has no support for passing an MPEG-TS KLV/data
track through today (open upstream feature request, unimplemented), so
metadata would be silently dropped before this bridge ever saw it. Set
`STANAG4609_VIDEO_RELAY_ENABLE=1` (off by default) to also relay the
video/audio essence into mediamtx from that same already-open connection —
ffmpeg's own `tee` muxer splits the one demux into the KLV output
(unchanged) and a best-effort remux (`-c copy`, no transcode) into
mediamtx's RTMP ingest, tagged `onfail=ignore` so a down or restarting
mediamtx never affects KLV extraction. Once relayed, the feed shows up in
the WebUI Streams tab and reaches TAK/SitaWare exactly like a drone feed —
see `STANAG4609_VIDEO_PATH` in `.env.example` for the path name it uses.
