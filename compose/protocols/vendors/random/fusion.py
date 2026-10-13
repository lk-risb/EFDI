#!/usr/bin/env python3
"""fusion.py — Multi-source track correlation and enrichment protocol.

Subscribes to all air track topics in the EFDI Zenoh fabric, correlates
tracks from multiple sensors, and publishes enriched fused tracks.

Fusion strategy (in priority order):
  1. ICAO 24-bit key match (exact)
       Radar (CAT-48 Mode-S) already decodes ICAO hex from SSR responses.
       Partner ADS-B and CAT-21 sources provide the same key.
       When both have the same ICAO hex, the fused track takes:
         • Position, speed, heading → from the radar (higher accuracy, lower latency)
         • callsign, registration, aircraft_type, squawk → from ADS-B (richer metadata)

  2. Squawk + altitude match
       For Mode-C transponders (no Mode-S): radar gets squawk + baro altitude.
       Match ADS-B tracks with same squawk code within 500 ft vertical tolerance.

  3. Spatial proximity match (fallback for PSR-only / non-cooperative targets)
       Radar track with no transponder: compare position against all ADS-B tracks.
       If the nearest ADS-B track is within SPATIAL_THRESHOLD_NM nautical miles
       AND the age difference is within 30 s, merge identities.
       Marks the track as "probable" rather than confirmed.

NEPTUN corroboration (non-cooperative radar tracks vs the NEPTUN feed in dangausakis_bridge.py threats):
  A radar track WITHOUT an ICAO address that lies within the NEPTUN threat's own
  uncertainty radius (clamped to FUSION_NEPTUN_MIN_NM..FUSION_NEPTUN_MAX_NM),
  agrees in heading (60 deg, when both have one) and in speed class (drone-class
  tracks <= 220 m/s, missile/ballistic/kab >= 100 m/s, when the radar reports a
  speed) is taken to be the same object — but only one-to-one: if the track fits
  more than one threat, or the threat fits more than one radar object (tracks
  sharing a cross-radar handoff primary count as one), nothing is merged. The radar track stays the marker — its remarks gain "Also
  reported by NEPTUN" — and the NEPTUN marker is suppressed by publishing
    <ORG>/air/trackfusion/suppress/neptun/<uid>
  which the dangausakis bridge's NEPTUN feed honours for FUSION_NEPTUN_SUPPRESS_S (30 s) after the
  last refresh, retracting its marker with a tombstone. Fusion never suppresses
  a radar track, and when the radar match stops (track lost, drifted apart) the
  suppression lapses and the NEPTUN marker returns. With fusion not running,
  NEPTUN simply publishes normally.

Twin suppression (duplicate suppression, whatever feeds it):

  An aircraft can reach the fabric twice with different identities: once keyed by its ICAO address
  (`icao24`, drawn as an aircraft) and once as a unit whose id or callsign merely looks like that
  address ("48A5C6" with no `icao24`, drawn as an unknown ground unit beside it). Fixing that in each
  decoder only covers the feeds somebody has seen do it, and a decoder restarted cold has not yet seen the
  keyed copy. Track fusion sees every source, so it looks for the pair and announces the loose copy on
  <ORG>/air/trackfusion/suppress/twin/<uid>; the output layers (tak_layer, sitaware_layer) hold back a
  track that has no icao24 and whose uid is announced, and retract it if it was already drawn.

  A loose track is a twin when a fresh keyed track exists for the address read from its uid or callsign, or
  carries the same callsign within TWIN_NEAR_KM. Nothing keyed by an address is ever suppressed.

  Objects with no address at all (a drone reported by a camera feed, a telemetry feed and a partner's
  situational-awareness feed, each under its own id) are matched by name instead: the distinctive last segment of
  the callsign or label ("minerva:teil-s-10:deltaquad-nvrec5" and "drone_telemetry:deltaquad-nvrec5" both read
  "deltaquad-nvrec5"), in the same dimension, within TWIN_NEAR_KM. Of such a group the copy that declares the most
  (an affiliation, then a heading or speed) is kept, ties go to the smaller uid, and the others are announced. A
  source that declares less never overrides one that declares more, and a kept copy that goes quiet is replaced
  by the next best one after TWIN_FRESH_S.

Echo filter - Recognise our own tracks (sensors, alert zones, markers) when a partner sends them back.

  A participant on the backbone fabric can ingest what this pod exports and publish it
  again under its own ids. The soc-bx feed uses object ids such as
  "SENSOR:MAINLINE-DRONU-0478D5C6" for our sensor MAINLINE-DRONU-0478D5C6; a generic JSON
  feed uses a UUID but keeps our display name ("radar-30684") as the callsign. Decoded as
  normal tracks these get a uid unlike the original, so every such sensor shows twice. The
  same happens to our alert zones: a partner's copy arrived as a ground marker called
  "DA-ZONE-UA-R:..." (our uid as its callsign) or "UA Vyshhorodskyi District YELLOW" (our
  callsign) beside the real polygon. EchoFilter remembers the ids (sensor id, uid) and names
  (sensor name, callsign) of what this pod publishes under land/**, so a decoder can drop
  those copies. A copy that carries none of our ids or names (a UUID with the tail of the UUID as
  its callsign, seen live on top of MAINLINE-DRONU-595EFFB1) is still recognised by position: it sits
  within a few metres of one of our fixed sensors.

Ghost and twin filters (ICAO bit-flip copies) - Drop ghost aircraft: the same callsign (or registration) under an ICAO address one or two bits away.

  A feed that decodes Mode-S without checking the CRC reports an occasional corrupted
  address, so one flight shows up twice (seen live: ASL124 as 4C11EC and 4C01EC, CFC3455 as
  C2C36D and 82C36D). The real address is the one seen first; a later, different address with
  the same callsign that differs by at most MAX_BITS bits is treated as a bit-flip copy.
  A registration is checked the same way as a second key: it stays the same across flights
  (a callsign does not), so it also catches a copy that carries a different callsign.

Marker memory - Keeps a marker from flipping between a declared and an undeclared identity.

  Two feeds can describe one object with the same uid, one declaring its affiliation (friendly, hostile, neutral) and
  one not. Whatever arrives last would redraw the marker as friendly, then unknown, then friendly again. MarkerMemory
  remembers, per uid, the last symbol that declared an affiliation, and keeps returning it for HOLD_S after an
  undeclared one arrives. A declared symbol always replaces the remembered one at once.

Strike merging (ground strike markers from Drone Strike Map, GeoConfirmed and any later strike feed):
  Two strike markers on the same side (the topic's friendly / hostile / ...), with the same day and within
  FUSION_STRIKE_M (100 m) of each other, are one attack. The marker with the smallest position uncertainty is
  kept (a geolocated placemark over a town centroid), ties go to the smaller uid, and the others are retracted
  with a tombstone on their own topic, which the output layers handle like any delete. A marker with role "other"
  (a tank driving, a POW) is never merged. The bridges only fetch and publish; merging and filtering is done here.

Strike side by place: a strike marker whose side was told only by where it landed (side_basis "place": Drone Strike Map, GeoConfirmed
  on damaged infrastructure) that lies inside Russian-held territory (the occupied areas, ORDLO and Crimea of DeepState, announced on
  <ORG>/land/deepstate/status/occupied) is a Ukrainian strike. A hostile one there is retracted and re-published as <uid>-UA on the
  friendly topic. Front-line proximity of Russian units is not used: units stand within a few kilometres of the front on both sides.

Cross-radar handoff (same-protocol, PSR-only targets):
  When multiple radars of the same type (e.g., two CAT-48 sites) cover overlapping
  areas, a PSR-only target crossing the boundary would otherwise create two separate
  ATAK markers. The fusion bridge prevents this by:

    Overlap zone (both radars tracking):
      Both tracks are within FUSION_HANDOFF_NM (default 2 NM) after dead-reckoning
      each to a common time, AND their headings agree within 45°.
      → Both share the primary radar's radar_id.  Single ATAK marker updated by
        whichever radar sent the most recent report.

    Handoff (target leaves primary radar's coverage):
      Primary track ages out of the cache.  The surviving secondary is promoted:
      its radar_id becomes the new stable ID.  One ATAK UID change occurs at this
      moment; the marker then stays stable under the new radar.

    Mode-S targets are unaffected — ICAO24 is a global stable key across all radars.

Config:
  FUSION_HANDOFF_NM=2.0   Max distance for cross-radar PSR association

Fused tracks are published on the object key, in every view:
  <ORG>/air/trackfusion/fused/<affiliation>/aircraft/<type>/<id>/{sapient,json,proto,raw}
→ tak_layer.py picks these up and shows them in ATAK with full identity.

Non-correlated radar tracks (truly non-cooperative, no ID possible) are
re-published as-is under:
  <ORG>/air/trackfusion/fused/unknown/aircraft/<type>/<id>/{sapient,json,proto,raw}
so they still appear in ATAK but without enrichment.

`source` is `trackfusion` (this bridge) and `modality` is `fused`; the pair is
also what keeps our own output out of the radar-source subscription — see
_OWN_PREFIX below.

Config (compose/.env):
  FUSION_SPATIAL_NM=2.0    Max distance for spatial match (default 2 NM)
  FUSION_MAX_AGE_S=60      Drop tracks older than this from the cache
  FUSION_RADAR_PREF=1      Set 0 to prefer ADS-B position (e.g. for GPS accuracy)
  FUSION_NEPTUN_MIN_NM=1.0 Smallest radius for a radar<->NEPTUN match
  FUSION_NEPTUN_MAX_NM=5.0 Largest radius (caps NEPTUN's own uncertainty)
  FUSION_NEPTUN_UAV_MAX_MS=220     Fastest radar speed still accepted for a drone-class threat
  FUSION_NEPTUN_MISSILE_MIN_MS=100 Slowest radar speed accepted for a missile / glide bomb

Run:
  venv/bin/python3 protocols/fusion.py
  venv/bin/python3 protocols/fusion.py --verbose
"""

