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

**2026-09-16 update — WiFi/spectrum blocks now wired, `/sample` still stale:**
the operator rewired the RTSA-Suite PRO flow graph on the AARTOS laptop
today (new mission `automatic_master_final_nauja_0916_efdi.rmix`, confirmed
via `/info`): added a merger/mux block (`/inputs` on port 54663 now lists
16 `muxN` inputs plus `main`, where it previously had none) and connected
every detector directly to it, intending all data to reach a single HTTP
Server block on port 54663. Checked live against `/healthstatus`:

- `WIFI 180`/`WIFI 183`/`WIFI 184` blocks are now present with
  `state: 5` (operational) — this directly supersedes the 2026-09-02 entry
  above, which found the WiFi/operator-position block wired to an HTTP
  endpoint but with **no** detector connected (empty health tree). It has a
  real detector connected now.
- `SPECTRAN 180`/`SPECTRAN 184` (Aaronia SPECTRAN V6B spectrum-analyzer
  blocks) also report `state: 5`; `SPECTRAN 183` reports `state: 7`
  (Warning) — consistently, across every place it appears in the tree — an
  operator-side issue to check, not something visible from `/sample`.
- `IQ DJI DroneID Decoder` blocks (one per antenna: 180/183/184) are present
  with `state: 5` — a DJI RemoteID (DroneID) decode path exists in this
  flow graph. Not yet cross-checked against a real decoded payload (no
  drone was broadcasting DroneID during this check).

Despite all of that, `/sample` on port 54663 returned the **exact same
`data.antennas[]` snapshot, same `updateTime`, across every poll over a
10+ minute window** (`trackings: []` throughout) — the live health tree
above proves the flow graph itself is running and freshly updating (its own
`Last Update` timestamp advances), but whatever `/sample` actually
serializes did not pick up new data in that window. Two explanations,
neither confirmed:
1. Nothing was actually detected in that window (no aircraft flying, no
   WiFi target in range) — `trackings: []` would be correct, not stale.
2. The HTTP Server block's own sample-source binding was not repointed at
   the new merger's output when the merger was added, and is still reading
   whatever single block fed it before.
Whichever it is, `aartos_json.py`'s decoder was not touched — nothing in
that decoder's field mapping needs to change based on the merger itself;
merging multiple blocks into one HTTP Server does not create any new JSON
key or shape, per the 2026-09-02 finding above ("no separate endpoint for a
particular detection type ... depends entirely on what's wired").

Also worth stating explicitly since it came up in this check: RTSA-Suite
PRO's HTTP Server block only ever serializes Sample/TrackState JSON
(`antennas[]`/`trackings[]`) regardless of what's wired to it — raw
spectrum-sweep/waterfall data from a SPECTRAN block is not expected to
appear in this JSON at all, even once everything is correctly wired. That
data class is not part of this vendor contract as understood so far;
treat any future report of "spectrum data over `/sample`" as needing new
research, not an extension of the existing decoder.

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
  response from this deployment. **Correction to this file's own earlier
  claim:** as of commit 7a81c04, this reclassification IS wired end to
  end — `tracking_to_track()` sets `_entity_kind: "unit"` for any tracking
  whose `categoryName` matches, and `topic_for_track()` routes it onto
  `{root}/land/aartos/passive_rf/{affiliation}/unit` instead of the usual
  `air/.../uav` topic. tak_layer.py already has generic, source-agnostic
  CoT mappings for every affiliation on that exact shape
  (`land/**/friendly|hostile|neutral|unknown/unit/**`, all four), so once a
  real WiFi/operator tracking entry flows through, it renders on TAK as a
  ground unit with zero additional code — checked directly in
  compose/layers/tak_layer.py, not assumed. What remains genuinely
  unverified: the actual JSON *shape* of such a tracking entry, since no
  operator/WiFi tracking has been observed in a real payload yet (the
  2026-09-16 rewiring above got the WIFI/SPECTRAN blocks to `state: 5` in
  `/healthstatus`, but `/sample` never produced a single non-empty
  `trackings[]` entry, WiFi or otherwise, in that session — no format was
  ever captured to decode, only proof the pipeline is enabled). Treat the
  field-name mapping inside `tracking_to_track()` as provisional until an
  actual operator-category tracking entry is seen; the *routing* (unit vs.
  uav, land vs. air, and TAK's rendering of it) is not provisional — that
  part is code-verified today.

- `alertLevel`'s exact trigger condition is unconfirmed. RTSA-Suite PRO's
  zone editor has three colored zone tiers (user-confirmed 2026-09-16:
  green/yellow/red), which plausibly map onto `_AFFILIATION`'s tiers —
  yellow -> `warning`, red -> `defend`/`panic` — matching why `_AFFILIATION`
  puts `warning` at `unknown` (a caution zone, not yet a confirmed hostile
  act) and only `defend`/`panic` at `hostile` (2026-09-16 change, previously
  `warning` was also mapped to `hostile`). This is naming-plausible, not
  confirmed: no real zone crossing has been observed via `/sample` yet. To
  confirm, watch `trackings[].alertLevel` + `.zoneIDs` together during an
  actual green/yellow/red zone crossing and update this entry with what
  RTSA-Suite PRO actually sends.
