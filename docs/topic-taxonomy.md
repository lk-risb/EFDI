# EFDI topic taxonomy

Status: **implemented**.

## The key

```
{prefix}/{pod}/{domain}/{source}/{modality}/{affiliation}/{entity}/{type}/{id}/{view}
```

```
LTU/CISB/hq/air/adsblol/adsb/civ/aircraft/b738/ly-abc/sapient
LTU/CISB/hq/air/010-042/radar/unknown/aircraft/unknown/cat48-010-042-4211/json
LTU/CISB/hq/land/dronuradaras/acoustic/unknown/drone/unknown/1/sapient
LTU/CISB/hq/sea/aisstream/ais/civ/vessel/cargo/276000000/raw
```

| Segment | Meaning | Example |
|---|---|---|
| `prefix` | Org prefix, deployment-configured | `LTU/CISB` |
| `pod` | Which pod published it | `hq` |
| `domain` | Physical domain | `air` · `land` · `sea` · `space` |
| `source` | WHO observed it — the sensor or feed identity | `010-042` · `adsblol` · `dronuradaras` |
| `modality` | HOW it was observed | `radar` · `acoustic` · `adsb` |
| `affiliation` | Friend/foe posture | `civ` · `mil` · `friendly` · `hostile` · `neutral` · `unknown` |
| `entity` | What kind of thing | `aircraft` · `uav` · `vessel` · `person` · `vehicle` |
| `type` | Specific type, `unknown` when the sensor cannot know it | `b738` · `unknown` |
| `id` | Stable identity of this object | tail number, ICAO24, MMSI, radar track number |
| `view` | Which encoding of the same object | `sapient` · `json` · `proto` · `raw` |

## Why source and modality are separate segments

They answer different questions, and one segment cannot serve both.

`source` is provenance: *who said this*. Two ADS-B feeds must stay distinct,
because `track_fusion_bridge` exists to de-duplicate them against each other.
Two radars feeding one router must stay distinct, or their tracks collide.

`modality` is method: *how was it sensed*. This is what a C2 consumer filters
on, because trust, latency and accuracy differ by sensing method far more than
by vendor.

Collapsing them costs one or the other. Keeping both means `**/acoustic/**`
and `**/dronuradaras/**` are both answerable.

### The modality vocabulary

Taken from the SAPIENT `NodeType` enum
(`compose/vendor/sapient_msg/bsi_flex_335_v2_0/registration.proto`), so the
topic segment and the payload agree instead of drifting:

`radar` · `lidar` · `camera` · `seismic` · `acoustic` · `proximity_sensor` ·
`passive_rf` · `human` · `chemical` · `biological` · `radiation` · `kinetic` ·
`jammer` · `cyber` · `ldew` · `rfdew` · `mobile_node` · `pointable_node` ·
`fusion_node`

Four values SAPIENT lumps under `PASSIVE_RF` / `OTHER` are kept distinct
because EFDI has to route on them:

| Extra | Meaning |
|---|---|
| `adsb` | Aircraft self-reporting, relayed by a ground station |
| `mlat` | Position computed from time differences of arrival |
| `ais` | Vessel self-reporting over AIS |
| `c2` | Arrived via a command-and-control or battle-management system |
| `telemetry` | The platform reporting its own state (own UAV, MAVLink, DJI) |
| `fused` | Output of a tracker that already combined several sensors |
| `unknown` | Not an observation at all (station health, overlays) |

## Where each segment comes from

**ASTERIX names its own sensor.** Every category carries SAC/SIC in I0xx/010,
so `source` is `{SAC:03d}-{SIC:03d}` — decoded per record, not configured. The
topic constants in `cat.py` are therefore templates holding `{source}`.

**The ASTERIX category picks the modality**, because the category already
encodes the sensing method:

| Category | Physically | modality |
|---|---|---|
| CAT-010, CAT-034, CAT-048 | primary/secondary radar | `radar` |
| CAT-021 | ADS-B, relayed | `adsb` |
| CAT-020 | multilateration | `mlat` |
| CAT-062 | system tracks, already fused | `fused` |

**SAPIENT ingest reads it off the wire.** A node declares its `NodeType` when
it registers, so an incoming SAPIENT camera lands under `/camera/` and a radar
under `/radar/`, rather than every node collapsing into one segment.