import argparse
import json
import math
import os
import re
import threading
import time
from protocols.vendors.random.proto.normalized_track_pb2 import NormalizedTrack

from google.protobuf.message import DecodeError
from namespace_prefix import topic_root
from protocols.vendors.random.gateway import open_session, publish_collection, subscribe
from protocols.vendors.random import region_geometry
from protocols.vendors.random.track_views import add_version, semantic_topic, strip_version

TOPIC_ROOT = topic_root()

_SPATIAL_NM      = float(os.environ.get("FUSION_SPATIAL_NM",  "2.0"))
_MAX_AGE_S       = float(os.environ.get("FUSION_MAX_AGE_S",   "60"))
_RADAR_PREF      = os.environ.get("FUSION_RADAR_PREF", "1") != "0"

# Cross-radar handoff — PSR-only targets seen by multiple radars simultaneously
_HANDOFF_NM      = float(os.environ.get("FUSION_HANDOFF_NM",  "2.0"))  # spatial tolerance
_HANDOFF_HDG_TOL = 45.0   # heading difference tolerance in degrees

# NEPTUN corroboration (see module docstring)
_NEPTUN_MIN_NM   = float(os.environ.get("FUSION_NEPTUN_MIN_NM", "1.0"))
_NEPTUN_MAX_NM   = float(os.environ.get("FUSION_NEPTUN_MAX_NM", "5.0"))
_NEPTUN_HDG_TOL  = 60.0
_NEPTUN_TOPIC    = "{}/air/neptun/hostile/**".format(TOPIC_ROOT)
_SUPPRESS_TOPIC  = "{}/air/trackfusion/suppress/neptun/{{}}".format(TOPIC_ROOT)
_SUPPRESS_REFRESH_S = 5.0   # re-announce a standing match at most this often

TWIN_TTL_S = 20.0            # a layer keeps holding a loose copy back this long after the last announcement
TWIN_FRESH_S = 120.0         # a keyed or loose track counts for this long after it was last seen
TWIN_NEAR_KM = 10.0
_NAME_MIN_LEN = 8            # shorter or digit-free names ("unit", "N21") are too generic to match two objects on
_HEX = re.compile(r"^(?:[A-Za-z]+[:_-])*([0-9A-Fa-f]{6})$")      # "48A5C6", "STATES:48A5C6", "Entity:aircraft:48a5c6"
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def civil_topic(topic_root: str, icao24: str) -> str:
    return "{}/air/trackfusion/suppress/civil/{}".format(topic_root, _UNSAFE.sub("_", str(icao24).lower()))


_MILITARY_AIR = ("/friendly/aircraft/", "/hostile/aircraft/", "/neutral/aircraft/", "/mil/aircraft/")


def twin_topic(topic_root: str, uid: str) -> str:
    return "{}/air/trackfusion/suppress/twin/{}".format(topic_root, _UNSAFE.sub("_", str(uid)))


def _address(text) -> str | None:
    match = _HEX.match(str(text or "").strip())
    return match.group(1).lower() if match else None


def _callsign(track: dict) -> str:
    return str(track.get("callsign") or "").strip().upper()


def _name_key(track: dict) -> str | None:
    """The distinctive name of an object, or None when its names are too generic to match on."""
    for field in ("callsign", "label", "uid"):
        token = re.split(r"[:/]", str(track.get(field) or ""))[-1].strip().lower()
        if len(token) >= _NAME_MIN_LEN and re.search(r"\d", token):
            return token
    return None


def _dimension_of(topic: str | None) -> str:
    for part in str(topic or "").split("/"):
        if part in ("air", "land", "sea", "space"):
            return part
    return ""


def _rank(track: dict, topic: str | None) -> int:
    declared = any(part in ("friendly", "hostile", "neutral") for part in str(topic or "").split("/"))
    moving = any(track.get(key) is not None for key in ("heading_deg", "speed_mps"))
    return 2 * declared + moving


def _km(lat1, lon1, lat2, lon2) -> float:
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


class TwinIndex:
    """Fusion side: feed every track to observe(); it returns the uids of loose copies to announce."""

    def __init__(self):
        self._keyed: dict = {}      # icao24 -> (seen, lat, lon, callsign)
        self._by_call: dict = {}    # callsign -> (seen, lat, lon, icao24)
        self._loose: dict = {}      # uid -> (seen, address read from uid/callsign, lat, lon, callsign)
        self._named: dict = {}      # (dimension, name) -> {uid: (seen, lat, lon, rank)}
        self._lock = threading.Lock()

    def observe(self, track: dict, now: float | None = None, topic: str | None = None) -> list:
        now = time.time() if now is None else now
        lat, lon = track.get("lat_deg"), track.get("lon_deg")
        uid = track.get("uid") or track.get("icao24")      # an ADS-B track is keyed by its address and may carry no uid
        if (track.get("_delete") or lat is None or lon is None or not uid or track.get("geometry")
                or str(uid).endswith("-TRAIL")):
            return []
        callsign = _callsign(track)
        icao = str(track.get("icao24") or "").strip().lower()
        with self._lock:
            if icao:
                self._keyed[icao] = (now, lat, lon, callsign)
                if len(callsign) >= 4:
                    self._by_call[callsign] = (now, lat, lon, icao)
                return [u for u, entry in list(self._loose.items()) if self._is_twin(entry, now)]
            self._loose[str(uid)] = (now, _address(uid) or _address(track.get("callsign")), lat, lon, callsign)
            self._forget(now)
            if self._is_twin(self._loose[str(uid)], now):
                return [str(uid)]
            return self._named_twins(track, str(uid), lat, lon, topic, now)

    def _named_twins(self, track: dict, uid: str, lat, lon, topic, now) -> list:
        name = _name_key(track)
        if name is None:
            return []
        group = self._named.setdefault((_dimension_of(topic), name), {})
        group[uid] = (now, lat, lon, _rank(track, topic))
        for member in [m for m, v in group.items() if now - v[0] > TWIN_FRESH_S]:
            group.pop(member)
        best = min(group, key=lambda m: (-group[m][3], m))
        return [m for m, v in group.items()
                if m != best and _km(v[1], v[2], group[best][1], group[best][2]) <= TWIN_NEAR_KM]

    def _is_twin(self, entry, now) -> bool:
        seen, address, lat, lon, callsign = entry
        if now - seen > TWIN_FRESH_S:
            return False
        keyed = self._keyed.get(address) if address else None
        if keyed and now - keyed[0] <= TWIN_FRESH_S:
            return True
        same = self._by_call.get(callsign) if len(callsign) >= 4 else None
        return bool(same and now - same[0] <= TWIN_FRESH_S and _km(lat, lon, same[1], same[2]) <= TWIN_NEAR_KM)

    def _forget(self, now) -> None:
        for table in (self._keyed, self._by_call, self._loose):
            for key in [k for k, v in table.items() if now - v[0] > 3600]:
                table.pop(key, None)
        for key in [k for k, group in self._named.items() if all(now - v[0] > 3600 for v in group.values())]:
            self._named.pop(key, None)


