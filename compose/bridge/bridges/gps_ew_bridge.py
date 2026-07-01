#!/usr/bin/env python3
"""gps_ew_bridge.py — GPS jamming / spoofing threat bridge.

Aggregates interference data from multiple public sources and publishes
each affected area as a unified threat record to the EFDI Zenoh fabric.

Sources enabled via env vars (all optional — bridge runs with whatever is set):
  GPSJam.org       — hex-grid jamming probability from ADS-B anomaly analysis (free)
  EUROCONTROL GNSS — structured interference reports (GPSEW_EUROCONTROL_KEY required)
  Custom / SKAI    — any REST endpoint returning JSON (GPSEW_CUSTOM_URL [+ GPSEW_CUSTOM_KEY])

Zenoh topic: <ORG>/env/gps/ew/hostile/threat/v1
CoT type:    a-h-G-E-X  (hostile ground electronic warfare equipment)

Schema (published JSON):
  _ts            float   Unix timestamp of this record
  _src           str     Source: "gpsjam" | "eurocontrol" | "custom"
  lat_deg        float   Centre of affected area
  lon_deg        float   Centre of affected area
  radius_km      float   Approximate radius of effect (0 = point report)
  event_type     str     "jamming" | "spoofing" | "unknown"
  severity       str     "low" | "medium" | "high"
  jam_prob       float   0.0–1.0 where known, else -1
  report_utc     str     ISO-8601 observation time
  detail         str     Free-text detail / NOTAM text

Run:
    venv/bin/python3 gps_ew_bridge.py
    GPSEW_CUSTOM_URL=https://api.skai.example/gps-threats venv/bin/python3 gps_ew_bridge.py
"""

import argparse
import http.server
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import zenoh

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = os.environ.get("PARTNER_NAMESPACE", "")
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

# ── Source configuration (all from env) ───────────────────────────────────────

# GPSJam.org  — free, no key. Publishes daily hex-grid interference maps derived
# from ADS-B anomaly analysis across thousands of receiver stations.
# TODO: verify the exact JSON endpoint URL at https://gpsjam.org — the path below
#       is the best available public reference; update if the upstream API changes.
GPSJAM_URL = os.environ.get(
    "GPSEW_GPSJAM_URL",
    "https://gpsjam.org/api/jam",
)

# EUROCONTROL GNSS — structured interference/NOTAM feed.
# Register at: https://www.eurocontrol.int/service/gnss-data-service
EUROCONTROL_URL = "https://www.eurocontrol.int/api/gnss/interference"  # TODO: confirm endpoint
EUROCONTROL_KEY = os.environ.get("GPSEW_EUROCONTROL_KEY", "")

# Custom source — plug in SKAI, commercial feeds, or any REST endpoint returning JSON.
# If the endpoint returns a list, each item is passed to normalize_custom().
# If it returns an object with a list field, set GPSEW_CUSTOM_LIST_KEY to that field name.
CUSTOM_URL      = os.environ.get("GPSEW_CUSTOM_URL", "")
CUSTOM_KEY      = os.environ.get("GPSEW_CUSTOM_KEY", "")
CUSTOM_LIST_KEY = os.environ.get("GPSEW_CUSTOM_LIST_KEY", "")  # e.g. "threats" or "events"

POLL_INTERVAL = int(os.environ.get("GPSEW_POLL_S", "1800"))  # 30 min default

# Minimum jamming probability to publish (GPSJam only). Filter noise below this.
JAM_THRESHOLD = float(os.environ.get("GPSEW_JAM_THRESHOLD", "0.4"))

# Bounding box filter (S, W, N, E) — skip hex cells well outside area of interest.
# Default covers Europe + Middle East. Set to "" to disable.
_BBOX_STR = os.environ.get("GPSEW_BBOX", "30,0,72,60")


def _bbox():
    if not _BBOX_STR:
        return None
    parts = [float(x) for x in _BBOX_STR.split(",")]
    return parts[0], parts[1], parts[2], parts[3]   # s, w, n, e