## Formats

All four views are named — nothing is implicit, so a consumer reading a key
always knows what the bytes are:

| Topic | Payload |
|---|---|
| `…/{id}/sapient` | BSI Flex 335 v2 `SapientMessage` — the fabric contract |
| `…/{id}/json` | Flat JSON, readable |
| `…/{id}/proto` | EFDI per-protocol protobuf, full sensor detail |
| `…/{id}/raw` | Original wire bytes in a `RawEnvelope` |

## Control plane keeps versions

Data formats never supersede each other, so they are named, not numbered.
Control-plane contracts genuinely revise, so they keep versions:

```
{root}/**/@config/v1          {root}/**/@config/status/v1
{root}/**/@config/relay/v1    {root}/**/@topology          (unversioned)
```

## Subscribing

```
**/air/**                       all airborne, any source
**/radar/**                     everything sensed by radar, any radar
**/010-042/**                   everything from ONE radar
**/hostile/**                   all hostile contacts
**/aircraft/**                  aircraft only
…/aircraft/b738/**              one aircraft type
…/{id}/sapient                  one object, SAPIENT
**/sapient                      every object, SAPIENT only
```

## Known trade-offs

**`type` is mutable.** ADS-B knows `b738` from the registry; radar does not and
publishes `unknown`. If a track's type resolves later its topic CHANGES, and
subscribers see the old key go quiet and a new one appear. Accepted
deliberately — the type is also inside the SAPIENT `classification`, which can
be revised without moving the key.

**`id` makes topics per-object.** Cardinality goes from ~40 topics to one per
tracked object. Zenoh handles this, but it changes retention: a storage plugin
now keeps a last-known-value per object rather than per class.

**`modality` is dead weight for non-sensors.** Overlays and health beacons
(`mission`, `cap`, `ogc`, `health`) carry `unknown` in a segment that never
means anything for them. Accepted so every key has fixed positions and a
consumer can split on `/` without checking which segments are present.

**Wildcards where the source is dynamic.** A radar names itself by SAC/SIC, so
its topic is not knowable at startup. Subscribers use `*` in the source slot
(`…/air/*/radar/**`) — which also, deliberately, picks up every radar.

## How it is built

`semantic_topic()` in `compose/protocols/protobuf_codec.py` is the single place
that assembles a key. Publishers pass only the semantic prefix
(`…/{domain}/{source}/{modality}/{affiliation}/{entity}`); the builder appends
`{type}/{id}` from the track itself, and each publish leg appends its own view.
That is why the taxonomy lives in one function rather than at 26 publish sites.

`id` priority: `registration` → `icao24` → `mmsi` → `uid` → `callsign` →
`track_num` → `radar_id`. First match wins and becomes the object's key for its
lifetime.

Collisions worth knowing about, all handled:

- **`sapient` is both a view name and a SOURCE name.** A key like
  `…/air/sapient/acoustic/unknown/uav/…` must not have its source rewritten
  when deriving the `/raw` view, so only the FINAL segment is ever treated as
  a view.
- **`object_id` must be fully identity-derived.** Deriving only the random half
  of the ULID leaves the timestamp half moving, so two reports for one aircraft
  milliseconds apart would key differently and read as two contacts.
- **Source and modality must not be the same word.** `fused/fused` and
  `ais/ais` are meaningless; the source is named for the node
  (`trackfusion`, `nmea`) and the modality stays the method.
- **Subscribing by modality can match your own output.** `track_fusion_bridge`
  subscribes to `…/air/*/fused/**` to ingest ASTERIX CAT-062 — which also
  matches its own published tracks. It rejects its own prefix explicitly;
  without that, a fused track is re-ingested and fused with itself.

Router ACL is unchanged: data keys stay under `${DATA_TOPIC_ROOT}/**`.

## What breaks

Any existing subscriber. Nothing consumes these in production yet, so the cost
is lowest now and rises with every consumer added.

Subscribers inside the repo are already migrated: `cot_layer` and `nvg_layer`
match with `**`, which absorbs the added segment; `track_fusion_bridge` and
`cot_layer`'s radar-status subscription were rewritten to key on modality.