class TwinSuppressions:
    """Layer side: on_sample() for the announcements, is_suppressed(track) before drawing a track; on_military() for the
    announcements of military aircraft, is_civil_copy(track, topic) before drawing a civil track."""

    def __init__(self, ttl_s: float = TWIN_TTL_S):
        self._ttl = ttl_s
        self._until: dict = {}
        self._military: dict = {}       # icao24 -> time the announcement runs out
        self._lock = threading.Lock()

    def on_sample(self, sample) -> None:
        try:
            uid = json.loads(bytes(sample.payload).decode()).get("uid")
        except (ValueError, UnicodeDecodeError, AttributeError):
            return
        if uid:
            with self._lock:
                self._until[str(uid)] = time.time() + self._ttl

    def on_military(self, sample) -> None:
        try:
            icao = str(json.loads(bytes(sample.payload).decode()).get("icao24") or "").lower()
        except (ValueError, UnicodeDecodeError, AttributeError):
            return
        if icao:
            with self._lock:
                self._military[icao] = time.time() + self._ttl

    def is_civil_copy(self, track: dict, topic: str, now: float | None = None) -> bool:
        """A civil track of an aircraft that another feed reports as military: not drawn, it would turn the marker civil."""
        icao = str(track.get("icao24") or "").lower()
        if not icao or "/civ/aircraft" not in topic:
            return False
        now = time.time() if now is None else now
        with self._lock:
            return self._military.get(icao, 0.0) > now

    def is_suppressed(self, track: dict, now: float | None = None) -> bool:
        if track.get("icao24") or not track.get("uid"):
            return False
        now = time.time() if now is None else now
        with self._lock:
            return self._until.get(str(track["uid"]), 0.0) > now


OCCUPIED_TOPIC = "{}/land/deepstate/status/occupied".format(TOPIC_ROOT)
OCCUPIED_TTL_S = 26 * 3600.0         # the DeepState bridge re-announces the occupied territory every 10 minutes


# Strike markers (see "Strike merging" in the module docstring)
STRIKE_NEAR_M = max(0, int(os.environ.get("FUSION_STRIKE_M", "100")))
STRIKE_FRESH_S = 3 * 3600.0          # a marker counts this long after it was last seen (bridges re-send every poll)
STRIKE_SOURCES = ("/land/dronestrikemap/strike/", "/land/geoconfirmed/event/")
_SIDE = re.compile(r"/(friendly|hostile|neutral|unknown)/")
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


def is_strike_topic(topic: str) -> bool:
    return any(source in topic for source in STRIKE_SOURCES)


def _metres(a, b) -> float:
    dlat = (a[0] - b[0]) * 111320.0
    dlon = (a[1] - b[1]) * 111320.0 * math.cos(math.radians(a[0]))
    return math.hypot(dlat, dlon)


def _day(track: dict) -> str:
    match = _DAY.search(str(track.get("event_date") or "")) or _DAY.search(str(track.get("uid") or ""))
    return match.group(0) if match else time.strftime("%Y-%m-%d", time.gmtime(track.get("_ts") or time.time()))


class StrikeIndex:
    """observe(track, now, topic) → [(uid, topic, type)] of the strike markers to retract now (empty for anything else)."""

    def __init__(self):
        self._groups: dict = {}      # (side, day) -> {uid: (seen, lat, lon, uncertainty, topic, type)}
        self._lock = threading.Lock()

    def observe(self, track: dict, now: float | None = None, topic: str | None = None) -> list:
        side = _SIDE.search(topic or "")
        uid = str(track.get("uid") or "")
        if not (topic and side and uid and is_strike_topic(topic)) or track.get("role") == "other":
            return []
        now = time.time() if now is None else now
        with self._lock:
            if track.get("_delete"):
                for group in self._groups.values():
                    group.pop(uid, None)
                return []
            try:
                lat, lon = float(track["lat_deg"]), float(track["lon_deg"])
                uncertainty = float(track.get("position_uncertainty_m") or 0)
            except (KeyError, TypeError, ValueError):
                return []
            for key in [k for k, g in self._groups.items() if all(now - v[0] > STRIKE_FRESH_S for v in g.values())]:
                self._groups.pop(key)
            group = self._groups.setdefault((side.group(1), _day(track)), {})
            group[uid] = (now, lat, lon, uncertainty, topic, str(track.get("type") or "strike"))
            for member in [m for m, v in group.items() if now - v[0] > STRIKE_FRESH_S]:
                group.pop(member)
            kept, retract = [], []
            for member in sorted(group, key=lambda m: (group[m][3], m)):
                if any(_metres(group[member][1:3], group[k][1:3]) <= STRIKE_NEAR_M for k in kept):
                    retract.append((member, group[member][4], group[member][5]))
                else:
                    kept.append(member)
            return retract


# --- Echo filter ---

# Shorter ids could appear inside an unrelated uid by accident. 10 keeps our shortest real ids (MAPA-323725,
# 11 characters) while an ICAO address (6) or a bare number still cannot match by accident.
MIN_ID_LEN = 10
# Shorter names are too generic to treat as ours, and the bridge's own fallback name
# ("dronu-sensor") says nothing about which sensor it is.
MIN_NAME_LEN = 8
# A fixed sensor of ours and a partner's copy of it share coordinates, so a partner object within
# POSITION_CELL_DEG (about 11 m, checked with its neighbouring cells) of one counts as its copy.
POSITION_CELL_DEG = 1e-4
# A partner can keep re-sending a copy of a threat of ours long after our own track expired (a missile frozen at its
# last position, stamped fresh), so what we published is remembered for hours, not minutes.
_MAX_REMEMBERED = 50000
_GENERIC_NAMES = {"dronu-sensor", "sensor", "unknown"}
# Operators can drop a partner's test or junk objects without a code change: a regex matched
# against uid, callsign and label of each backbone record (restart socbx / generic_json after setting it).
_DROP = re.compile(os.environ["BACKBONE_DROP_REGEX"], re.I) if os.environ.get("BACKBONE_DROP_REGEX") else None


# Topics whose tracks this pod publishes and a partner could send back: every land track, and the
# drones, missiles and bombs from the open-data bridges (they have no ICAO address to merge on), and their
# trail lines (a partner's copy of a trail arrives as a point marker).
OWN_TOPICS = ("land/**", "air/*/hostile/**", "air/*/trail/**")


