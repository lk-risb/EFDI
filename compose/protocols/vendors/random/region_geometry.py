"""Region (first-level administrative) outlines for a country, from geoBoundaries gbOpen.

geoBoundaries (wmgeolab, https://www.geoboundaries.org) publishes open boundaries per country; the Russian
Federation's 83 regions and Belarus's 7 come with an ODbL 1.0 licence that asks for credit. This module fetches
the simplified ADM1 GeoJSON through the API, keeps a copy under ${POD_STATE_DIR}/region-geometry (refreshed after
30 days; an old copy is used when the download fails) and hands back, per region, its name and outer rings.

ADM0 gives the country's own outline (borders).

Bridges use it to draw a country as its regions, like the site's own LT/LV/PL/EE/UA zones, and to find the region
an alert source names ("Kaliningrad Oblast", "Mordovia") by region_index() / match_region().
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

API = "https://www.geoboundaries.org/api/current/gbOpen/{}/{}/"
CACHE_TTL_S = 30 * 86400
MAX_BYTES = 24 * 1024 * 1024
CREDIT = "Region outlines: geoBoundaries (wmgeolab, ODbL 1.0) https://www.geoboundaries.org"
_HEADERS = {"User-Agent": "EFDI-region-geometry (+open data, credit geoBoundaries)"}


def _cache_dir() -> str:
    return os.path.join(os.environ.get("POD_STATE_DIR") or "/tmp", "region-geometry")


def _read_url(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=_HEADERS), timeout=timeout) as resp:
        body = resp.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("response over {} bytes".format(MAX_BYTES))
    return body


def _valid(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("features"), list) and bool(data["features"])


def fetch_geojson(iso3: str, cache_dir: str | None = None, now: float | None = None, read=_read_url,
                  level: str = "ADM1") -> dict | None:
    """The simplified `level` (ADM1 regions, ADM0 the country) FeatureCollection for an ISO 3166-1 alpha-3 code, or None if there is neither a
    fresh download nor an old copy."""
    now = time.time() if now is None else now
    cache_dir = cache_dir or _cache_dir()
    path = os.path.join(cache_dir, "{}-{}.geojson".format(iso3.upper(), level))
    cached = None
    try:
        with open(path, encoding="utf-8") as handle:
            cached = json.load(handle)
        if _valid(cached) and now - os.path.getmtime(path) < CACHE_TTL_S:
            return cached
    except (OSError, ValueError):
        cached = None
    try:
        meta = json.loads(read(API.format(iso3.upper(), level)))
        meta = meta[0] if isinstance(meta, list) else meta
        data = json.loads(read(meta["simplifiedGeometryGeoJSON"]))
        if not _valid(data):
            raise ValueError("no features")
    except (urllib.error.URLError, OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        print("region geometry {} download failed: {}{}".format(
            iso3, exc, " - using the old copy" if _valid(cached) else ""), flush=True)
        return cached if _valid(cached) else None
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
        os.replace(path + ".tmp", path)
    except OSError as exc:
        print("region geometry {} not cached: {}".format(iso3, exc), flush=True)
    return data


def _rings(geometry: dict) -> list:
    """Outer rings of a Polygon / MultiPolygon, largest first."""
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    rings = [p[0] for p in polygons if p and p[0]]
    return sorted(rings, key=lambda ring: -abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))))


def regions(iso3: str, cache_dir: str | None = None, read=_read_url, level: str = "ADM1") -> list:
    """[{"id": "RU-BEL", "name": "Belgorod Oblast", "rings": [...]}] for a country (level ADM0: the country itself);
    [] when unavailable."""
    data = fetch_geojson(iso3, cache_dir, read=read, level=level)
    out = []
    for feature in (data or {}).get("features", []):
        props, geometry = feature.get("properties") or {}, feature.get("geometry") or {}
        if geometry.get("type") not in ("Polygon", "MultiPolygon") or not props.get("shapeName"):
            continue
        out.append({"id": props.get("shapeISO") or props.get("shapeID") or props["shapeName"],
                    "name": props["shapeName"], "rings": _rings(geometry)})
    return out


def _norm(name: str) -> str:
    text = re.sub(r"\(.*?\)", " ", str(name or "").lower())
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return re.sub(r"^republic of ", "", text)


def region_index(items: list) -> dict:
    """normalised name -> region, for match_region()."""
    return {_norm(r["name"]): r for r in items}


def match_region(index: dict, name: str) -> dict | None:
    """The region a source calls `name`: "Kaliningrad Oblast" finds "Kaliningrad", "Mordovia" finds
    "Republic of Mordovia", but "Moscow" and "Moscow Oblast" stay two regions."""
    key = _norm(name)
    for candidate in (key, re.sub(r" oblast$", "", key), key + " oblast"):
        if candidate in index:
            return index[candidate]
    return None


def overlaps(ring: list, bbox: tuple) -> bool:
    """True when the ring's bounding box meets bbox = (lon min, lat min, lon max, lat max)."""
    xs, ys = [p[0] for p in ring], [p[1] for p in ring]
    return max(xs) >= bbox[0] and min(xs) <= bbox[2] and max(ys) >= bbox[1] and min(ys) <= bbox[3]


def _in_ring(ring: list, lon: float, lat: float) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def contains(region: dict, lon: float, lat: float) -> bool:
    """True when the point is inside one of the region's outer rings."""
    return any(_in_ring(ring, lon, lat) for ring in region["rings"]
               if min(p[0] for p in ring) <= lon <= max(p[0] for p in ring)
               and min(p[1] for p in ring) <= lat <= max(p[1] for p in ring))
