#!/usr/bin/env python3
"""cmems_bridge.py — Copernicus Marine Service (CMEMS) → Zenoh bridge.

Downloads a small slice of Baltic Sea physics analysis from CMEMS and publishes
sampled grid points as OceanPoint JSON to the EFDI Zenoh fabric.

Requires: pip install copernicusmarine  (already in Dockerfile.cmems)
Free account: https://marine.copernicus.eu/  (instant registration)

Credentials via env vars:
  COPERNICUSMARINE_SERVICE_USERNAME=<user>
  COPERNICUSMARINE_SERVICE_PASSWORD=<pass>

Zenoh topic:  <ORG>/ocean/cmems/<dataset-id>/v1
Proto schema: ocean_point.proto  (message OceanPoint, package ltu.cis.tracks.v1)

Run:
    COPERNICUSMARINE_SERVICE_USERNAME=x COPERNICUSMARINE_SERVICE_PASSWORD=y \\
        venv/bin/python3 cmems_bridge.py
"""

import argparse
import json
import os
import time

import zenoh

ROUTER = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG    = "1851281db70ccc0409dad4ecfc874cf5"
HERE   = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)

POLL_INTERVAL = 21600  # 6 h — Baltic physics analysis updates daily

# Baltic Sea Physics Analysis and Forecast (NEMO model, daily, 1 km)
DATASET_ID = "cmems_mod_bal_phy_anfc_P1D-m"
VARIABLES  = ["thetao", "so", "uo", "vo"]  # SST, salinity, eastward/northward current

# Sample points across the Baltic Sea
SAMPLE_POINTS = [
    {"name": "gotland-basin",   "lat": 57.5, "lon": 20.0},
    {"name": "bornholm-basin",  "lat": 55.5, "lon": 15.5},
    {"name": "gulf-of-finland", "lat": 60.0, "lon": 25.0},
    {"name": "gulf-of-riga",    "lat": 57.5, "lon": 23.5},
    {"name": "lithuanian-coast","lat": 55.7, "lon": 21.0},
    {"name": "gdansk-bay",      "lat": 54.6, "lon": 19.2},
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


def fetch_and_publish(pub_cache: dict, session, dataset_id: str, points: list):
    try:
        import copernicusmarine  # noqa: PLC0415
    except ImportError:
        raise SystemExit(
            "copernicusmarine not installed — use Dockerfile.cmems or "
            "pip install copernicusmarine"
        )

    # Derive a bounding box that covers all sample points (+0.5° margin)
    lats = [p["lat"] for p in points]
    lons = [p["lon"] for p in points]
    margin = 0.5

    print("Opening CMEMS dataset {}…".format(dataset_id), flush=True)
    ds = copernicusmarine.open_dataset(
        dataset_id=dataset_id,
        variables=VARIABLES,
        minimum_latitude=min(lats) - margin,
        maximum_latitude=max(lats) + margin,
        minimum_longitude=min(lons) - margin,
        maximum_longitude=max(lons) + margin,
        minimum_depth=0,
        maximum_depth=1,  # surface layer only
    )

    # Select the latest available time step
    latest_time = ds.time[-1].values
    ds_latest = ds.sel(time=latest_time, method="nearest")
    time_str = str(latest_time)[:19].replace("T", " ")

    now = time.time()
    for pt in points:
        try:
            slice_ = ds_latest.sel(
                latitude=pt["lat"], longitude=pt["lon"], method="nearest"
            )

            def _val(var):
                try:
                    v = float(slice_[var].values.flat[0])
                    return None if (v != v) else round(v, 4)  # NaN check
                except Exception:
                    return None

            point = {
                "_ts":                         now,
                "_src":                        "cmems",
                "dataset_id":                  dataset_id,
                "lat_deg":                     pt["lat"],
                "lon_deg":                     pt["lon"],
                "time_utc":                    time_str,
                "sea_surface_temperature_c":   _val("thetao"),
                "sea_surface_salinity_psu":    _val("so"),
                "eastward_current_ms":         _val("uo"),
                "northward_current_ms":        _val("vo"),
            }
            # Remove None values
            point = {k: v for k, v in point.items() if v is not None}

            key = "{}/sea/surface/{}/v1".format(ORG, dataset_id)
            if key not in pub_cache:
                pub_cache[key] = session.declare_publisher(key)
            pub_cache[key].put(json.dumps(point).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
            print("PUB cmems {} {} SST={}°C sal={}PSU".format(
                pt["name"], time_str,
                point.get("sea_surface_temperature_c", "?"),
                point.get("sea_surface_salinity_psu", "?"),
            ), flush=True)
        except Exception as exc:
            print("CMEMS sample error at {}: {}".format(pt["name"], exc), flush=True)

    ds.close()


def run(args):
    if not os.environ.get("COPERNICUSMARINE_SERVICE_USERNAME"):
        raise SystemExit(
            "Set COPERNICUSMARINE_SERVICE_USERNAME and COPERNICUSMARINE_SERVICE_PASSWORD\n"
            "Register free at https://marine.copernicus.eu/"
        )

    session = zenoh.open(make_config())
    pub_cache: dict = {}

    try:
        while True:
            fetch_and_publish(pub_cache, session, args.dataset, args.points)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        for pub in pub_cache.values():
            pub.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="Copernicus Marine (CMEMS) → Zenoh bridge")
    ap.add_argument("--dataset", default=DATASET_ID,
                    help="CMEMS dataset ID (default: Baltic physics daily)")
    ap.add_argument("--points", type=json.loads, default=SAMPLE_POINTS,
                    help='JSON list of {name,lat,lon} sample points')
    ap.add_argument("--interval", type=int, default=POLL_INTERVAL,
                    help="Re-fetch interval in seconds (default: 21600 = 6 h)")
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
