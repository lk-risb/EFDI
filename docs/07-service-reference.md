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
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | 60 s online-only device poll with offline eviction / 10 s detection poll |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Configurable REST poll |
| `nffi` | `protocols/random/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Complete XML documents under `…/raw/nffi/*` in Zenoh |
| `stanag` | `protocols/vendors/stanag/stanag.py --proto {4586,4607,4609,5516}` | `…/raw/stanag_4609/klv`, `…/air/stanag_4609/camera/unknown/uav`, STANAG 4586 track topics, and `…/{air,sea,land}/stanag_5516/c2/**` | Launcher starts each configured `--proto` directly |
| `sapient-raw`, `stanag4586-raw`, `stanag5516-raw` | `bridges/*_bridge.py` | `…/raw/<protocol>/<source>` | Optional socket ingress; matching protocol runs with `*_ZENOH_RAW=1` |
| `cap` | `protocols/random/cap.py` | `…/land/cap/c2/neutral/sensor/{type}/{id}/sapient` | Complete CAP 1.2 XML on `…/raw/cap/**` |
| `mqtt` | `protocols/random/mqtt_json.py` | `…/land/mqtt/iot/unknown/sensor/{type}/{id}/sapient` | Vendor JSON on `…/raw/mqtt/**` (bridge forwards any payload verbatim) |
| `sparkplug` | `protocols/vendors/sparkplug/sparkplug.py` | `…/land/sparkplug/iot/unknown/sensor/{type}/{id}/sapient` | Sparkplug B protobuf on `…/raw/mqtt/spBv1.0/**` |
| `sensor-health` / `mission-route` | Matching `protocols/random/*.py` | `…/land/health/**`, `…/air/mission/**` | JSON on their `…/raw/**` topics |
| `tak_layer` | `layers/tak_layer.py` | Subscriber — all topics | Event-driven |
| `tak-bridge` | `bridges/tak_bridge.py` | Subscriber — all topics | TAK-visible CoT ingress |
| `sitaware-hq-nvg` | `layers/sitaware_layer.py` | Subscriber — all track topics | Pull-based NVG snapshot |
| `track-fusion` | `protocols/fusion.py` | CAT-48 + CAT-21 subscriber | Event-driven |

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
