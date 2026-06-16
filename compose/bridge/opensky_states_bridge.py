#!/usr/bin/env python3
"""opensky_bridge.py — OpenSky Network → Zenoh bridge.

Polls the OpenSky Network REST API for live aircraft positions within a
geographic bounding box and publishes each track as JSON to the EFDI fabric.

Free anonymous tier: 1 request / 10 s, ~400 req/day — handled automatically.
No API key required.

Zenoh topic:  <ORG>/opensky/tracks/v1
Proto schema: aircraft_track.proto  (message AircraftTrack, package ltu.cis.tracks.v1)

Run:
    . venv/bin/activate
    python3 opensky_bridge.py
    python3 opensky_bridge.py --lamin 52 --lamax 60 --lomin 18 --lomax 30
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = "1851281db70ccc0409dad4ecfc874cf5"
HERE   = os.path.dirname(os.path.abspath(__file__))
# GOAT_CERT_DIR: directory containing efdi-ca-root.pem + ORG-{cert,key}.pem.
# Defaults to HERE so the script works unchanged when run directly from the bundle.
# In Docker set GOAT_CERT_DIR=/certs and mount the bundle certs there.
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)

OPENSKY_URL  = "https://opensky-network.org/api/states/all"
POLL_INTERVAL = 10  # seconds — OpenSky free-tier minimum

# Operational focus: Baltic + Scandinavia + Eastern Europe + Middle East
# Covers ~20°N–73°N, 4°E–65°E  (Lithuania centroid ≈ 55.17°N, 23.88°E)
DEFAULT_LAMIN = 20.0
DEFAULT_LAMAX = 73.0
DEFAULT_LOMIN = 4.0
DEFAULT_LOMAX = 65.0

# OpenSky states/all column indices
_ICAO24       = 0
_CALLSIGN     = 1
_ORIGIN       = 2
_TIME_POS     = 3
_LAST_CONTACT = 4
_LON          = 5
_LAT          = 6
_BARO_ALT     = 7
_ON_GROUND    = 8
_VELOCITY     = 9
_TRUE_TRACK   = 10
_VERT_RATE    = 11
_GEO_ALT      = 13
_SQUAWK       = 14
_POS_SOURCE   = 16

_POS_SOURCE_NAMES = {0: "ADS-B", 1: "ASTERIX", 2: "MLAT", 3: "FLARM"}


# ---------------------------------------------------------------------------
# Zenoh
# ---------------------------------------------------------------------------

def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([ROUTER]))
    conf.insert_json5("transport/link/tls", json.dumps({
        "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
        "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
        "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
        "enable_mtls": True,
        "verify_name_on_connect": True,
    }))
    return conf


# ---------------------------------------------------------------------------
# OpenSky fetch
# ---------------------------------------------------------------------------

def fetch_states(lamin, lamax, lomin, lomax) -> list:
    """Return raw states list from OpenSky, or [] on error."""
    url = (
        "{}?lamin={}&lamax={}&lomin={}&lomax={}"
        .format(OPENSKY_URL, lamin, lamax, lomin, lomax)
    )
    req = urllib.request.Request(url, headers={"User-Agent": "efdi-opensky-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print("OpenSky HTTP {}: {}".format(exc.code, exc.reason), flush=True)
        return []
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print("OpenSky fetch error:", exc, flush=True)
        return []
    return data.get("states") or []


def normalize(state: list) -> dict | None:
    """Convert one OpenSky state vector to a track dict. Returns None if no position."""
    lat = state[_LAT]
    lon = state[_LON]
    if lat is None or lon is None:
        return None

    track = {
        "_ts":     time.time(),
        "_src":    "opensky",
        "icao24":  state[_ICAO24],
        "lat_deg": round(lat, 6),
        "lon_deg": round(lon, 6),
    }

    callsign = state[_CALLSIGN]
    if callsign:
        track["callsign"] = callsign.strip()

    if state[_ORIGIN]:
        track["origin_country"] = state[_ORIGIN]

    if state[_BARO_ALT] is not None:
        track["baro_alt_m"] = round(state[_BARO_ALT], 1)

    if state[_GEO_ALT] is not None:
        track["geo_alt_m"] = round(state[_GEO_ALT], 1)

    if state[_VELOCITY] is not None:
        track["speed_ms"] = round(state[_VELOCITY], 2)

    if state[_TRUE_TRACK] is not None:
        track["heading_deg"] = round(state[_TRUE_TRACK], 1)

    if state[_VERT_RATE] is not None:
        track["vertical_rate_ms"] = round(state[_VERT_RATE], 2)

    track["on_ground"] = bool(state[_ON_GROUND])

    if state[_SQUAWK]:
        track["squawk"] = state[_SQUAWK]

    src = state[_POS_SOURCE]
    if src is not None:
        track["pos_source"] = _POS_SOURCE_NAMES.get(src, str(src))

    if state[_TIME_POS]:
        track["time_position"] = int(state[_TIME_POS])

    return track


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(args):
    topic = "{}/air/civ/{}".format(ORG, args.topic_suffix)
    print("Zenoh topic:", topic, flush=True)
    print("Bounding box: lat [{}, {}]  lon [{}, {}]".format(
        args.lamin, args.lamax, args.lomin, args.lomax), flush=True)

    session = zenoh.open(make_config())
    pub = session.declare_publisher(topic)

    try:
        while True:
            states = fetch_states(args.lamin, args.lamax, args.lomin, args.lomax)
            published = 0
            skipped_ground = 0
            for state in states:
                if args.airborne_only and state[_ON_GROUND]:
                    skipped_ground += 1
                    continue
                track = normalize(state)
                if track is None:
                    continue
                payload = json.dumps(track)
                pub.put(payload.encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
                print("PUB", payload[:140], flush=True)
                published += 1

            print("Poll: {} states → {} published, {} on-ground skipped".format(
                len(states), published, skipped_ground), flush=True)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="OpenSky Network → Zenoh bridge")
    ap.add_argument("--lamin", type=float, default=DEFAULT_LAMIN, help="Min latitude")
    ap.add_argument("--lamax", type=float, default=DEFAULT_LAMAX, help="Max latitude")
    ap.add_argument("--lomin", type=float, default=DEFAULT_LOMIN, help="Min longitude")
    ap.add_argument("--lomax", type=float, default=DEFAULT_LOMAX, help="Max longitude")
    ap.add_argument("--topic-suffix", default="tracks/v1", help="Topic suffix")
    ap.add_argument("--airborne-only", action="store_true", default=True,
                    help="Skip on-ground aircraft (default: true)")
    ap.add_argument("--all", dest="airborne_only", action="store_false",
                    help="Include on-ground aircraft")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