def subscribe_own(session, topic_root: str, echoes: "EchoFilter", subscribe) -> list:
    """Declare the subscriptions that feed `echoes`; returns them so the caller can undeclare."""
    return [subscribe(session, "{}/{}".format(topic_root, pattern), echoes.on_sample) for pattern in OWN_TOPICS]


# Force tracks of the TAK users that nffi_layer.py and ffi_layer.py put into SitaWare. SitaWare sends them back through an NFFI or FFI
# server whose source layer holds them (the EFDI-TAK layer), and the NFFI and FFI decoders drop them here, so a TAK user does not come
# back into TAK as a second marker, while a partner pod's users (another EFDI pod, or any other system) are kept. Two marks tell ours:
#   - the source system is "EFDI-" plus THIS pod's namespace (own_force_track_system), not just "EFDI": every EFDI pod writes the same
#     product name, and a partner pod's tracks must not be taken for ours; the subsystem is TAK;
#   - the transponderId is a TAK uid that this pod's own bridge published (EchoFilter.has_id): it needs no tag at all, so it also
#     catches tracks sent before the tag existed or through a layer that does not keep it.
OWN_FORCE_TRACK_SUBSYSTEM = "TAK"


def own_force_track_system() -> str:
    """The source system of this pod's force tracks: "EFDI-" and the pod's namespace (LTU/CISB/zenoh_gateway -> EFDI-LTU-CISB-ZENOH-GATEWAY),
    cut to the 30 characters the NFFI field takes and to its [A-Z0-9-] pattern."""
    identity = os.environ.get("PARTNER_NAMESPACE", "").strip("/") or topic_root()          # the pod's own namespace, not the shared prefix
    tag = re.sub(r"[^A-Z0-9]+", "-", identity.upper()).strip("-")
    return ("EFDI-" + tag).rstrip("-")[:30] if tag else "EFDI"


def is_own_force_track(track: dict, echoes: "EchoFilter | None" = None) -> bool:
    """True for a decoded NFFI/FFI track that this pod itself put into SitaWare: its source system is this pod's and its subsystem TAK,
    or its transponderId is an id this pod published (`echoes`, an EchoFilter fed with subscribe_own)."""
    if (str(track.get("source_system") or "").upper(), str(track.get("source_subsystem") or "").upper()) == (
            own_force_track_system(), OWN_FORCE_TRACK_SUBSYSTEM):
        return True
    return bool(echoes is not None and track.get("transponder_id") and echoes.has_id(track["transponder_id"]))


class EchoFilter:
    def __init__(self, ttl_s: float = 12 * 3600.0):
        self._ttl = ttl_s
        self._ids: dict = {}
        self._names: dict = {}
        self._cells: dict = {}
        self._lock = threading.Lock()

    def on_sample(self, sample) -> None:
        """Zenoh callback for this pod's own tracks under land/**. Records that came in from the
        backbone are not ours, and tombstones carry nothing to remember."""
        try:
            data = json.loads(bytes(sample.payload).decode())
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(data, dict) or data.get("_delete"):
            return
        if str(data.get("_src") or "").startswith("backbone:"):
            return
        now = time.time()
        ids = {str(data[k]).upper() for k in ("sensor_id", "uid") if data.get(k)}
        names = {str(data[k]).strip().lower() for k in ("sensor_name", "callsign") if data.get(k)}
        with self._lock:
            if data.get("sensor_id") and data.get("lat_deg") is not None and data.get("lon_deg") is not None:
                self._cells[self._cell(data["lat_deg"], data["lon_deg"])] = now
            for identifier in ids:
                self._ids[identifier] = now
            for name in names:
                if len(name) >= MIN_NAME_LEN and name not in _GENERIC_NAMES:
                    self._names[name] = now
            for table in (self._ids, self._names):
                if len(table) > _MAX_REMEMBERED:        # forget the oldest first
                    for stale in sorted(table, key=table.get)[:len(table) - _MAX_REMEMBERED]:
                        del table[stale]

    @staticmethod
    def _cell(lat, lon) -> tuple:
        return (int(float(lat) / POSITION_CELL_DEG), int(float(lon) / POSITION_CELL_DEG))

    def _at_a_sensor(self, record: dict, now: float) -> bool:
        if record.get("icao24") or record.get("lat_deg") is None or record.get("lon_deg") is None:
            return False
        try:
            row, col = self._cell(record["lat_deg"], record["lon_deg"])
        except (TypeError, ValueError):
            return False
        with self._lock:
            return any(now - self._cells.get((row + dr, col + dc), float("-inf")) <= self._ttl
                       for dr in (-1, 0, 1) for dc in (-1, 0, 1))

    def has_id(self, identifier, now: float | None = None) -> bool:
        """True when `identifier` is, exactly, an id (uid or sensor id) this pod published lately."""
        key = str(identifier or "").upper()
        now = time.time() if now is None else now
        with self._lock:
            return len(key) >= MIN_ID_LEN and now - self._ids.get(key, float("-inf")) <= self._ttl

    def is_echo(self, record: dict, now: float | None = None) -> bool:
        """True when the record's uid, callsign or label embeds one of our ids (a partner copy often
        uses our uid as its callsign), or its callsign or label is one of our names."""
        if _DROP is not None and any(_DROP.search(str(record.get(k) or "")) for k in ("uid", "callsign", "label")):
            return True
        uid = str(record.get("uid") or "").upper()
        labels = {str(record.get(k) or "").strip().lower() for k in ("callsign", "label")} - {""}
        now = time.time() if now is None else now
        with self._lock:
            ids = [i for i, seen in self._ids.items() if now - seen <= self._ttl and len(i) >= MIN_ID_LEN]
            names = {n for n, seen in self._names.items() if now - seen <= self._ttl}
        texts = {uid} | {label.upper() for label in labels}
        return any(i in text for i in ids for text in texts) or bool(labels & names) or self._at_a_sensor(record, now)


# --- Ghost and twin filters (ICAO bit-flip copies) ---

MAX_BITS = 2
WINDOW_S = 600.0


class GhostFilter:
    def __init__(self, window_s: float = WINDOW_S):
        self._window = window_s
        self._seen: dict = {}           # "C:callsign" / "R:registration" -> {icao24: last seen}
        self._lock = threading.Lock()

    def is_ghost(self, icao24: str, callsign: str, now: float | None = None, registration: str | None = None) -> bool:
        """Register (icao24, callsign, registration) and say whether it is a bit-flip copy of an
        address already seen with the same callsign or the same registration. Records with
        neither are never ghosts."""
        keys = [prefix + value for prefix, value in (("C:", (callsign or "").strip().upper()),
                                                     ("R:", (registration or "").strip().upper())) if value]
        if not keys or not icao24:
            return False
        now = time.time() if now is None else now
        address = int(icao24, 16)
        ghost = False
        with self._lock:
            known_by_key = {k: {a: t for a, t in self._seen.get(k, {}).items() if now - t <= self._window} for k in keys}
            for known in known_by_key.values():
                if address not in known and any(bin(address ^ other).count("1") <= MAX_BITS for other in known):
                    ghost = True
            for key, known in known_by_key.items():
                if address in known or not ghost:
                    known[address] = now
                self._seen[key] = known
            if len(self._seen) > 20000:     # bound the memory on a long run
                self._seen = {c: v for c, v in self._seen.items() if v}
        return ghost


