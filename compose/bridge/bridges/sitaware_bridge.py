#!/usr/bin/env python3
"""sitaware_bridge.py — SitaWare REST API → Zenoh bridge.

Polls a SitaWare Headquarters server for unit positions and publishes each unit
as a JSON track to the EFDI Zenoh fabric so cot_layer.py can forward it to ATAK.

SitaWare uses MIL-STD-2525C / NATO APP-6 SIDC codes (15-char symbol ID) to
describe each unit's affiliation, battle dimension, and type.  This bridge maps
SIDCs to the correct Zenoh topic path so cot_layer.py assigns the right CoT type.

SIDC affiliation (char 2):
    F / A → friendly   → a-f-
    H     → hostile    → a-h-
    N / L → neutral    → a-n-
    U / P → unknown    → a-u-

SIDC battle dimension (char 3):
    A → air    → air/**/friendly|hostile|.../aircraft/tracks/v1
    G → ground → land/**/friendly|hostile|.../unit/tracks/v1
    S → sea    → sea/**/friendly|hostile|.../vessel/tracks/v1
    U → subsurface → sea/**/…/vessel/tracks/v1
    P → space  → space/**/friendly|hostile|.../satellite/tracks/v1
    F → special operations forces → land/**/…/unit/tracks/v1

Configuration (compose/.env):
    SITAWARE_URL=https://10.0.0.1          # base URL of SitaWare server (LAN, primary)
    SITAWARE_URL_FALLBACK=https://100.x.x.x  # optional second base URL (e.g. NetBird mesh IP)
    SITAWARE_USER=admin                    # basic-auth username
    SITAWARE_PASS=secret                   # basic-auth password
    SITAWARE_SOURCE=efdi-live              # source tag written into _src field
    SITAWARE_API_PATH=/rest/v2/units       # endpoint path (default shown)
    SITAWARE_POLL_S=10                     # poll interval in seconds (default 10)
    SITAWARE_TLS_VERIFY=1                  # set 0 to skip certificate check (self-signed)

Both base URLs are tried every poll, preferring whichever one last succeeded —
so it survives losing either the LAN path or the NetBird mesh path without
manual intervention.

Run:
    venv/bin/python3 sitaware_bridge.py
    venv/bin/python3 sitaware_bridge.py --verbose

The bridge handles the most common SitaWare REST response shapes:
    { "units": [...] }        # HQ 2.x array wrapper
    { "data": [...] }         # alternative wrapper
    [...]                     # bare array
    { "unit": {...} }         # single-unit response

Each unit element is normalised — the bridge tries multiple field name variants
so it works across SitaWare HQ 2.x and 3.x without code changes.
"""

import argparse
import json
import os
import time
import urllib.request
import urllib.error
import base64
import math
import ssl

import zenoh
from namespace_prefix import prefix

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = prefix() + "/" + ORG   # org prefix (configurable) precedes the pod namespace
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

_BASE_URL_PRIMARY  = os.environ.get("SITAWARE_URL",          "").rstrip("/")
_BASE_URL_FALLBACK = os.environ.get("SITAWARE_URL_FALLBACK", "").rstrip("/")
_BASE_URLS   = [u for u in (_BASE_URL_PRIMARY, _BASE_URL_FALLBACK) if u]
_BASE_URL    = _BASE_URL_PRIMARY   # kept for --discover, which only needs one host
_active_url_idx = 0                # index into _BASE_URLS of the last-known-good base
_USER        = os.environ.get("SITAWARE_USER",     "")
_PASS        = os.environ.get("SITAWARE_PASS",     "")
_SOURCE      = os.environ.get("SITAWARE_SOURCE",   "sitaware")
_API_PATH    = os.environ.get("SITAWARE_API_PATH", "/rest/v2/units")
_POLL_S      = float(os.environ.get("SITAWARE_POLL_S", "10"))
_TLS_VERIFY  = os.environ.get("SITAWARE_TLS_VERIFY", "1") not in ("0", "false", "no")

# Common fallback API paths tried in order if the primary returns 404
_API_FALLBACKS = [
    "/rest/v2/units",
    "/rest/v1/units",
    "/api/v3/units",
    "/api/v2/units",
    "/sitaware/rest/v2/units",
    "/sitaware/rest/v1/units",
]


# ---------------------------------------------------------------------------
# Zenoh
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
# SIDC → Zenoh topic
# ---------------------------------------------------------------------------

_AFF_SLUG = {
    "F": "friendly", "A": "friendly",   # Assumed Friendly
    "H": "hostile",
    "N": "neutral",  "L": "neutral",    # Exercise Neutral
    "U": "unknown",  "P": "unknown",    # Pending
    "J": "unknown",  "K": "unknown",    # Exercise / Faker
}