def _in_bbox(lat: float, lon: float) -> bool:
    bb = _bbox()
    if bb is None:
        return True
    s, w, n, e = bb
    return s <= lat <= n and w <= lon <= e


def _severity(prob: float) -> str:
    if prob >= 0.7:
        return "high"
    if prob >= 0.4:
        return "medium"
    return "low"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _fetch(url: str, headers: dict | None = None, timeout: int = 20) -> bytes | None:
    req = urllib.request.Request(url, headers={
        "User-Agent": "efdi-gps-ew-bridge/1.0",
        **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        print("GPS-EW fetch error {}: {}".format(url, exc), flush=True)
        return None


# ── GPSJam.org ────────────────────────────────────────────────────────────────

def fetch_gpsjam() -> list[dict]:
    """Fetch today's hex-grid interference map from GPSJam.org."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = "{}?date={}".format(GPSJAM_URL, date_str)
    raw = _fetch(url)
    if not raw:
        return []
    try:
        data = json.loads(raw.decode())
    except json.JSONDecodeError as exc:
        print("GPS-EW gpsjam JSON error:", exc, flush=True)
        return []
    # GPSJam returns a list of hex cell objects or a dict with a cells/data key.
    # Handle both shapes defensively.
    if isinstance(data, list):
        cells = data
    elif isinstance(data, dict):
        cells = data.get("cells") or data.get("data") or data.get("features") or []
    else:
        cells = []
    return cells


def normalize_gpsjam(cell: dict) -> dict | None:
    """Normalize one GPSJam hex cell to the common schema."""
    # GPSJam hex cells carry lat/lon of cell centre + a jamming probability score.
    # Field names vary by API version — try multiple known keys.
    lat = cell.get("lat") or cell.get("latitude") or cell.get("center_lat")
    lon = cell.get("lon") or cell.get("longitude") or cell.get("center_lon")
    # H3 hex cell objects from a GeoJSON-style feed use geometry.coordinates
    if lat is None and "geometry" in cell:
        coords = cell["geometry"].get("coordinates", [None, None])
        lon, lat = coords[0], coords[1]
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not _in_bbox(lat, lon):
        return None

    prob = float(cell.get("prob") or cell.get("probability") or
                 cell.get("properties", {}).get("prob", -1))
    if 0 <= prob < JAM_THRESHOLD:
        return None   # below noise threshold

    # H3 resolution 3 hex cell ≈ 59 km edge length ≈ 100 km diameter
    radius_km = float(cell.get("radius_km") or 50.0)

    sev = _severity(prob) if prob >= 0 else "unknown"
    return {
        "_ts":        time.time(),
        "_src":       "gpsjam",
        "callsign":   "GPS-JAM {}".format(sev.upper()),
        "lat_deg":    lat,
        "lon_deg":    lon,
        "radius_km":  radius_km,
        "event_type": "jamming",
        "severity":   sev,
        "jam_prob":   round(prob, 3),
        "report_utc": _now_utc(),
        "detail":     "GPSJam hex cell  prob={:.0%}".format(prob) if prob >= 0 else "GPSJam cell",
    }


# ── EUROCONTROL GNSS ──────────────────────────────────────────────────────────

def fetch_eurocontrol() -> list[dict]:
    """Fetch GNSS interference reports from EUROCONTROL."""
    if not EUROCONTROL_KEY:
        return []
    raw = _fetch(EUROCONTROL_URL, headers={"Authorization": "Bearer " + EUROCONTROL_KEY})
    if not raw:
        return []
    try:
        data = json.loads(raw.decode())
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return data
    return data.get("reports") or data.get("events") or data.get("interferences") or []


def normalize_eurocontrol(item: dict) -> dict | None:
    lat = item.get("latitude") or item.get("lat")
    lon = item.get("longitude") or item.get("lon")
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not _in_bbox(lat, lon):
        return None

    etype = item.get("type", "").lower()
    if "spoof" in etype:
        event_type = "spoofing"
    elif "jam" in etype:
        event_type = "jamming"
    else:
        event_type = "unknown"

    sev = item.get("severity", "unknown").lower()
    return {
        "_ts":        time.time(),
        "_src":       "eurocontrol",
        "callsign":   "GPS-{} {}".format(event_type.upper()[:3], sev.upper()),
        "lat_deg":    lat,
        "lon_deg":    lon,
        "radius_km":  float(item.get("radius_nm", 0) or 0) * 1.852,
        "event_type": event_type,
        "severity":   sev,
        "jam_prob":   -1.0,
        "report_utc": item.get("start_time") or item.get("date") or _now_utc(),
        "detail":     item.get("description") or item.get("text") or "",
    }


# ── Custom / SKAI / commercial ────────────────────────────────────────────────

def fetch_custom() -> list[dict]:
    """Fetch from GPSEW_CUSTOM_URL (SKAI or any REST endpoint)."""
    if not CUSTOM_URL:
        return []
    headers = {}
    if CUSTOM_KEY:
        headers["Authorization"] = "Bearer " + CUSTOM_KEY
    raw = _fetch(CUSTOM_URL, headers=headers)
    if not raw:
        return []
    try:
        data = json.loads(raw.decode())
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return data
    if CUSTOM_LIST_KEY and isinstance(data, dict):
        return data.get(CUSTOM_LIST_KEY) or []
    # Try common list keys
    for key in ("threats", "events", "reports", "items", "data", "results"):
        if key in data and isinstance(data[key], list):
            return data[key]
    return []


def normalize_custom(item: dict) -> dict | None:
    """Best-effort normalization for unknown custom source JSON.

    Tries common field naming conventions. Adjust for specific provider.
    """
    lat = (item.get("lat") or item.get("latitude") or
           item.get("center_lat") or item.get("centroid_lat"))
    lon = (item.get("lon") or item.get("longitude") or
           item.get("center_lon") or item.get("centroid_lon"))
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not _in_bbox(lat, lon):
        return None

    raw_type = str(item.get("type") or item.get("event_type") or item.get("threat_type") or "")
    if "spoof" in raw_type.lower():
        event_type = "spoofing"
    elif "jam" in raw_type.lower():
        event_type = "jamming"
    else:
        event_type = "unknown"

    prob = float(item.get("probability") or item.get("confidence") or item.get("prob") or -1)
    sev  = item.get("severity") or item.get("level") or (_severity(prob) if prob >= 0 else "unknown")

    sev_str = str(sev).lower()
    return {
        "_ts":        time.time(),
        "_src":       "custom",
        "callsign":   "GPS-{} {}".format(event_type.upper()[:3], sev_str.upper()),
        "lat_deg":    lat,
        "lon_deg":    lon,
        "radius_km":  float(item.get("radius_km") or item.get("radius") or 0),
        "event_type": event_type,
        "severity":   sev_str,
        "jam_prob":   round(prob, 3),
        "report_utc": (item.get("timestamp") or item.get("time") or
                       item.get("detected_at") or _now_utc()),
        "detail":     str(item.get("description") or item.get("detail") or item.get("text") or ""),
    }


# ── Aggregate poll ────────────────────────────────────────────────────────────

_SOURCES = [
    ("gpsjam",      fetch_gpsjam,      normalize_gpsjam),
    ("eurocontrol", fetch_eurocontrol, normalize_eurocontrol),
    ("custom",      fetch_custom,      normalize_custom),
]


def poll_all() -> list[dict]:
    results = []
    for name, fetch_fn, norm_fn in _SOURCES:
        try:
            raw_items = fetch_fn()
        except Exception as exc:
            print("GPS-EW {}: fetch error: {}".format(name, exc), flush=True)
            continue
        count = 0
        for item in raw_items:
            point = norm_fn(item)
            if point:
                results.append(point)
                count += 1
        if count:
            print("GPS-EW {}: {} threats".format(name, count), flush=True)
    return results


# ── KML layer generation ──────────────────────────────────────────────────────
# KML color format: AABBGGRR  (note: reversed from HTML #RRGGBB)
# Opacity byte aa: 0xcc = ~80%
_KML_STYLE = {
    "high":    ("jam_high",    "cc0000ff"),  # red,    ~80% opaque
    "medium":  ("jam_medium",  "cc0088ff"),  # orange, ~80% opaque
    "low":     ("jam_low",     "cc00ddff"),  # yellow, ~80% opaque
    "spoofing":("spoof_high",  "ccff00ff"),  # magenta for spoofing
    "unknown": ("jam_unknown", "cc888888"),  # grey
}

_R_EARTH_KM = 6371.0

def _hex_ring(lat_c: float, lon_c: float, radius_km: float) -> str:
    """Return KML coordinate string for a regular hexagon of radius_km around (lat_c, lon_c)."""
    verts = []
    for i in range(7):  # 6 vertices + close
        angle = math.radians(60 * i - 30)  # flat-top orientation
        d_lat = math.degrees(radius_km / _R_EARTH_KM * math.cos(angle))
        d_lon = math.degrees(radius_km / _R_EARTH_KM * math.sin(angle)
                             / max(math.cos(math.radians(lat_c)), 1e-9))
        verts.append("{},{},0".format(round(lon_c + d_lon, 6), round(lat_c + d_lat, 6)))
    return " ".join(verts)


def _circle_ring(lat_c: float, lon_c: float, radius_km: float, n: int = 32) -> str:
    """Return KML coordinate string for a circle approximation (n-sided polygon)."""
    verts = []
    for i in range(n + 1):
        angle = math.radians(360 * i / n)
        d_lat = math.degrees(radius_km / _R_EARTH_KM * math.cos(angle))
        d_lon = math.degrees(radius_km / _R_EARTH_KM * math.sin(angle)
                             / max(math.cos(math.radians(lat_c)), 1e-9))
        verts.append("{},{},0".format(round(lon_c + d_lon, 6), round(lat_c + d_lat, 6)))
    return " ".join(verts)


def build_kml(threats: list[dict]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '<Document>',
        '  <name>GPS EW Threats</name>',
        '  <description>GPS jamming and spoofing threat areas — auto-refreshed</description>',
        '  <open>1</open>',
    ]
    # Styles
    for style_id, color in _KML_STYLE.values():
        lines += [
            '  <Style id="{}">'.format(style_id),
            '    <PolyStyle>',
            '      <color>{}</color>'.format(color),
            '      <outline>1</outline>',
            '    </PolyStyle>',
            '    <LineStyle>',
            '      <color>ff{}</color>'.format(color[2:]),  # fully opaque border
            '      <width>1</width>',
            '    </LineStyle>',
            '  </Style>',
        ]
    # One Folder per source for easy toggling in ATAK Overlay Manager
    by_src: dict[str, list[dict]] = {}
    for t in threats:
        by_src.setdefault(t.get("_src", "?"), []).append(t)

    for src_name, items in sorted(by_src.items()):
        lines += ['  <Folder>', '    <name>{}</name>'.format(src_name.upper()), '    <open>1</open>']
        for t in items:
            lat = t.get("lat_deg"); lon = t.get("lon_deg")
            if lat is None or lon is None:
                continue
            radius = float(t.get("radius_km") or 50.0)
            etype  = t.get("event_type", "unknown")
            sev    = t.get("severity",   "unknown")
            prob   = t.get("jam_prob",   -1.0)
            cs     = t.get("callsign",   "GPS EW")
            detail = t.get("detail",     "")
            rep    = t.get("report_utc", "")

            # Style: spoofing always gets magenta; jamming gets severity colour
            if etype == "spoofing":
                style_id = _KML_STYLE["spoofing"][0]
            else:
                style_id = _KML_STYLE.get(sev, _KML_STYLE["unknown"])[0]

            # Use hex polygon for GPSJam cells (H3 hex grid), circle for everything else
            if src_name == "gpsjam":
                coords = _hex_ring(lat, lon, radius)
            else:
                coords = _circle_ring(lat, lon, radius)

            prob_str = "{:.0%}".format(prob) if prob >= 0 else "N/A"
            desc = "<![CDATA[Type: {} | Severity: {} | Prob: {} | Radius: {} km<br/>Src: {} | {}{}]]>".format(
                etype.upper(), sev.upper(), prob_str, round(radius),
                src_name.upper(), rep, "<br/>" + detail if detail else "")

            lines += [
                '    <Placemark>',
                '      <name>{}</name>'.format(cs),
                '      <description>{}</description>'.format(desc),
                '      <styleUrl>#{}</styleUrl>'.format(style_id),
                '      <Polygon>',
                '        <tessellate>1</tessellate>',
                '        <outerBoundaryIs><LinearRing>',
                '          <coordinates>{}</coordinates>'.format(coords),
                '        </LinearRing></outerBoundaryIs>',
                '      </Polygon>',
                '    </Placemark>',
            ]
        lines.append('  </Folder>')
    lines += ['</Document>', '</kml>']
    return "\n".join(lines)


# ── HTTP server for KML layer ─────────────────────────────────────────────────

_kml_lock    = threading.Lock()
_kml_content = b""
_kml_updated = 0.0


def _update_kml(threats: list[dict]) -> None:
    global _kml_content, _kml_updated
    kml = build_kml(threats)
    with _kml_lock:
        _kml_content = kml.encode("utf-8")
        _kml_updated = time.time()


class _KmlHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path not in ("/gps-ew.kml", "/"):
            self.send_response(404); self.end_headers(); return
        with _kml_lock:
            body = _kml_content
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.google-earth.kml+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # silence access log
        pass


def _start_kml_server(port: int) -> None:
    import socket
    srv = http.server.HTTPServer(("0.0.0.0", port), _KmlHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    # Print every non-loopback address so any reachable interface works
    addrs = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                addrs.append(ip)
    except OSError:
        pass
    if not addrs:
        addrs = ["<this-host>"]
    print("GPS-EW KML layer serving on port {} — reachable via any of:".format(port), flush=True)
    for ip in addrs:
        print("  http://{}:{}/gps-ew.kml".format(ip, port), flush=True)
    print("  → ATAK/WinTAK/iTAK: Overlay Manager → Add Layer → Remote KML", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args):
    session = zenoh.open(make_config())
    topic   = "{}/env/gps/ew/hostile/threat/v1".format(ORG)
    pub     = session.declare_publisher(topic)

    active = [n for n, f, _ in _SOURCES
              if n == "gpsjam" or
                 (n == "eurocontrol" and EUROCONTROL_KEY) or
                 (n == "custom"      and CUSTOM_URL)]
    print("GPS-EW bridge: sources={} interval={}s threshold={:.0%}".format(
        active, args.interval, JAM_THRESHOLD), flush=True)
    if not active:
        print("GPS-EW warning: no sources configured — set GPSEW_CUSTOM_URL or GPSEW_EUROCONTROL_KEY", flush=True)

    if args.kml_port > 0:
        _start_kml_server(args.kml_port)

    try:
        while True:
            threats = poll_all()
            for pt in threats:
                pub.put(json.dumps(pt).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
            if args.kml_port > 0:
                _update_kml(threats)
            print("GPS-EW: published {} threat points{}".format(
                len(threats), ", KML updated" if args.kml_port > 0 else ""), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="GPS jamming/spoofing threat bridge")
    ap.add_argument("--interval",  type=int,   default=POLL_INTERVAL,
                    help="Poll interval in seconds (default: {})".format(POLL_INTERVAL))
    ap.add_argument("--threshold", type=float, default=JAM_THRESHOLD,
                    help="Minimum jam probability to publish, 0–1 (default: {})".format(JAM_THRESHOLD))
    ap.add_argument("--kml-port",  type=int,   default=int(os.environ.get("GPSEW_KML_PORT", "8765")),
                    help="HTTP port for KML layer (0 to disable, default: 8765)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