class TwinFilter:
    """Drop a second record for the same aircraft that has no ICAO address.

    A partner feed also sends an aircraft under a UUID (seen live: "OMCBS" as a ground marker
    beside the ICAO-keyed "OMCBS"). A record without icao24 whose callsign equals that of an
    ICAO-keyed record seen in the last WINDOW_S seconds within RADIUS_KM is that aircraft's twin.
    """

    WINDOW_S = 180.0
    RADIUS_KM = 50.0            # an airliner covers about 45 km in WINDOW_S, and the UUID copy is sent only every few minutes

    def __init__(self):
        self._seen: dict = {}           # callsign -> (lat, lon, time) of an ICAO-keyed record
        self._lock = threading.Lock()

    def is_twin(self, record: dict, now: float | None = None) -> bool:
        callsign = str(record.get("callsign") or "").strip().upper()
        lat, lon = record.get("lat_deg"), record.get("lon_deg")
        if len(callsign) < 3 or lat is None or lon is None:
            return False
        now = time.time() if now is None else now
        with self._lock:
            if record.get("icao24"):
                self._seen[callsign] = (lat, lon, now)
                if len(self._seen) > 20000:
                    self._seen = {c: v for c, v in self._seen.items() if now - v[2] <= self.WINDOW_S}
                return False
            seen = self._seen.get(callsign)
        if not seen or now - seen[2] > self.WINDOW_S:
            return False
        km = ((lat - seen[0]) * 111.0) ** 2 + ((lon - seen[1]) * 111.0 * max(0.1, math.cos(math.radians(lat)))) ** 2
        return km <= self.RADIUS_KM ** 2


# --- Marker memory ---

HOLD_S = 300.0
_UNDECLARED_COT = "u", "p"            # a-<affiliation>-...: unknown, pending
_UNDECLARED_SIDC = "U", "P"           # S<affiliation>...: unknown, pending


def _declared(code: str) -> bool | None:
    """Whether a CoT type (a-f-A) or 2525B SIDC (SFAP------*****) declares an affiliation; None if it is neither."""
    if code.startswith("a-") and len(code) > 2:
        return code[2] not in _UNDECLARED_COT
    if len(code) >= 2 and code[0] == "S":
        return code[1] not in _UNDECLARED_SIDC
    return None


class MarkerMemory:
    def __init__(self, hold_s: float = HOLD_S):
        self._hold = hold_s
        self._last: dict = {}           # uid -> (seen, code that declared an affiliation)
        self._lock = threading.Lock()

    def settle(self, uid: str, code: str, now: float | None = None) -> str:
        """The code to draw for uid, given the one its latest message resolved to."""
        declared = _declared(code)
        if declared is None:
            return code
        now = time.time() if now is None else now
        with self._lock:
            if declared:
                self._last[uid] = (now, code)
                return code
            seen, kept = self._last.get(uid, (0.0, None))
            if kept is not None and now - seen <= self._hold:
                return kept
            if len(self._last) > 20000:
                self._last = {u: v for u, v in self._last.items() if now - v[0] <= self._hold}
            return code


TOPIC_FUSED = "{}/air/trackfusion/fused/{}/aircraft"

# Everything this bridge publishes lives under here — used to reject our own
# output when re-subscribing by modality.
_OWN_PREFIX = "{}/air/trackfusion/".format(TOPIC_ROOT)

# Radar and ADS-B are separated by MODALITY, not by source name. The source
# segment is a wildcard because a radar names itself by SAC/SIC, so its topic
# is not knowable at startup — and two radars must both be picked up.
#
# This split is the whole point of the bridge: ASTERIX CAT-048 (radar) is the
# positional authority, CAT-021 (ADS-B relayed by a ground station) only
# enriches identity. While both published under the literal `asterix` these
# two lists were identical and every ASTERIX track was fed in as both.

# Positional authority
_RADAR_TOPICS = [
    "{}/air/*/radar/**".format(TOPIC_ROOT),
    "{}/air/*/mlat/**".format(TOPIC_ROOT),
    "{}/air/*/fused/**".format(TOPIC_ROOT),
    "{}/air/stanag_4586/telemetry/**".format(TOPIC_ROOT),
]

# Identity enrichment only
_ADSB_TOPICS = [
    "{}/air/*/adsb/**".format(TOPIC_ROOT),
]

# Fields that carry identity (we prefer ADS-B values for these)
_ID_FIELDS = ("callsign", "registration", "aircraft_type", "icao24",
              "squawk", "origin", "destination", "operator",
              "route", "rssi_db", "emitter_category_str", "on_ground")

# ADS-B fields taken as supplement only when radar cannot provide them
_ADSB_SUPPLEMENT = ("vertical_rate_ms",)

# Fields where radar is the authority (position, kinematics)
_RADAR_FIELDS = ("lat_deg", "lon_deg", "alt_m", "alt_baro_ft", "alt_3d_ft",
                 "speed_ms", "heading_deg", "range_nm", "azimuth_deg",
                 "range_nm", "sac", "sic", "track_num", "radar_id", "tod_s")


def _extrapolate_pos(lat, lon, speed_ms, heading_deg, dt_s):
    """Dead-reckon lat/lon forward by dt_s seconds. Returns (lat, lon) unchanged if no speed."""
    if lat is None or lon is None or not speed_ms:
        return lat, lon
    d  = speed_ms * dt_s
    R  = 6_371_000.0
    az = math.radians(heading_deg or 0)
    la = math.radians(lat)
    lo = math.radians(lon)
    la2 = math.asin(math.sin(la) * math.cos(d / R) +
                    math.cos(la) * math.sin(d / R) * math.cos(az))
    lo2 = lo + math.atan2(math.sin(az) * math.sin(d / R) * math.cos(la),
                          math.cos(d / R) - math.sin(la) * math.sin(la2))
    return math.degrees(la2), math.degrees(lo2)


def _haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 3440.065   # Earth radius in NM
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _heading_diff(a, b) -> float:
    diff = abs(a - b) % 360
    return 360 - diff if diff > 180 else diff


_NEPTUN_MISSILE_TYPES = frozenset({"missile", "ballistic", "kab"})
_NEPTUN_UAV_TYPES = frozenset({"uav", "fpv", "recon"})
# Jet-powered drones reach 600-700 km/h (167-194 m/s), so the drone ceiling sits
# above that (220 m/s ~ 790 km/h). The two ranges overlap on purpose: each is only
# a plausibility check against the threat's own declared type, not a classifier.
_NEPTUN_UAV_MAX_MS = float(os.environ.get("FUSION_NEPTUN_UAV_MAX_MS", "220"))
_NEPTUN_MISSILE_MIN_MS = float(os.environ.get("FUSION_NEPTUN_MISSILE_MIN_MS", "100"))


def _neptun_pair_ok(radar: dict, threat: dict) -> bool:
    """Could this radar track be that NEPTUN threat? Position inside the threat's
    own uncertainty radius, heading and speed class compatible. Transponder
    (ICAO) tracks never qualify. A missing heading or speed is not held against
    the pair."""
    if radar.get("icao24") or radar.get("lat_deg") is None or radar.get("lon_deg") is None:
        return False
    if threat.get("lat_deg") is None or threat.get("lon_deg") is None:
        return False
    radius = min(max((threat.get("position_uncertainty_m") or 0) / 1852.0, _NEPTUN_MIN_NM), _NEPTUN_MAX_NM)
    if _haversine_nm(radar["lat_deg"], radar["lon_deg"], threat["lat_deg"], threat["lon_deg"]) > radius:
        return False
    r_hdg, n_hdg = radar.get("heading_deg"), threat.get("heading_deg")
    if r_hdg is not None and n_hdg is not None and _heading_diff(r_hdg, n_hdg) > _NEPTUN_HDG_TOL:
        return False
    speed = radar.get("speed_ms")
    kind = (threat.get("type") or "").lower()
    if speed and kind in _NEPTUN_MISSILE_TYPES:
        return speed >= _NEPTUN_MISSILE_MIN_MS
    if speed and kind in _NEPTUN_UAV_TYPES:
        return speed <= _NEPTUN_UAV_MAX_MS
    return True   # mig31k / unknown: no speed class to check against