_DIM_CONFIG = {
    # sidc_char: (domain, entity, fallback_cot_dim)
    "A": ("air",   "aircraft", "A"),
    "G": ("land",  "unit",     "G"),
    "S": ("sea",   "vessel",   "S"),
    "U": ("sea",   "vessel",   "U"),   # subsurface → sea topic
    "P": ("space", "satellite", "P"),
    "F": ("land",  "unit",      "G"),   # special operations forces
}


def sidc_to_topic(sidc: str) -> str:
    """Map a 15-char SIDC to the appropriate Zenoh topic."""
    sidc = (sidc or "").upper().replace("*", "-").replace("-", "")
    if len(sidc) < 3:
        return "{}/land/sitaware/rest/unknown/unit/tracks/v1".format(TOPIC_ROOT)

    aff_char = sidc[1] if len(sidc) > 1 else "U"
    dim_char = sidc[2] if len(sidc) > 2 else "G"

    aff  = _AFF_SLUG.get(aff_char, "unknown")
    cfg  = _DIM_CONFIG.get(dim_char, ("land", "unit", "G"))
    domain, entity, _ = cfg

    # Preserve SitaWare's explicit affiliation. Routing these through the generic
    # "mil" slot loses friendly/hostile state because those topics infer it from
    # ICAO addresses, which friendly-force records normally do not carry.
    if domain == "air":
        return "{}/air/sitaware/rest/{}/aircraft/tracks/v1".format(TOPIC_ROOT, aff)

    return "{}/{}/sitaware/rest/{}/{}/tracks/v1".format(TOPIC_ROOT, domain, aff, entity)


# ---------------------------------------------------------------------------
# HTTP helper (no external deps — uses stdlib urllib)
# ---------------------------------------------------------------------------

def _make_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not _TLS_VERIFY:
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
    return ctx


def _http_get(url: str) -> dict | list | None:
    """GET url with basic auth. Returns parsed JSON or None on error."""
    req = urllib.request.Request(url)
    if _USER:
        creds = base64.b64encode("{}:{}".format(_USER, _PASS).encode()).decode()
        req.add_header("Authorization", "Basic " + creds)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, context=_make_ssl_ctx(), timeout=10) as resp:
            body = resp.read(10_000_001)
            if len(body) > 10_000_000:
                raise ValueError("SitaWare response exceeds 10 MB")
            return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None          # caller handles 404 as "try next path"
        raise
    except Exception as exc:
        raise RuntimeError("HTTP GET {} failed: {}".format(url, exc)) from exc


def _http_get_any(path: str) -> tuple:
    """GET path against each configured base URL, preferring the last-known-good
    one first. Returns (result, base_url_used). A 404 from the preferred base is
    authoritative (path problem, not a connectivity problem) and is returned
    as-is rather than trying the fallback base. Only connection-level failures
    (timeout, refused, DNS) advance to the next base URL."""
    global _active_url_idx
    order = [_active_url_idx] + [i for i in range(len(_BASE_URLS)) if i != _active_url_idx]
    last_exc = None
    for i in order:
        base = _BASE_URLS[i]
        try:
            result = _http_get(base + path)
            _active_url_idx = i
            return result, base
        except Exception as exc:
            last_exc = exc
            continue
    raise last_exc


def _discover_api_path() -> str:
    """Try fallback paths until one returns 200. Returns the working path."""
    for path in _API_FALLBACKS:
        try:
            result = _http_get(_BASE_URL + path)
            if result is not None:
                print("SitaWare API path: {}".format(path), flush=True)
                return path
        except Exception:
            continue
    raise RuntimeError(
        "Could not reach SitaWare at {} — tried paths: {}".format(
            _BASE_URL, _API_FALLBACKS))


# ---------------------------------------------------------------------------
# Response normalisation
# ---------------------------------------------------------------------------

