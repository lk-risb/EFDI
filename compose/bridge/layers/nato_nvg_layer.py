#!/usr/bin/env python3
"""zenoh_nvg_bridge.py — Zenoh EFDI track topics → SitaWare Edge NVG 2.0 bridge.

Subscribes to all EFDI track topics and forwards live positions to SitaWare Edge
via its NVG REST API.  SitaWare Frontline clients connected to the same Edge server
see all tracks automatically — no separate Frontline integration required.

NVG items are PUT individually on each Zenoh update so ATAK/Frontline operators see
real-time movement.  A background refresh thread re-PUTs live items every
REFRESH_S seconds and DELETEs items older than STALE_S seconds.

SitaWare Edge REST base: http(s)://<host>:<port>/SWEdge/nvg/v2
  PUT  /sources/{source}/items/{item-id}   — create / update one item
  DELETE /sources/{source}/items/{item-id} — remove one item

Required env vars (or --args):
  SITAWARE_URL    http://192.168.x.x:8080   (no trailing slash)
  SITAWARE_USER   your Edge username
  SITAWARE_PASS   your Edge password
  SITAWARE_SOURCE efdi-live                 (source name, created automatically)

Run:
  SITAWARE_URL=http://192.168.1.10:8080 SITAWARE_USER=admin SITAWARE_PASS=secret \\
    venv/bin/python3 zenoh_nvg_bridge.py
"""

import argparse
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = "LTU/CISB/" + ORG   # organization prefix precedes the pod namespace
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

NVG_NS      = "http://tide.act.nato.int/schemas/2012/10/nvg"
NVG_VERSION = "2.0.2"
REFRESH_S   = 10    # re-PUT all live tracks at this interval
STALE_S     = 120   # delete tracks older than this

# APP-6(B) SIDC codes — keyed by new schema wildcard patterns.
# ** matches zero-or-more Zenoh path segments.
_TOPIC_SIDC = {
    "air/**/civ/aircraft/**":        "SFAPCF----E----",  # Neutral Air Fixed Wing (civil ADS-B)
    "air/**/mil/aircraft/**":        "SNAPCF----E----",  # Neutral Air Fixed Wing (military)
    "air/**/unknown/**":             "SUAPCF----E----",  # Unknown Air (radar / SAPIENT, no ID)
    # Fused tracks (radar + open-source identity merged by track_fusion_layer.py)
    "air/fused/civ/aircraft/**":     "SNAPCF----E----",  # Identified cooperative contact
    "air/fused/unknown/aircraft/**": "SUAPCF----E----",  # PSR-only — no transponder match
    "land/**/civ/vehicle/**":        "SFGPUCV---E----",  # Friendly Ground Vehicle
    "land/**/neutral/station/**":    "SFGP------E----",  # Neutral Ground Installation
    "land/**/friendly/unit/**":      "SFGPU-----E----",  # Friendly Ground Unit (NFFI)
    "sea/**/civ/vessel/**":          "SFSPXF----E----",  # Friendly Sea Surface
    "sea/**/mil/vessel/**":          "SNSPXF----E----",  # Neutral Sea Surface (military)
    "space/**/civ/satellite/**":     "SFPAP-----E----",  # Friendly Space (satellite)
}


# ---------------------------------------------------------------------------
# Zenoh config
# ---------------------------------------------------------------------------

def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
        "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
        "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
        "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
        "enable_mtls": True,
        "verify_name_on_connect": True,
    }))
    return conf


# ---------------------------------------------------------------------------
# NVG XML builders
# ---------------------------------------------------------------------------

def _ts_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _uid(track: dict) -> str:
    src = track.get("_src", "efdi")
    for key in ("icao24", "mmsi", "sensor_id"):
        v = track.get(key)
        if v:
            return "EFDI-{}-{}".format(src, str(v).upper())
    cs = (track.get("callsign") or "").strip()
    if cs:
        return "EFDI-{}-{}".format(src, cs)
    return "EFDI-{}-{:.5f}-{:.5f}".format(src, track.get("lat_deg", 0), track.get("lon_deg", 0))