def match_neptun(radar: dict, radar_uid: str, radar_cache: dict, neptun: dict, now: float,
                 ident=lambda uid: uid, max_age_s: float = None):
    """uid of the NEPTUN threat this radar track corroborates, or None.

    One-to-one only: the radar track must be compatible with exactly ONE fresh
    threat, and that threat with exactly ONE radar object (cache entries that
    `ident` maps to the same cross-radar primary count as one object). Anything
    ambiguous returns None, so NEPTUN stays visible rather than risk hiding a
    threat behind a wrong pairing.

    `radar_cache` and `neptun` are {uid: {"track": dict, "ts": float}}.
    """
    max_age_s = _MAX_AGE_S if max_age_s is None else max_age_s
    fresh = [(uid, e["track"]) for uid, e in neptun.items() if now - e["ts"] <= max_age_s]
    candidates = [(uid, t) for uid, t in fresh if _neptun_pair_ok(radar, t)]
    if len(candidates) != 1:
        return None
    threat_uid, threat = candidates[0]
    me = ident(radar_uid)
    for other_uid, entry in radar_cache.items():
        if now - entry["ts"] > max_age_s or ident(other_uid) == me:
            continue
        if _neptun_pair_ok(entry["track"], threat):
            return None
    return threat_uid


def _uid_of(track: dict) -> str:
    for f in ("icao24", "uid", "track_num", "radar_id", "mmsi"):
        v = track.get(f)
        if v:
            return str(v)
    return "unknown"


def _aff_of(track: dict, topic: str) -> str:
    parts = topic.split("/")
    for p in parts:
        if p in ("friendly", "hostile", "neutral", "unknown", "civ", "mil"):
            return p if p in ("friendly", "hostile", "neutral") else "unknown"
    return "unknown"


