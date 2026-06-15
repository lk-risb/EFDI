#!/usr/bin/env python3
"""notam_bridge.py — ICAO Dataservices NOTAM API → Zenoh bridge.

Polls the ICAO Dataservices real-time NOTAM API for active NOTAMs at
configured airports/FIRs in the Baltic region and publishes each as a
Notam JSON to the EFDI Zenoh fabric.

Free API key: https://dataservices.icao.int/  (register, approval ~24 h)

Zenoh topic:  <ORG>/airspace/notam/<location>/v1
Proto schema: notam.proto  (message Notam, package ltu.cis.tracks.v1)

Run:
    ICAO_NOTAM_KEY=<key> venv/bin/python3 notam_bridge.py
    ICAO_NOTAM_KEY=<key> venv/bin/python3 notam_bridge.py --locations EYVI EYKA EVRA EETN
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
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)

ICAO_URL      = "https://dataservices.icao.int/api/notams-realtime-list"
POLL_INTERVAL = 900  # 15 min — NOTAMs can be issued at any time

# Baltic region: Lithuanian, Latvian, Estonian airports + key regional FIRs
DEFAULT_LOCATIONS = [
    "EYVI", "EYKA", "EYPA", "EYSA",  # Lithuania: Vilnius, Kaunas, Palanga, Šiauliai
    "EVRA",                            # Latvia: Riga
    "EETN",                            # Estonia: Tallinn
    "EFHK",                            # Finland: Helsinki
    "EPWA",                            # Poland: Warsaw
    "ESSA",                            # Sweden: Stockholm Arlanda
]


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


def fetch_notams(api_key: str, locations: list[str]) -> list:
    url = "{}?api_key={}&airports={}&format=json".format(
        ICAO_URL, api_key, ",".join(locations)
    )
    req = urllib.request.Request(url, headers={"User-Agent": "efdi-notam-bridge/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
            return data if isinstance(data, list) else []
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
        print("NOTAM fetch error:", exc, flush=True)
        return []


def normalize(raw: dict) -> dict | None:
    location = raw.get("location", "")
    if not location:
        return None
    return {
        "_ts":          time.time(),
        "_src":         "icao-notam",
        "notam_id":     raw.get("key", raw.get("id", "")),
        "notam_number": raw.get("message", raw.get("notamNumber", "")),
        "notam_type":   raw.get("type", raw.get("entity", "")),
        "location":     location,
        "state_code":   raw.get("StateCode", ""),
        "start_validity": raw.get("startdate", ""),
        "end_validity":   raw.get("enddate", ""),
        "q_code":       raw.get("Qcode", ""),
        "subject":      raw.get("Subject", ""),
        "condition":    raw.get("Condition", ""),
        "full_text":    raw.get("all", raw.get("text", "")),
        "lat_deg":      float(raw.get("latitude",  0) or 0),
        "lon_deg":      float(raw.get("longitude", 0) or 0),
        "radius_nm":    float(raw.get("radius",    0) or 0),
        "lower_fl":     int(raw.get("bottom",  0) or 0),
        "upper_fl":     int(raw.get("top",     0) or 0),
        "criticality":  int(raw.get("criticality", 0) or 0),
    }


def run(args):
    if not args.key:
        raise SystemExit(
            "ICAO_NOTAM_KEY not set — register free at https://dataservices.icao.int/"
        )

    session = zenoh.open(make_config())
    pub_cache: dict[str, object] = {}

    def get_pub(location: str):
        if location not in pub_cache:
            pub_cache[location] = session.declare_publisher(
                "{}/airspace/notam/{}/v1".format(ORG, location)
            )
        return pub_cache[location]

    print("NOTAM locations:", args.locations, flush=True)

    try:
        while True:
            notams = fetch_notams(args.key, args.locations)
            seen = set()
            for raw in notams:
                point = normalize(raw)
                if point is None:
                    continue
                loc = point["location"]
                get_pub(loc).put(json.dumps(point).encode())
                seen.add(loc)
            print("Published {} NOTAMs across {} locations".format(
                len(notams), len(seen)), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        for pub in pub_cache.values():
            pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="ICAO NOTAM → Zenoh bridge")
    ap.add_argument("--key", default=os.environ.get("ICAO_NOTAM_KEY", ""),
                    help="ICAO Dataservices API key (or set ICAO_NOTAM_KEY env var)")
    ap.add_argument("--locations", nargs="+", default=DEFAULT_LOCATIONS,
                    help="ICAO airport/FIR codes to query")
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: 900)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
