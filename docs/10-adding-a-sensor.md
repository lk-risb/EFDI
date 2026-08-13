# 10 — Adding a New Sensor or Protocol

This is the step-by-step path from "I have a new sensor/feed" to "it shows up
in TAK and SitaWare automatically." It assumes the pod is already installed
and running (§§1-6 above).

Read [§7 Integrations](08-integrations.md#integrations) first if you haven't — it explains
the fabric this walkthrough plugs into (the topic taxonomy, the four output
views, what's already wired). This section is the concrete "now build one"
steps; that one is the reference for what already exists.

## 10.0 Decide: bridge, or protocol?

- **`compose/bridges/`** — your new integration *connects to a product or
  service*: it polls an HTTP API, opens a TCP socket to a vendor box, listens
  on a UDP port for a specific device. One file per external thing it talks to.
- **`compose/protocols/`** — your new integration *decodes an already-defined
  wire format* that isn't tied to one vendor (a standard, a spec, a schema).

Most new sensors are bridges — a physical or networked device this router
connects to directly. If in doubt, pick `bridges/`; it's the more common case
and nothing downstream cares which directory a script lives in.

## 10.1 Do you need a new message schema, or does an existing one fit?

If your sensor reports a moving object — position, optionally speed/heading/
altitude/identity — it almost certainly fits the existing generic
`NormalizedTrack` schema (`../compose/protocols/proto/normalized_track.proto`)
and you need **no new protobuf work at all**. Skip to step 2.

Only define a new `.proto` message if your data has structured fields
`NormalizedTrack` genuinely can't express (e.g. a multi-point area/zone, or
a domain-specific compound value). If so:

1. Add a new `.proto` file under `compose/protocols/proto/` — every
   EFDI-authored schema lives there, regardless of which translator owns it
   (an actual vendored/licensed third-party schema, like the SAPIENT or
   Sparkplug B wire contracts, is the one exception and stays under
   `compose/protocols/vendors/<name>/` next to its own LICENSE) —
   modeled on an existing one — `geojson_features.proto` is a short example.
2. Regenerate the Python bindings: `scripts/generate-protobuf.sh` (needs
   `grpc_tools.protoc` + `protobuf` — already in `compose/requirements.txt`).
   This writes into `compose/generated/`, which is gitignored — every
   developer/deployment regenerates it locally, nothing generated is
   committed.

## 10.2 Write the script

Every bridge/protocol script follows the same shape. This is the complete,
working reference — `compose/protocols/random/geojson_features.py` (127
lines) — trimmed to the parts that matter:

```python
from namespace_prefix import topic_root
from gateway import open_session, publish_dual
# Reuse the generic schema — no new .proto needed for a plain moving object:
from protocols.proto.normalized_track_pb2 import NormalizedTrack

TOPIC_ROOT = topic_root()
OUTPUT_TOPIC = TOPIC_ROOT + "/<domain>/<your-source-name>/<modality>/<affiliation>/<entity>"

def normalize(raw: dict) -> dict | None:
    """Turn one of your sensor's records into the shared track shape.
    Required: _ts (epoch seconds), _src (your source name), uid (stable per-object id).
    Everything else is optional — only set what you actually have."""
    return {
        "_ts": time.time(),
        "_src": "your-sensor-name",
        "uid": "YOURSENSOR-" + raw["id"],
        "lat_deg": raw["lat"],
        "lon_deg": raw["lon"],
        # optional: "speed_ms", "heading_deg", "baro_alt_m", "callsign", ...
    }

def run() -> None:
    session = open_session()
    for raw in your_data_source():          # poll an API, read a socket, etc.
        record = normalize(raw)
        if record:
            publish_dual(session, OUTPUT_TOPIC, record, NormalizedTrack)
```

Your script never imports `zenoh` itself — `gateway.py` is the only module that
does. If you need to subscribe to a raw input topic instead of polling, use
`gateway.subscribe(session, topic, callback)` the same way.

`publish_dual` does the rest: it publishes all four fabric views (`/sapient`,
`/json`, `/proto`, `/raw`) from that one call — see
[§7 Integrations → "Egress topic views"](08-integrations.md#egress-topic-views-sapient-json-proto-raw)
for what each view is for. You never publish to TAK or SitaWare directly —
`tak_layer`/`sitaware_layer` subscribe to every normalized topic on the fabric
automatically, so a correctly-published track appears in both without any
further code.

**Topic path.** Follow the taxonomy from [§7 Integrations](08-integrations.md):
`{domain}/{source}/{modality}/{affiliation}/{entity}` — e.g. `land` (or `air`/
`sea`), your sensor's short name, what kind of sensing it is, `neutral` if you
don't have real affiliation data, and what the object is (`vehicle`,
`vessel`, `unit`, ...). Look at a few existing topics
(`docs/13-topic-taxonomy.md`) for the pattern before inventing a new shape.

**Configuration — nothing hardcoded.** Any host, port, URL, or credential your
script needs comes from an environment variable, never a literal in the code
(`compose/bridges/sitaware_bridge.py` is a good example of an all-env-driven
bridge). Add each new variable to `compose/.env.example` with a one-line
comment explaining what it's for — that file is the single source of truth
for what a deployment can configure, and it's what the next administrator
reads to know what to fill in.

**Verify it compiles:**
```bash
python3 -m py_compile compose/bridges/your_new_bridge.py
```

## 10.3 Register it with the launcher

Four small edits to `start.sh`, following the existing `geojson` entry as the
template (search for `geojson` in `start.sh` to see all four at once):

1. **`SERVICES` array** — add your service's short name to the list.
2. **`SVC_CAT`** — which category it shows under in the menu/WebUI
   (`"Sensor bridges"`, `"Protocols"`, etc.).
3. **`SVC_DESC`** — a one-line human description.
4. **`svc_ready()`** — when is it safe/meaningful to start? If it needs no
   configuration to be useful, add your name to a `return 0` case alongside
   `cap`/`geojson`/etc. If it needs an env var set first (a URL, a host), gate
   on that instead — e.g. `admin-control`'s gate checks a secret key is set;
   yours might check `[[ -n "${YOUR_SENSOR_URL:-}" ]]`.
5. **Launch case** — add `_start your-service-name path/to/your_script.py` in
   the big `case` block that actually launches services.

## 10.4 Verify end-to-end

```bash
./start.sh --service your-service-name
```
Then confirm data is actually flowing — subscribe to your topic with any
Zenoh client (the repo's `clients/examples/` has ready-made subscribe
scripts) and confirm records arrive. If TAK or SitaWare output is enabled,
open ATAK/WinTAK or the SitaWare map and confirm your object appears with no
further configuration — that's the proof the fabric contract was followed
correctly.

## 10.5 New CoT symbol needed? (TAK output only)

If your sensor's affiliation/entity combination doesn't already map to a CoT
type, add it to `_TOPIC_COT` in `compose/layers/tak_layer.py`:
```python
"air/**/hostile/uav/**":      ("a-h-A-M-F-Q", AIR_STALE_S),
"land/**/neutral/sensor/**":  ("a-n-G-E-S",   LAND_STALE_S * 2),
```
The key is a topic-suffix glob; the value is the MIL-STD-2525C/APP-6 CoT type
code and a staleness window. Most new sensors already match an existing
pattern — only add one if your topic path genuinely doesn't.

## 10.6 Document it

Add a row to the relevant table in [§7 Integrations](08-integrations.md) (under
"Source-specific bridges" or the protocol table) describing what it needs
configured. This is what makes the *next* administrator's job the same
one-read, no-guessing experience this doc gave you.

### Checklist before you call it done

- [ ] No literal host/port/URL/credential in the script — everything is an
      env var, documented in `compose/.env.example`.
- [ ] `python3 -m py_compile` passes.
- [ ] Registered in all four `start.sh` places (`SERVICES`, `SVC_CAT`,
      `SVC_DESC`, `svc_ready`) plus the launch case.
- [ ] Confirmed the topic on the fabric, then confirmed it in TAK/SitaWare
      with no code changes to either.
- [ ] A row added to [§7 Integrations](08-integrations.md).

---