class TrackFuser:
    def __init__(self, session: "zenoh.Session", verbose: bool):
        self._session = session
        self._verbose = verbose
        self._lock    = threading.Lock()
        # uid → {track, topic, ts}
        self._radar_tracks: dict[str, dict] = {}
        self._adsb_tracks:  dict[str, dict] = {}
        # icao24 → uid in adsb_tracks
        self._adsb_by_icao:   dict[str, str] = {}
        # icao24 → uid in radar_tracks (for fast coverage check in on_adsb)
        self._radar_by_icao:  dict[str, str] = {}
        # squawk → list of adsb uids
        self._adsb_by_squawk: dict[str, list] = {}
        # PSR cross-radar handoff: uid → primary_uid (stable ID across radar boundaries)
        # Key = any radar uid; value = whichever radar uid was first to own this target.
        # When the primary ages out, the surviving secondary is promoted automatically.
        self._radar_primary: dict[str, str] = {}
        # NEPTUN threats seen on the fabric, and when each suppression was last announced
        self._neptun_tracks: dict[str, dict] = {}
        self._suppress_sent: dict[str, float] = {}
        # Loose copies of an ICAO-keyed aircraft (see "Twin suppression" above) and when each was last announced
        self._twins = TwinIndex()
        self._twin_sent: dict[str, float] = {}
        # Russian-held territory (rings, announced by the DeepState bridge), to put a strike told only by its place on the right side
        self._occupied: list = []
        self._occupied_seen = 0.0
        # Strike markers reported twice (see "Strike merging")
        self._strikes = StrikeIndex()
        # Periodic age-out: ensures _radar_by_icao is cleaned even when radar goes
        # silent (no on_radar() calls), so ADS-B fallback kicks in automatically.
        self._start_age_timer()

    # ------------------------------------------------------------------
    # Ingest handlers
    # ------------------------------------------------------------------

    def on_radar(self, sample):
        topic = str(sample.key_expr)
        # A track is published in four views; the fusion model is dict-based, so
        # only the JSON view is consumed. The `**` subscription matches all four,
        # so the other three are skipped here rather than left to fail json.loads.
        if not topic.endswith("/tracks/v1"):
            return
        base = strip_version(topic)
        if base.rsplit("/", 1)[-1] in {"sapient", "proto", "raw"}:
            return
        # _RADAR_TOPICS matches `/air/*/fused/**` to pick up ASTERIX CAT-062
        # system tracks — which also matches THIS bridge's own output, so a
        # fused track would be re-ingested as a radar source and fused with
        # itself. Drop our own publications before anything else.
        if topic.startswith(_OWN_PREFIX):
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, DecodeError):
            return
        self._age_out()
        now = time.time()
        with self._lock:
            uid = _uid_of(track)
            self._radar_tracks[uid] = {"track": track, "topic": topic, "ts": now}
            icao = track.get("icao24", "").strip().lower()
            if icao:
                self._radar_by_icao[icao] = uid

            # Cross-radar handoff for PSR-only targets
            primary_uid = self._cross_radar_associate(track, uid, now)
            if primary_uid != uid:
                # Borrow the primary radar's radar_id so tak_layer produces the
                # same ATAK UID throughout the overlap and across the handoff.
                primary_entry = self._radar_tracks.get(primary_uid)
                if primary_entry and primary_entry["track"].get("radar_id"):
                    track = dict(track)
                    track["radar_id"] = primary_entry["track"]["radar_id"]
                    if self._verbose:
                        print("HANDOFF {} → {}".format(
                            uid, primary_uid), flush=True)

        self._fuse_and_publish(track, topic)

    def _start_age_timer(self):
        self._age_out()
        t = threading.Timer(_MAX_AGE_S / 2, self._start_age_timer)
        t.daemon = True
        t.start()

    # ------------------------------------------------------------------
    # Cross-radar handoff (PSR-only targets)
    # ------------------------------------------------------------------

    def _cross_radar_associate(self, track: dict, uid: str, now: float) -> str:
        """Called under self._lock.

        For PSR-only targets (no ICAO24): search all other-radar tracks for a
        spatially + kinematically consistent match.  If found, both tracks share
        the same primary uid → same radar_id in the fused output → single ATAK
        marker throughout the overlap zone and across the handoff boundary.

        Returns the stable primary uid to use for fused publication.
        """
        # Mode-S: already stable by ICAO — no cross-radar association needed
        if track.get("icao24"):
            return uid

        r_lat = track.get("lat_deg")
        r_lon = track.get("lon_deg")
        if r_lat is None or r_lon is None:
            return self._radar_primary.setdefault(uid, uid)

        r_sac = track.get("sac")
        r_sic = track.get("sic")
        r_hdg = track.get("heading_deg")

        best_d       = _HANDOFF_NM
        best_primary = None

        for other_uid, entry in self._radar_tracks.items():
            if other_uid == uid:
                continue
            other = entry["track"]
            # Same radar — skip (different tracks on the same sensor are not handoffs)
            if other.get("sac") == r_sac and other.get("sic") == r_sic:
                continue
            # Mode-S tracks have their own stable ID — don't pull them into PSR handoff
            if other.get("icao24"):
                continue
            dt = now - entry["ts"]
            if dt > _MAX_AGE_S:
                continue

            # Extrapolate the other track forward to align timestamps
            o_lat, o_lon = _extrapolate_pos(
                other.get("lat_deg"), other.get("lon_deg"),
                other.get("speed_ms", 0) or 0,
                other.get("heading_deg", 0) or 0,
                dt)
            if o_lat is None:
                continue

            d = _haversine_nm(r_lat, r_lon, o_lat, o_lon)
            if d >= best_d:
                continue

            # Heading consistency gate (skip if headings diverge > tolerance)
            o_hdg = other.get("heading_deg")
            if r_hdg is not None and o_hdg is not None:
                diff = abs(r_hdg - o_hdg) % 360
                if diff > 180:
                    diff = 360 - diff
                if diff > _HANDOFF_HDG_TOL:
                    continue

            best_d       = d
            best_primary = self._radar_primary.get(other_uid, other_uid)

        if best_primary is not None:
            self._radar_primary[uid] = best_primary
        else:
            self._radar_primary.setdefault(uid, uid)

        return self._radar_primary[uid]

    def on_neptun(self, sample):
        topic = str(sample.key_expr)
        if not topic.endswith("/tracks/v1"):
            return
        if strip_version(topic).rsplit("/", 1)[-1] in {"sapient", "proto", "raw"}:
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, DecodeError):
            return
        uid = track.get("uid")
        if not uid:
            return
        with self._lock:
            if track.get("_delete"):
                self._neptun_tracks.pop(uid, None)
            else:
                self._neptun_tracks[uid] = {"track": track, "ts": time.time()}

    def _corroborate_with_neptun(self, fused: dict, radar: dict) -> None:
        """If a NEPTUN threat matches this radar track one-to-one, annotate `fused`
        and tell the dangausakis bridge to hold back that threat's marker."""
        now = time.time()
        radar_uid = _uid_of(radar)
        with self._lock:
            for uid in [u for u, e in self._neptun_tracks.items() if now - e["ts"] > _MAX_AGE_S]:
                del self._neptun_tracks[uid]
            uid = match_neptun(radar, radar_uid, self._radar_tracks, self._neptun_tracks, now,
                               ident=lambda u: self._radar_primary.get(u, u))
            threat = self._neptun_tracks[uid]["track"] if uid else None
            announce = bool(uid) and now - self._suppress_sent.get(uid, 0) >= _SUPPRESS_REFRESH_S
            if announce:
                self._suppress_sent[uid] = now
        if not uid:
            return
        label = "Also reported by NEPTUN ({} {})".format(
            threat.get("type", "threat"), threat.get("locality") or threat.get("region") or "").strip()
        fused["remarks"] = "{}; {}".format(fused["remarks"], label) if fused.get("remarks") else label
        if announce:
            self._session.put(_SUPPRESS_TOPIC.format(uid),
                              json.dumps({"uid": uid, "radar_uid": radar_uid, "ts": now}).encode())
            if self._verbose:
                print("NEPTUN match {} ~ radar {}".format(uid, radar_uid), flush=True)

    def on_occupied(self, sample):
        try:
            rings = json.loads(bytes(sample.payload).decode())["rings"]
        except (ValueError, UnicodeDecodeError, KeyError, TypeError):
            return
        if isinstance(rings, list):
            with self._lock:
                self._occupied, self._occupied_seen = [{"rings": [r]} for r in rings if isinstance(r, list)], time.time()

    def _in_occupied(self, lat, lon, now: float) -> bool:
        with self._lock:
            regions = self._occupied if now - self._occupied_seen <= OCCUPIED_TTL_S else []
        return any(region_geometry.contains(region, lon, lat) for region in regions)

    def _reside(self, track: dict, topic: str, now: float) -> None:
        """A strike marker told only by its place that lands in Russian-held territory is a Ukrainian strike: retract the hostile
        marker and publish it on the friendly topic as <uid>-UA (a different uid, so the two cannot undo each other)."""
        uid = str(track.get("uid") or "")
        with self._lock:
            if now - self._twin_sent.get("reside:" + uid, 0) < _SUPPRESS_REFRESH_S:
                return
            self._twin_sent["reside:" + uid] = now
        parts = strip_version(topic).split("/")
        prefix = "/".join(parts[:-2]).replace("/hostile/", "/friendly/", 1)
        moved = dict(track, uid=uid + "-UA", _fused=True, _ts=now, side_basis="occupied",
                     remarks=str(track.get("remarks") or "") + " Landed in Russian-held territory (DeepState): a Ukrainian strike.")
        self._session.put(add_version(semantic_topic(prefix, moved)), json.dumps(moved).encode())
        self._session.put(topic, json.dumps({"uid": uid, "type": track.get("type", "strike"), "_delete": True, "_ts": now,
                                             "_src": "trackfusion"}).encode())
        if self._verbose:
            print("strike {} landed in occupied territory: friendly".format(uid), flush=True)

    def on_any_track(self, sample):
        """Every track on the fabric, whatever its source: announce the loose copies of aircraft that
        another feed already reports by their ICAO address, so the output layers draw one marker."""
        topic = str(sample.key_expr)
        if not topic.endswith("/tracks/v1") or topic.startswith(_OWN_PREFIX):
            return
        if strip_version(topic).rsplit("/", 1)[-1] in {"sapient", "proto", "raw"}:
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(track, dict):
            return
        now = time.time()
        if (track.get("side_basis") == "place" and not track.get("_fused") and not track.get("_delete")
                and "/hostile/" in topic and is_strike_topic(topic)):
            try:
                lat, lon = float(track["lat_deg"]), float(track["lon_deg"])
            except (KeyError, TypeError, ValueError):
                lat = lon = None
            if lat is not None and self._in_occupied(lat, lon, now):
                self._reside(track, topic, now)
                return
        icao = str(track.get("icao24") or "").lower()
        if icao and any(part in topic for part in _MILITARY_AIR):      # military by another feed: civil copies are held back
            with self._lock:
                due = now - self._twin_sent.get("civil:" + icao, 0) >= _SUPPRESS_REFRESH_S
                if due:
                    self._twin_sent["civil:" + icao] = now
            if due:
                self._session.put(civil_topic(TOPIC_ROOT, icao), json.dumps({"icao24": icao, "ts": now}).encode())
        for uid, strike_topic, strike_type in self._strikes.observe(track, now, topic):
            with self._lock:
                if now - self._twin_sent.get(uid, 0) < _SUPPRESS_REFRESH_S:
                    continue
                self._twin_sent[uid] = now
            self._session.put(strike_topic, json.dumps({"uid": uid, "type": strike_type, "_delete": True,
                                                        "_ts": now, "_src": "trackfusion"}).encode())
            if self._verbose:
                print("strike merged, retracted: {}".format(uid), flush=True)
        for uid in self._twins.observe(track, now, topic):
            with self._lock:
                if now - self._twin_sent.get(uid, 0) < _SUPPRESS_REFRESH_S:
                    continue
                self._twin_sent[uid] = now
            self._session.put(twin_topic(TOPIC_ROOT, uid), json.dumps({"uid": uid, "ts": now}).encode())
            if self._verbose:
                print("twin held back: {}".format(uid), flush=True)

    def on_adsb(self, sample):
        topic = str(sample.key_expr)
        # Enrichment tracks are consumed from the JSON view only — see on_radar.
        # The old `/v2` protobuf branch keyed on a view name that no longer
        # exists (the typed view is `/proto` now), so it was dead: parsing it
        # here as well would double-ingest every track.
        if not topic.endswith("/json"):
            return
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, DecodeError):
            return
        self._age_out()   # clean stale radar entries before checking coverage
        radar_covers = False
        with self._lock:
            uid = _uid_of(track)
            self._adsb_tracks[uid] = {"track": track, "topic": topic, "ts": time.time()}
            icao = track.get("icao24", "").strip().lower()
            if icao:
                self._adsb_by_icao[icao] = uid
                radar_covers = icao in self._radar_by_icao
            sq = track.get("squawk", "")
            if sq and sq not in ("0000", "7500", "7600", "7700"):
                self._adsb_by_squawk.setdefault(sq, [])
                if uid not in self._adsb_by_squawk[sq]:
                    self._adsb_by_squawk[sq].append(uid)

        if not radar_covers:
            # No radar covering this aircraft — publish ADS-B track as fallback so
            # it appears in ATAK. When radar picks it up, the fused track takes over
            # seamlessly (same ICAO-based UID, same marker in ATAK).
            affiliation = "mil" if track.get("is_military") else "civ"
            pub_topic = TOPIC_FUSED.format(TOPIC_ROOT, affiliation)
            publish_collection(self._session, pub_topic, track, NormalizedTrack)
            if self._verbose:
                ident = track.get("callsign") or track.get("icao24") or "?"
                print("ADSB fallback [no radar] {}".format(ident), flush=True)

    # ------------------------------------------------------------------
    # Fusion logic
    # ------------------------------------------------------------------

    def _fuse_and_publish(self, radar: dict, radar_topic: str):
        adsb = self._find_match(radar)
        if adsb is not None:
            fused, method = self._merge(radar, adsb)
        else:
            fused, method = dict(radar), "radar-only"
        self._corroborate_with_neptun(fused, radar)

        # Matched tracks retain the ADS-B database's civil/military category;
        # tak_layer.py then applies the scenario ICAO affiliation classifier.
        # Unmatched radar-only contacts stay unknown.
        if adsb is None:
            aff_slot = "unknown"
        else:
            aff_slot = "mil" if adsb.get("is_military") else "civ"
        topic = TOPIC_FUSED.format(TOPIC_ROOT, aff_slot)
        publish_collection(self._session, topic, fused, NormalizedTrack)
        if self._verbose:
            ident = (fused.get("callsign") or fused.get("icao24") or
                     fused.get("radar_id") or "?")
            print("FUSE [{}] {} → {}".format(method, ident,
                  "/".join(topic.split("/")[1:4])), flush=True)

    def _find_match(self, radar: dict) -> dict | None:
        """Return the best-matching ADS-B track or None."""
        now = time.time()
        with self._lock:
            # 1. ICAO exact match
            icao = radar.get("icao24", "").strip().lower()
            if icao:
                uid = self._adsb_by_icao.get(icao)
                if uid and uid in self._adsb_tracks:
                    entry = self._adsb_tracks[uid]
                    if now - entry["ts"] < _MAX_AGE_S:
                        return entry["track"]

            # 2. Squawk + altitude match
            sq = radar.get("squawk", "")
            if sq and sq not in ("0000", "7500", "7600", "7700"):
                uids = self._adsb_by_squawk.get(sq, [])
                r_alt = radar.get("alt_baro_ft")
                for uid in uids:
                    if uid not in self._adsb_tracks:
                        continue
                    entry = self._adsb_tracks[uid]
                    if now - entry["ts"] >= _MAX_AGE_S:
                        continue
                    a_alt = entry["track"].get("alt_baro_ft")
                    if r_alt and a_alt and abs(r_alt - a_alt) < 500:
                        return entry["track"]
                    elif r_alt is None and a_alt is None:
                        return entry["track"]

            # 3. Spatial proximity
            r_lat = radar.get("lat_deg")
            r_lon = radar.get("lon_deg")
            if r_lat is None or r_lon is None:
                return None
            best_d, best_t = _SPATIAL_NM, None
            for uid, entry in self._adsb_tracks.items():
                if now - entry["ts"] >= _MAX_AGE_S:
                    continue
                t = entry["track"]
                a_lat = t.get("lat_deg")
                a_lon = t.get("lon_deg")
                if a_lat is None or a_lon is None:
                    continue
                d = _haversine_nm(r_lat, r_lon, a_lat, a_lon)
                if d < best_d:
                    best_d, best_t = d, t
            if best_t is not None:
                best_t = dict(best_t)
                best_t["_fusion_method"] = "spatial-{:.2f}NM".format(best_d)
                return best_t

        return None

    def _merge(self, radar: dict, adsb: dict) -> tuple[dict, str]:
        fused  = {}
        method = adsb.get("_fusion_method", "icao-exact")

        if _RADAR_PREF:
            # Radar is authoritative for all kinematics — start from radar entirely,
            # then layer on only the identity fields from ADS-B.
            fused.update(radar)
            for k in _ID_FIELDS:
                if k in adsb:
                    fused[k] = adsb[k]
            # Supplement: take kinematic fields from ADS-B only if radar cannot
            # provide them (e.g. vertical rate — not available in CAT-48).
            for k in _ADSB_SUPPLEMENT:
                if k not in fused and k in adsb:
                    fused[k] = adsb[k]
        else:
            # ADS-B pref: start from ADS-B, overwrite kinematics with radar
            fused.update(adsb)
            for k in _RADAR_FIELDS:
                if k in radar:
                    fused[k] = radar[k]

        # Always use the fresher timestamp
        fused["_ts"]  = max(radar.get("_ts", 0), adsb.get("_ts", 0))
        fused["_src"] = "{} + {}".format(
            radar.get("_src", "radar"), adsb.get("_src", "adsb"))
        fused.pop("_fusion_method", None)
        return fused, method

    # ------------------------------------------------------------------
    # Cache maintenance
    # ------------------------------------------------------------------

    def _age_out(self):
        now = time.time()
        with self._lock:
            stale_r = [k for k, v in self._radar_tracks.items()
                       if now - v["ts"] > _MAX_AGE_S]
            stale_a = [k for k, v in self._adsb_tracks.items()
                       if now - v["ts"] > _MAX_AGE_S]
            stale_set = set(stale_r)

            for k in stale_r:
                entry = self._radar_tracks.pop(k)
                icao = entry["track"].get("icao24", "").strip().lower()
                if icao and self._radar_by_icao.get(icao) == k:
                    del self._radar_by_icao[icao]

                # Cross-radar handoff promotion:
                # If k was a primary, elect the first surviving secondary as the
                # new primary so the ATAK marker transfers cleanly.
                if self._radar_primary.get(k) == k:
                    new_primary = None
                    for other_uid, p in list(self._radar_primary.items()):
                        if p == k and other_uid not in stale_set and other_uid in self._radar_tracks:
                            new_primary = other_uid
                            break
                    if new_primary:
                        # Repoint every uid that referenced old primary → new primary
                        for uid2 in list(self._radar_primary):
                            if self._radar_primary[uid2] == k:
                                self._radar_primary[uid2] = new_primary
                        self._radar_primary[new_primary] = new_primary
                        if self._verbose:
                            print("HANDOFF promote {} → {}".format(k, new_primary), flush=True)
                self._radar_primary.pop(k, None)

            for k in stale_a:
                entry = self._adsb_tracks.pop(k)
                icao = entry["track"].get("icao24", "").strip().lower()
                self._adsb_by_icao.pop(icao, None)
                sq = entry["track"].get("squawk", "")
                if sq in self._adsb_by_squawk:
                    try:
                        self._adsb_by_squawk[sq].remove(k)
                    except ValueError:
                        pass


