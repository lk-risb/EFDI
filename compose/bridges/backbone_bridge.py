#!/usr/bin/env python3
"""backbone_bridge.py — EFDI Backbone trial fabric -> Zenoh bridge.

Brings other participants' tracks on the shared EFDI Backbone trial fabric
into this pod's own namespace so tak_layer.py renders them on the TAK map,
the same as any other bridge's sensor tracks.

Reaches the backbone through zenoh-router-backbone's own peering with this
pod's primary router (tcp/127.0.0.1:7448, the same local connection every
bridge already uses) — no separate backbone TLS identity needed here; that
identity belongs to zenoh-router-backbone alone. The primary router's own
ACL (examples/zenoh-router.json5.tmpl, "pod-backbone-read" rule) is what
actually lets a local-tcp client like this one subscribe to backbone
content at all.

Only one schema is understood so far: the "catalyst" convention several
trial-fabric participants publish (top-level "location": {"latitude",
"longitude"}, "ci_uuid"/"ci_name", "metadata": {"affiliation"}, "device":
{"type"}). Everything else on the fabric (raw protobuf, encrypted payloads,
other JSON shapes) is silently skipped — there is no schema registry for
this trial fabric, so new shapes get added here as they're identified, not
guessed at up front.
"""

import argparse
import json
import time

import zenoh
from protocols.gateway import TOPIC_ROOT, base_record, open_session, payload_json, subscribe

_AFFILIATION_SLOT = {
    "FRIENDLY": "friendly",
    "HOSTILE": "hostile",
    "NEUTRAL": "neutral",
}


def _kind(device_type: str) -> str:
    device_type = device_type.lower()
    if "vehicle" in device_type or "ugv" in device_type or "uav" in device_type:
        return "vehicle"
    return "unit"


def _handle(session, sample, verbose: bool):
    key = str(sample.key_expr)
    if key.startswith(TOPIC_ROOT + "/"):
        return  # our own re-published output, matched by the "**" subscription
    try:
        obj = payload_json(sample)
    except (ValueError, UnicodeDecodeError):
        return
    if not isinstance(obj, dict):
        return
    loc = obj.get("location")
    if not isinstance(loc, dict):
        return
    try:
        lat = float(loc["latitude"])
        lon = float(loc["longitude"])
    except (KeyError, TypeError, ValueError):
        return

    uid = obj.get("ci_uuid") or obj.get("ci_name") or key
    affiliation = str((obj.get("metadata") or {}).get("affiliation", "")).upper()
    slot = _AFFILIATION_SLOT.get(affiliation, "unknown")
    kind = _kind(str((obj.get("device") or {}).get("type", "")))

    track = base_record(
        "backbone:" + key.split("/", 1)[0],
        "backbone-{}".format(uid),
        lat_deg=round(lat, 6),
        lon_deg=round(lon, 6),
        callsign=obj.get("ci_name", uid),
    )
    topic = "{}/land/backbone/{}/{}/tracks/v1".format(TOPIC_ROOT, slot, kind)
    session.put(topic, json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
    if verbose:
        print("backbone track {} -> {} ({:.5f},{:.5f})".format(uid, topic, lat, lon), flush=True)


def main():
    ap = argparse.ArgumentParser(description="EFDI Backbone trial fabric -> Zenoh bridge")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("backbone bridge Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    print("backbone bridge starting — watching backbone fabric for known track schemas", flush=True)
    sub = subscribe(session, "**", lambda sample: _handle(session, sample, args.verbose))

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        sub.undeclare()
        session.close()


if __name__ == "__main__":
    main()
