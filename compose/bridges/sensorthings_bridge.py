"""OGC SensorThings API (Part 1: Sensing) ingress -> Zenoh raw observations.

Polls a SensorThings v1.1 service for recent Observations and republishes each
one verbatim under the fabric's raw namespace. Decoding into canonical sensor
records is the protocol's job (protocols/random/sensorthings.py).

The request expands Datastream and FeatureOfInterest so a single Observation
carries its unit of measurement, its sensor name and its location — the pieces
the normalizer needs to place a marker without a second round trip.

Config (compose/.env):
  SENSORTHINGS_URL=https://host/FROST-Server/v1.1   # required, service root
  SENSORTHINGS_POLL_S=30                            # poll interval
  SENSORTHINGS_TOKEN=                               # optional bearer token
  SENSORTHINGS_PAGE_LIMIT=200                       # max observations per poll

Run:
  venv/bin/python3 bridges/sensorthings_bridge.py
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from namespace_prefix import topic_root

import zenoh
from protocols.gateway import open_session

TOPIC_ROOT = topic_root()
_RECONNECT_S = float(os.environ.get("SENSORTHINGS_RECONNECT_S", "10"))
RAW_TOPIC = "{}/raw/sensorthings/observations".format(TOPIC_ROOT)
MAX_BYTES = int(os.environ.get("SENSORTHINGS_MAX_BYTES", "8388608"))


def observations_url(base: str, since: datetime, limit: int) -> str:
    """Build the Observations query, expanded and filtered to what is new."""
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    query = {
        "$expand": "Datastream($expand=Sensor,ObservedProperty,Thing($expand=Locations)),FeatureOfInterest",
        "$filter": "phenomenonTime gt {}".format(stamp),
        "$orderby": "phenomenonTime asc",
        "$top": str(limit),
    }
    return base.rstrip("/") + "/Observations?" + urllib.parse.urlencode(query)


def fetch(url: str, token: str) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "efdi-sensorthings-bridge/1.0"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read(MAX_BYTES).decode("utf-8"))


def run(args) -> None:
    if not args.url:
        raise SystemExit("Set SENSORTHINGS_URL in .env or pass --url https://host/v1.1")
    if not args.url.lower().startswith(("http://", "https://")):
        raise SystemExit("SENSORTHINGS_URL must be an http(s) service root")

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("SensorThings Zenoh connect failed: {} — retry in {}s".format(exc, _RECONNECT_S), flush=True)
            time.sleep(_RECONNECT_S)

    publisher = session.declare_publisher(RAW_TOPIC)
    print("SensorThings ingress: {} -> {}".format(args.url, RAW_TOPIC), flush=True)

    # Start one interval back so the first poll returns something useful.
    since = datetime.now(timezone.utc) - timedelta(seconds=args.poll * 2)
    try:
        while True:
            try:
                payload = fetch(observations_url(args.url, since, args.limit), args.token)
                observations = payload.get("value") or []
                for observation in observations:
                    publisher.put(json.dumps(observation, separators=(",", ":")).encode(),
                                  encoding=zenoh.Encoding.APPLICATION_JSON)
                    stamp = observation.get("phenomenonTime") or ""
                    # phenomenonTime may be an interval ("start/end"); keep the end.
                    stamp = stamp.split("/")[-1]
                    try:
                        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        since = max(since, parsed)
                    except ValueError:
                        pass
                if args.verbose:
                    print("SensorThings poll: {} observations".format(len(observations)), flush=True)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                print("SensorThings poll failed: {}".format(exc), flush=True)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="OGC SensorThings API ingress -> Zenoh raw observations")
    ap.add_argument("--url", default=os.environ.get("SENSORTHINGS_URL", ""),
                    help="SensorThings service root, e.g. https://host/FROST-Server/v1.1")
    ap.add_argument("--poll", type=float, default=float(os.environ.get("SENSORTHINGS_POLL_S", "30")))
    ap.add_argument("--token", default=os.environ.get("SENSORTHINGS_TOKEN", ""))
    ap.add_argument("--limit", type=int, default=int(os.environ.get("SENSORTHINGS_PAGE_LIMIT", "200")))
    ap.add_argument("--verbose", "-v", action="store_true")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