def _callsign(track: dict, uid: str) -> str:
    for key in ("callsign", "registration", "mmsi"):
        v = track.get(key)
        if v and str(v).strip():
            return str(v).strip()
    return uid[-12:]


def _hae_m(track: dict) -> float | None:
    for key, scale in (
        ("geo_alt_m",   1.0),
        ("alt_geom_ft", 0.3048),
        ("baro_alt_m",  1.0),
        ("alt_baro_ft", 0.3048),
        ("alt_m",       1.0),
    ):
        v = track.get(key)
        if v is not None and float(v) != 0:
            return round(float(v) * scale, 1)
    return None


def track_to_nvg_item(track: dict, sidc: str) -> tuple[str, str] | None:
    """Return (item_id, NVG XML string) or None if no position."""
    lat = track.get("lat_deg")
    lon = track.get("lon_deg")
    if lat is None or lon is None:
        return None

    uid   = _uid(track)
    label = _callsign(track, uid)
    ts    = float(track.get("_ts", time.time()))

    ET.register_namespace("", NVG_NS)
    root = ET.Element("{%s}nvg" % NVG_NS, {"version": NVG_VERSION})

    sym_attrs = {
        "id":     uid,
        "sidc":   sidc,
        "label":  label,
        # NVG points: "lon,lat" (longitude first — GeoJSON convention)
        "points": "{},{}".format(round(float(lon), 6), round(float(lat), 6)),
        "time":   _ts_str(ts),
    }
    alt = _hae_m(track)
    if alt is not None:
        sym_attrs["altitude"] = str(alt)

    sym = ET.SubElement(root, "{%s}symbol" % NVG_NS, sym_attrs)

    # Speed modifier
    speed = None
    if track.get("speed_ms") is not None:
        speed = round(float(track["speed_ms"]) * 1.94384, 1)  # m/s → knots
    elif track.get("ground_speed_kts") is not None:
        speed = round(float(track["ground_speed_kts"]), 1)
    if speed is not None:
        ET.SubElement(sym, "{%s}modifier" % NVG_NS, {"name": "Speed", "value": str(speed)})

    # Direction modifier
    for hkey in ("heading_deg", "track_deg"):
        v = track.get(hkey)
        if v is not None:
            ET.SubElement(sym, "{%s}modifier" % NVG_NS, {"name": "Direction", "value": str(round(float(v), 1))})
            break

    # Extra info in description
    parts = ["src:{}".format(track.get("_src", "?"))]
    for key in ("icao24", "mmsi", "registration", "aircraft_type", "squawk"):
        v = track.get(key)
        if v not in (None, ""):
            parts.append("{}:{}".format(key, v))
    ET.SubElement(sym, "{%s}description" % NVG_NS).text = " | ".join(parts)

    xml_str = '<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="unicode")
    return uid, xml_str


# ---------------------------------------------------------------------------
# SitaWare Edge HTTP client
# ---------------------------------------------------------------------------

class EdgeClient:
    def __init__(self, base_url: str, source: str, user: str, password: str):
        self.base    = base_url.rstrip("/")
        self.source  = source
        self._auth   = "Basic " + base64.b64encode("{}:{}".format(user, password).encode()).decode()

    def _req(self, method: str, path: str, body: str | None = None) -> int:
        url = "{}/SWEdge/nvg/v2/sources/{}/items{}".format(self.base, self.source, path)
        data = body.encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization":  self._auth,
            "Content-Type":   "application/xml; charset=utf-8",
            "Accept":         "application/xml",
            "User-Agent":     "efdi-nvg-bridge/1.0",
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code
        except urllib.error.URLError as exc:
            print("Edge HTTP error:", exc, flush=True)
            return 0

    def put_item(self, item_id: str, nvg_xml: str) -> bool:
        status = self._req("PUT", "/{}".format(item_id), nvg_xml)
        return status in (200, 201, 204)

    def delete_item(self, item_id: str) -> bool:
        status = self._req("DELETE", "/{}".format(item_id))
        return status in (200, 204, 404)