def run(args):
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("fusion Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)
    fuser   = TrackFuser(session, args.verbose)
    subs    = []

    print("Track fusion bridge started", flush=True)
    print("  Spatial threshold: {} NM".format(_SPATIAL_NM), flush=True)
    print("  Max track age:     {} s".format(_MAX_AGE_S), flush=True)
    print("  Position source:   {}".format("radar" if _RADAR_PREF else "ADS-B"), flush=True)

    for topic in _RADAR_TOPICS:
        subs.append(subscribe(session, topic, fuser.on_radar))
        print("  SUB radar: {}".format(topic), flush=True)
    for topic in _ADSB_TOPICS:
        subs.append(subscribe(session, topic, fuser.on_adsb))
        print("  SUB adsb:  {}".format(topic), flush=True)
    subs.append(subscribe(session, OCCUPIED_TOPIC, fuser.on_occupied))
    subs.append(subscribe(session, _NEPTUN_TOPIC, fuser.on_neptun))
    print("  SUB neptun: {}".format(_NEPTUN_TOPIC), flush=True)
    for domain in ("air", "land", "sea"):
        topic = "{}/{}/**".format(TOPIC_ROOT, domain)
        subs.append(subscribe(session, topic, fuser.on_any_track))
        print("  SUB twins:  {}".format(topic), flush=True)

    print("Fusion running — Ctrl-C to stop", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        for sub in subs:
            sub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="Multi-source track fusion bridge")
    ap.add_argument("--spatial-nm", type=float,
                    default=_SPATIAL_NM,
                    help="Spatial correlation threshold in NM (default {})".format(_SPATIAL_NM))
    ap.add_argument("--max-age", type=float,
                    default=_MAX_AGE_S,
                    help="Track cache age limit in seconds (default {})".format(_MAX_AGE_S))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
