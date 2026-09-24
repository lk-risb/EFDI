#!/usr/bin/env python3
"""backbone_bridge.py — EFDI Backbone trial fabric -> Zenoh bridge.

Brings other participants' tracks on the shared EFDI Backbone trial fabric
into this pod's own namespace so tak_layer.py renders them on the TAK map,
the same as any other bridge's sensor tracks.

Holds its OWN direct mTLS session to the real backbone router, using the
same identity uploaded via /api/certs/backbone/bootstrap (dual-written into
this pod's own primary-router TLS slot at
${POD_STATE_DIR}/zenoh/tls/backbone/{cert,key,ca-roots}.pem — a host path,
readable directly since this is a native host process, not a container).

This does NOT go through zenoh-router-backbone's router-to-router relay.
Confirmed live, with Zenoh's own debug logging: a subscription declared on
this pod's local router (tcp/127.0.0.1:7448) DOES get forwarded from the
primary router into zenoh-router-backbone (visible in its own debug log at
the exact right timestamp, registered internally) but zenoh-router-backbone
then never transmits anything for it onward to the real backbone link —
total silence on that link after its one-time connect-time declaration
dump, with or without scouting/gossip multihop enabled. Every other
integration in this codebase (dronuradaras_bridge.py, tak_layer.py,
federation_apply.py, ...) already works this same way — one bridge process
holding two independent zenoh sessions and explicitly relaying between them
in application code — never by relying on automatic multi-hop router-to-
router pub/sub propagation across more than one hop. zenoh-router-backbone
peering primary <-> the real backbone directly (one hop each side) is
proven reliable by the same debug-log evidence; going through it as a
SECOND hop from an already-local subscription is what doesn't work.

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
import os
import time

import zenoh
from protocols.gateway import TOPIC_ROOT, base_record, open_session, payload_json, subscribe

_AFFILIATION_SLOT = {
    "FRIENDLY": "friendly",
    "HOSTILE": "hostile",
    "NEUTRAL": "neutral",
}

_POD_STATE_DIR = os.environ.get("POD_STATE_DIR", "/root/efdi-router/compose/state")
_BACKBONE_TLS_DIR = os.path.join(_POD_STATE_DIR, "zenoh", "tls", "backbone")


def _kind(device_type: str) -> str:
    device_type = device_type.lower()
    if "vehicle" in device_type or "ugv" in device_type or "uav" in device_type:
        return "vehicle"
    return "unit"


def _backbone_endpoints() -> list[str]:
    """Same endpoint the WebUI's identity upload staged for display purposes
    (backbone_bootstrap.py's _control_env_update) — read fresh from the
    environment on every start (start.sh sources compose/.env before
    launching any native process), not baked in like the zenoh-admin
    container's own copy of this same variable."""
    raw = os.environ.get("EFDI_BACKBONE_FABRIC_ENDPOINTS", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return [raw]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    return [str(parsed)]


def _open_backbone_session():
    """Direct mTLS session to the real backbone fabric, bypassing
    zenoh-router-backbone's router-to-router relay entirely (see module
    docstring for why). Retries forever — the identity may not be uploaded
    yet, or the fabric endpoint may be temporarily unreachable."""
    cert = os.path.join(_BACKBONE_TLS_DIR, "cert.pem")
    key = os.path.join(_BACKBONE_TLS_DIR, "key.pem")
    ca = os.path.join(_BACKBONE_TLS_DIR, "ca-roots.pem")
    while True:
        endpoints = _backbone_endpoints()
        missing = [p for p in (cert, key, ca) if not os.path.isfile(p)]
        if not endpoints or missing:
            print(
                "backbone bridge: no backbone identity/endpoint configured yet "
                "(missing {}) — upload one via the Certificates page. Retry in 15s"
                .format(missing or "EFDI_BACKBONE_FABRIC_ENDPOINTS"),
                flush=True,
            )
            time.sleep(15)
            continue
        conf = zenoh.Config()
        conf.insert_json5("mode", '"client"')
        conf.insert_json5("connect/endpoints", json.dumps(endpoints))
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": ca,
            "connect_certificate": cert,
            "connect_private_key": key,
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
        try:
            return zenoh.open(conf)
        except Exception as exc:
            print("backbone bridge: backbone connect failed: {} — retry in 15s".format(exc), flush=True)
            time.sleep(15)


def _handle(local_session, sample, verbose: bool):
    key = str(sample.key_expr)
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
    local_session.put(topic, json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
    if verbose:
        print("backbone track {} -> {} ({:.5f},{:.5f})".format(uid, topic, lat, lon), flush=True)


def main():
    ap = argparse.ArgumentParser(description="EFDI Backbone trial fabric -> Zenoh bridge")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    while True:
        try:
            local_session = open_session()
            break
        except Exception as exc:
            print("backbone bridge: local Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    backbone_session = _open_backbone_session()
    print("backbone bridge starting — watching backbone fabric for known track schemas", flush=True)
    sub = subscribe(backbone_session, "**", lambda sample: _handle(local_session, sample, args.verbose))

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        sub.undeclare()
        backbone_session.close()
        local_session.close()


if __name__ == "__main__":
    main()