# ---------------------------------------------------------------------------
# Track cache + refresh thread
# ---------------------------------------------------------------------------

class TrackCache:
    def __init__(self, client: EdgeClient, stale_s: int, refresh_s: int, verbose: bool):
        self._client   = client
        self._stale_s  = stale_s
        self._refresh_s = refresh_s
        self._verbose  = verbose
        self._lock     = threading.Lock()
        self._tracks: dict[str, dict] = {}   # uid → {track, sidc, last_seen}
        self._thread   = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()

    def upsert(self, track: dict, sidc: str):
        result = track_to_nvg_item(track, sidc)
        if result is None:
            return
        uid, nvg_xml = result
        ok = self._client.put_item(uid, nvg_xml)
        if self._verbose:
            label = track.get("callsign") or track.get("mmsi") or uid[-10:]
            print("NVG PUT {} {} {}".format("OK" if ok else "FAIL", sidc[:6], label), flush=True)
        with self._lock:
            self._tracks[uid] = {"track": track, "sidc": sidc, "last_seen": time.time()}

    def _refresh_loop(self):
        while True:
            time.sleep(self._refresh_s)
            now = time.time()
            to_delete = []
            to_refresh = []
            with self._lock:
                for uid, entry in list(self._tracks.items()):
                    age = now - entry["last_seen"]
                    if age > self._stale_s:
                        to_delete.append(uid)
                    else:
                        to_refresh.append((uid, entry["track"], entry["sidc"]))
                for uid in to_delete:
                    del self._tracks[uid]

            for uid in to_delete:
                self._client.delete_item(uid)
                if self._verbose:
                    print("NVG DEL stale", uid, flush=True)

            live = 0
            for uid, track, sidc in to_refresh:
                result = track_to_nvg_item(track, sidc)
                if result:
                    self._client.put_item(uid, result[1])
                    live += 1
            if to_refresh or to_delete:
                print("NVG refresh: {} live, {} expired".format(live, len(to_delete)), flush=True)


# ---------------------------------------------------------------------------
# Zenoh subscriber callbacks
# ---------------------------------------------------------------------------

def make_handler(sidc: str, cache: TrackCache):
    def handler(sample):
        try:
            track = json.loads(bytes(sample.payload).decode())
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return
        cache.upsert(track, sidc)
    return handler


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    if not args.url:
        raise SystemExit("SITAWARE_URL not set — pass --url http://host:port or set env var")
    if not args.user or not args.password:
        raise SystemExit("SITAWARE_USER / SITAWARE_PASS not set")

    client = EdgeClient(args.url, args.source, args.user, args.password)
    cache  = TrackCache(client, stale_s=STALE_S, refresh_s=REFRESH_S, verbose=args.verbose)

    print("SitaWare Edge: {}  source: {}".format(args.url, args.source), flush=True)

    session = zenoh.open(make_config())
    subs = []
    for suffix, sidc in _TOPIC_SIDC.items():
        key = "{}/{}".format(TOPIC_ROOT, suffix)
        subs.append(session.declare_subscriber(key, make_handler(sidc, cache)))
        print("SUB {} → SIDC {}".format(key, sidc), flush=True)

    print("Bridge running — Ctrl-C to stop", flush=True)
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
    ap = argparse.ArgumentParser(description="Zenoh tracks → SitaWare Edge NVG bridge")
    ap.add_argument("--url",      default=os.environ.get("SITAWARE_URL", ""),
                    help="SitaWare Edge base URL, e.g. http://192.168.1.10:8080")
    ap.add_argument("--user",     default=os.environ.get("SITAWARE_USER", ""),
                    help="Edge username")
    ap.add_argument("--password", default=os.environ.get("SITAWARE_PASS", ""),
                    help="Edge password")
    ap.add_argument("--source",   default=os.environ.get("SITAWARE_SOURCE", "efdi-live"),
                    help="NVG source name (default: efdi-live)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print each NVG PUT/DELETE")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
