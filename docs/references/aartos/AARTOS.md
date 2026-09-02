# Aaronia AARTOS sources

`compose/protocols/vendors/aartos/aartos_json.py` decodes the Aaronia
AARTOS drone-tracking JSON format exposed by RTSA-Suite PRO's HTTP Server
block (Sample -> TrackState -> `trackings[]`/`antennas[]`), landed locally
by `compose/bridges/aartos_bridge.py`.

## Source

Aaronia's own vendor-documented REST API (RTSA-Suite PRO's "HTTP Server"
flow-graph block): a `GET /sample` (and `/stream`, `/samples`,
`/dronesdb`, `/healthstatus`) JSON contract.

**2026-09-02 update:** the actual "RTSA HTTP Stream Server Endpoints"
protocol document (v9) was located and fetched directly — it turns out to
be publicly downloadable from Aaronia's own SPECTRAN V6 support forum, not
vendor-private as originally assumed:
[`RTSA-http-Stream-Server-Endpoints-9.pdf`](https://v6-forum.aaronia.de/wp-content/uploads/asgarosforum/358/RTSA-http-Stream-Server-Endpoints-9.pdf),
attached to the forum thread
[`v6-forum.aaronia.de/forum/topic/rtsa-suite-pro-http-streaming/`](https://v6-forum.aaronia.de/forum/topic/rtsa-suite-pro-http-streaming/)
(an older v8 revision is attached to the same thread; not used). This is
the actual "HTTP Stream Server Endpoints" PDF `aartos_bridge.py`'s
docstring names — the not-yet-found companion "RTSA-Suite PRO JSON
Protocol Documentation" (data *shape* rather than endpoint list) remains
unlocated. Not vendored into either repo — Aaronia support-forum material,
copyright status unclear, unlike `asterix-specs` (BSD-3-Clause) or
SAPIENT's Dstl schema (Apache-2.0) which are both explicitly open-licensed.

Confirmed directly from the fetched v9 document:
- The full endpoint set is `/sample`, `/samples`, `/stream`, `/inputs`,
  `/control`, `/remoteconfig`, `/info`, `/healthstatus`, `/user` — this is
  **every** endpoint an HTTP Server block exposes, and it is identical
  regardless of what feeds that block. There is no separate endpoint for a
  particular detection type (drone, WiFi/operator, or otherwise) — the
  data behind `/sample`/`/stream` depends entirely on what's wired to that
  HTTP Server block inside RTSA-Suite PRO's own flow-graph editor, not the
  URL path queried.
- `/healthstatus`'s own documented shape is "one main group item ...
  with one child group item **per health-aware block**" (its `components`
  field additionally lists "sub blocks when using a system with HTTP block
  connected satellites"). A block with nothing connected to it therefore
  has no children to report — directly explaining the empty
  `items: []` tree observed below, independent of and prior to any
  question of whether a drone or WiFi signal is currently present.

## Trust: verified against real traffic

Unlike most of `asterix-specs/ASTERIX.md`'s categories, this decoder has
been run against a real, live Aaronia RTSA-Suite PRO deployment (three
IsoLOG multi-antenna units, zenoh-gateway pod) over an actual field
session, not just a synthetic self-consistency check:

- `data.antennas[]` (surveyed antenna position: `latitude`/`longitude`/
  `elevation`/`antennaID`/`antennaName`) was confirmed against three real
  antennas' real, distinct surveyed coordinates.
- The `/sample` vs `/stream` endpoints were found to behave differently on
  a real deployment than the vendor PDF alone would suggest: `/stream`
  (chunked, RS-delimited) returned HTTP 200 with correct headers but never
  delivered a single byte of body on this real system, while `/sample`
  (single-object poll) worked correctly — `aartos_bridge.py`'s
  `AARTOS_MODE=poll` fallback exists because of this real-world finding,
  not a hypothetical.
- A real RTSA-Suite PRO deployment was also found to expose *multiple*
  independent HTTP Server blocks on different ports simultaneously, only
  one of which was actually wired to live Tracking output — an unwired
  block still answers every endpoint with valid-looking JSON (`null` for
  `/sample`, an empty-but-well-formed object for `/healthstatus`) rather
  than erroring, so port choice cannot be assumed from the vendor docs
  alone; it has to be confirmed against real `/sample` output per
  deployment.
- **2026-09-02, reconfirmed on a second block:** this deployment's operator
  added a second HTTP Server block, `Block_HTTPServer_3` (`/info` title
  "WATCHDOG"), on port 54664, intended for WiFi-based drone-operator
  position finding alongside the existing drone/airframe block on 54663
  (`Block_HTTPServer_1`, "HTTP Server 2"). Checked directly against the
  live deployment: 54663's `/healthstatus` shows a full component tree
  (3 antennas, all `state: 5`/operational); 54664's `/healthstatus` is
  `{"type":"group","name":"healthstatus","label":"HealthStatus","flags":"","items":[]}`
  — the exact empty-tree signature above, reproduced on a different port
  months later. `/sample` on 54664 returned `null` on 5 consecutive tries
  and `/stream` returned nothing in a 10s window. `/dronesdb` (the
  category legend: Beacon/Bearing/Bird/Drone/.../WLAN/POA/Remote/etc.) is
  byte-identical on both ports — it is static reference data, not
  block-specific, and cannot be used to tell which port is wired to what.
  Conclusion: the WiFi/operator block exists as an HTTP endpoint but has
  no detector component connected to it in RTSA-Suite PRO's own flow graph
  yet — this is a config step on the AARTOS laptop itself (wire the
  WiFi/direction-finding detector to `Block_HTTPServer_3`), not an EFDI-side
  issue, and not something that starts producing data just because a drone
  starts flying.

## What hasn't been verified

- `data.trackings[]` (an actual drone detection) has not yet been observed
  in a real sample from this deployment — the antenna/site and stream-
  behavior findings above are real-traffic-verified, but the `trackings[]`
  field mapping in `tracking_to_track()` (lat/lon/velocity/predicted
  position/alertLevel) is implemented from the vendor's documented schema,
  not yet cross-checked against a real tracked-drone payload.
- `_OPERATOR_CATEGORIES` (`wlan`/`remote`/`poa` — intended to route a drone
  *operator's* RF-detected position to a ground-unit marker instead of a
  UAV one) is based on category *names* observed in a real `/dronesdb`
  response from this deployment, but the WiFi/operator-geolocation block
  identified above is confirmed unwired (see the 2026-09-02 entry) — so
  the actual JSON shape such a tracking entry would carry is still
  unconfirmed, and so is whether `_OPERATOR_CATEGORIES`'s reclassification
  is even wired into `tracking_to_track()`/`topic_for_track()` yet (as of
  this entry, it isn't — the field is computed but unused). Treat this
  mapping as provisional until the block goes live and a real one is seen.