def _extract_units(raw) -> list:
    """Extract the list of unit dicts from any common SitaWare response shape."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("units", "data", "items", "features", "results"):
            if isinstance(raw.get(key), list):
                return raw[key]
        if "unit" in raw:
            return [raw["unit"]]
    return []


def _get(obj: dict, *paths, default=None):
    """Try multiple dotted key paths; return first non-None match."""
    for path in paths:
        cur = obj
        try:
            for part in path.split("."):
                cur = cur[part]
            if cur is not None:
                return cur
        except (KeyError, TypeError):
            pass
    return default


def normalise_unit(raw: dict) -> dict | None:
    """Map a raw SitaWare unit dict to the common EFDI track format."""
    # Position
    lat = _get(raw, "position.latitude", "position.lat",
               "lat", "latitude", "Latitude")
    lon = _get(raw, "position.longitude", "position.lon",
               "lon", "longitude", "Longitude")
    if lat is None or lon is None:
        return None

    try:
        lat = float(lat); lon = float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    alt_raw = _get(raw, "position.altitude", "position.alt",
                   "altitude", "alt", "Altitude")
    try:
        alt_m = float(alt_raw) if alt_raw is not None else None
    except (TypeError, ValueError):
        alt_m = None
    if alt_m is not None and not math.isfinite(alt_m):
        alt_m = None

    # Identity
    uid      = str(_get(raw, "uid", "id", "unitId", "UnitId", "uuid", default=""))
    name     = str(_get(raw, "name", "label", "callsign", "Name", "Label", default=""))
    sidc     = str(_get(raw, "sidc", "SIDC", "symbolCode", "SymbolCode", default=""))
    callsign = str(_get(raw, "callsign", "Callsign", "name", "label", default=name))
    uid = uid.strip()
    callsign = callsign.strip()
    if not uid and not callsign:
        return None

    # Kinematics
    speed_raw   = _get(raw, "speed", "Speed", "groundSpeed")
    heading_raw = _get(raw, "course", "heading", "Course", "Heading", "direction")

    track = {
        "_ts":         time.time(),
        "_src":        _SOURCE,
        "uid":         uid,
        "callsign":    callsign,
        "lat_deg":     round(lat, 6),
        "lon_deg":     round(lon, 6),
        "sidc":        sidc,
    }
    if alt_m is not None:
        track["alt_m"] = round(alt_m, 1)
    if speed_raw is not None:
        try:
            speed = float(speed_raw)
            if math.isfinite(speed) and speed >= 0:
                track["speed_ms"] = round(speed, 2)
        except (TypeError, ValueError):
            pass
    if heading_raw is not None:
        try:
            heading = float(heading_raw)
            if math.isfinite(heading):
                track["heading_deg"] = round(heading % 360.0, 1)
        except (TypeError, ValueError):
            pass

    return track


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------

def publish_unit(session: "zenoh.Session", track: dict, verbose: bool):
    topic = sidc_to_topic(track.get("sidc", ""))
    session.put(topic, json.dumps(track).encode(),
                encoding=zenoh.Encoding.APPLICATION_JSON)
    if verbose:
        print("PUB sitaware {} {} sidc={} lat={} lon={}".format(
            track.get("callsign") or track.get("uid") or "?",
            topic.split("/")[2],   # domain
            track.get("sidc", "?")[:6],
            round(track.get("lat_deg", 0), 4),
            round(track.get("lon_deg", 0), 4),
        ), flush=True)


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def run(args):
    if not _BASE_URLS:
        print("ERROR: SITAWARE_URL is not set in .env — exiting.", flush=True)
        return

    api_path = _API_PATH
    if args.discover:
        api_path = _discover_api_path()

    session = zenoh.open(make_config())
    print("SitaWare bridge started", flush=True)
    if len(_BASE_URLS) > 1:
        print("  Servers: {} (+{} fallback)".format(_BASE_URLS[0], len(_BASE_URLS) - 1), flush=True)
    else:
        print("  Server : {}{}".format(_BASE_URLS[0], api_path), flush=True)
    print("  Poll   : {}s".format(_POLL_S), flush=True)
    print("  TLS    : {}".format("verify" if _TLS_VERIFY else "skip-verify"), flush=True)
    if not _TLS_VERIFY:
        print("  WARNING: SITAWARE_TLS_VERIFY=0 — certificate verification is OFF. "
              "HTTP Basic Auth credentials for this connection are exposed to anyone "
              "who can MITM the path to the SitaWare server. Only use this for a "
              "known self-signed cert on a trusted network.", flush=True)
    if _USER and any(url.lower().startswith("http://") for url in _BASE_URLS):
        print("  WARNING: SitaWare Basic Auth is being sent over plain HTTP; "
              "use HTTPS or an authenticated encrypted tunnel.", flush=True)

    consecutive_errors = 0
    last_used_base = None
    while True:
        try:
            raw, used_base = _http_get_any(api_path)
            if used_base != last_used_base:
                print("SitaWare active base: {}".format(used_base), flush=True)
                last_used_base = used_base
            if raw is None:
                print("WARN: 404 from SitaWare — check SITAWARE_API_PATH", flush=True)
                time.sleep(_POLL_S)
                continue

            units = _extract_units(raw)
            published = 0
            for raw_unit in units:
                track = normalise_unit(raw_unit)
                if track:
                    publish_unit(session, track, args.verbose)
                    published += 1

            if args.verbose or published:
                print("SitaWare poll: {} units published".format(published), flush=True)
            consecutive_errors = 0

        except KeyboardInterrupt:
            break
        except Exception as exc:
            consecutive_errors += 1
            print("SitaWare error ({}): {}".format(consecutive_errors, exc), flush=True)
            # Back off on repeated failures — max 60s
            time.sleep(min(_POLL_S * consecutive_errors, 60))
            continue

        time.sleep(_POLL_S)

    session.close()


def main():
    ap = argparse.ArgumentParser(description="SitaWare REST → Zenoh bridge")
    ap.add_argument("--discover", action="store_true",
                    help="Auto-discover API path by trying common endpoints")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
