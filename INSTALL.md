# EFDI Moon-Pod — Deployment Guide

> **Platform:** Linux · **Zenoh:** 1.9.0 · **Python:** 3.10+

This guide covers deploying the sensor bridge stack on a Linux host. The stack ingests ASTERIX CAT-48/34 (Giraffe AMB radar), dronuradaras.lt acoustic detection network, Link-16, MAVLink, and SitaWare, routing all tracks through a local Zenoh fabric to ATAK via CoT UDP multicast or TAK Server TCP.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Installation](#2-installation)
3. [Configuration](#3-configuration)
4. [Launching the Stack](#4-launching-the-stack)
5. [ATAK Setup](#5-atak-setup)
6. [Service Reference](#6-service-reference)
7. [Operations](#7-operations)
8. [Troubleshooting](#8-troubleshooting)
9. [Adding a New Bridge](#9-adding-a-new-bridge)

---

## 1. Prerequisites

### Software

| Dependency | Minimum | Verify |
|---|---|---|
| Python | 3.10 | `python3 --version` |
| Docker Engine | 24.0 | `docker --version` |
| Docker Compose | 2.20 | `docker compose version` |
| Git | any | `git --version` |

### Network

| Port / address | Direction | Purpose |
|---|---|---|
| UDP `<CAT48_PORT>` (default 30048) | inbound | Giraffe AMB ASTERIX stream |
| UDP multicast `239.2.3.1:6969` | outbound | CoT delivery to ATAK |
| TCP 7448 | localhost | Local Zenoh router |
| TCP 7447 TLS | outbound | Remote Zenoh router (requires NetBird) |
| HTTPS | outbound | dronuradaras.lt REST API |

ATAK devices must be on the same L2 segment as the server for multicast delivery. Cross-VLAN or cross-subnet deployments require a TAK Server (`cot-tcp` service).

### Certificates

An EFDI-issued `goat-bundle` is required for Zenoh mTLS. Obtain from your EFDI administrator. The bundle is **never stored in this repository**.

---

## 2. Installation

### 2.1 Clone the repository

```bash
git clone <repo-url> efdi-moon-pod
cd efdi-moon-pod
```

### 2.2 Install the goat-bundle

Place the bundle at `$HOME/goat-bundle/` (default path; override with `BUNDLE_DIR`):

```
~/goat-bundle/
├── efdi-ca-root.pem          # CA certificate (public)
├── <NAMESPACE>-cert.pem      # Node certificate
└── <NAMESPACE>-key.pem       # Private key — restrict permissions
```

`<NAMESPACE>` is the hex UUID assigned to your pod (e.g. `<YOUR_NAMESPACE>`).

```bash
# Verify
ls ~/goat-bundle/*.pem
chmod 600 ~/goat-bundle/*-key.pem
```

### 2.3 Create the Python virtual environment

`start.sh` creates the venv automatically on first run. To create it manually:

```bash
python3 -m venv compose/bridge/venv
compose/bridge/venv/bin/pip install eclipse-zenoh==1.9.0
```

> The `eclipse-zenoh` version must be **exactly 1.9.0** — minor version mismatches introduce breaking API changes.

### 2.4 Start the Zenoh router

```bash
docker compose -f compose/docker-compose.yml up -d zenoh-router
```

Verify the container is healthy before proceeding:

```bash
docker compose -f compose/docker-compose.yml ps zenoh-router
# "Status" column must read "healthy"
```

---

## 3. Configuration

```bash
cp compose/.env.example compose/.env
```

Edit `compose/.env`. The file is read by `start.sh` with safe line-by-line parsing — no `eval`, no subshell expansion.

> `compose/.env` is gitignored. **Never commit it.**

### Required fields

```bash
# ── Bundle path ──────────────────────────────────────────────────────────────
BUNDLE_DIR=/home/<user>/goat-bundle

# ── Giraffe AMB radar (ASTERIX CAT-48/34) ───────────────────────────────────
CAT48_PORT=30048               # UDP port the radar transmits on
CAT48_RADAR_SAC=<SAC>          # ASTERIX Source Area Code
CAT48_RADAR_SIC=<SIC>          # ASTERIX Source Identification Code
CAT48_RADAR_NAME=Giraffe AMB   # Callsign displayed in ATAK
```

> **Radar position (lat/lon):** The bridge reads radar position automatically from each CAT-34 north marker via ASTERIX field I034/120. You do **not** need to configure coordinates for a static or mobile radar — position, speed, and course update live. Set `CAT48_RADAR_LAT` / `CAT48_RADAR_LON` only as a fallback for radar systems that do not transmit I034/120, or to show an immediate ATAK marker before the first CAT-34 message arrives.

### Optional fields

```bash
# ── TAK Server (use cot-tcp service instead of cot-udp) ─────────────────────
TAK_HOST=127.0.0.1
TAK_PORT=8087

# ── SitaWare friendly-force tracking ────────────────────────────────────────
SITAWARE_URL=https://sitaware.example.com
SITAWARE_USER=
SITAWARE_PASS=

# ── Link-16 JREAP-C ─────────────────────────────────────────────────────────
LINK16_PORT=                   # Leave empty if no Link-16 source
LINK16_TCP=                    # Set to 1 for TCP mode, empty for UDP

# ── MAVLink ─────────────────────────────────────────────────────────────────
MAVLINK_PORT=
MAVLINK_TCP=
```

---

## 4. Launching the Stack

```bash
./start.sh
```

The interactive launcher displays all services with their readiness state. Toggle by number, then press **Enter** to launch selected services.

```
╔══════════════════════════════════════════════════════════════════╗
║           EFDI Bridge Launcher  —  select services to start      ║
╚══════════════════════════════════════════════════════════════════╝

  Infrastructure
  ──────────────────────────────────────────────────────────
  [ 1] [✓] zenoh          Zenoh message router (Docker)          ready

  Sensor bridges
  ──────────────────────────────────────────────────────────
  [ 2] [✓] asterix        ASTERIX CAT-48/34 radar tracks         ready
  [ 3] [ ] link16         Link-16 JREAP-C datalink               LINK16_PORT not set
  [ 4] [ ] mavlink        MAVLink UAV telemetry                   MAVLINK_PORT not set
  [ 5] [ ] vmf            VMF MIL-STD-47001C messages            VMF_PORT not set
  [ 6] [ ] sitaware       SitaWare friendly force tracking       SITAWARE_URL not set
  [ 7] [ ] dronuradaras   dronuradaras.lt drone detection        ready

  Output layers
  ──────────────────────────────────────────────────────────
  [ 8] [✓] cot-udp        CoT → ATAK UDP multicast 239.2.3.1:6969
  [ 9] [✓] cot-tcp        CoT → TAK Server TCP
  [10] [✓] track-fusion   Radar/ADS-B track correlation
```

**Launcher controls:**

| Input | Action |
|---|---|
| `1`–`10` | Toggle individual service (space-separated for multiple) |
| `a` | Select all ready services |
| `n` | Deselect all |
| Enter | Launch selected services |
| `q` | Quit without launching |

**Recommended deployments:**

| Scenario | Selection |
|---|---|
| Giraffe radar + ATAK multicast | `1 2 8` |
| Giraffe + drone detection + ATAK | `1 2 7 8` |
| All sensors + TAK Server | `a`, then deselect `8` (cot-udp) |
| Radar only, no ATAK (debug) | `1 2 10` |

Processes are tracked via PID files in `.pids/` and log to `logs/<service>.log`.

---

## 5. ATAK Setup

### UDP multicast (same-subnet deployments)

1. **Settings → Network → Multicast** — enable multicast receiver
2. Verify `239.2.3.1:6969` appears in the address list
3. Tracks should appear within one poll cycle (≤ 10 s for drone detections, ≤ 60 s for radar keepalive)

### TAK Server (cross-subnet / cross-VLAN)

Set `TAK_HOST` and `TAK_PORT` in `.env`, then select `cot-tcp` instead of `cot-udp` in the launcher.

### Icon reference

| ATAK appearance | CoT type | Source |
|---|---|---|
| Blue radar dish (with motion trail if mobile) | `a-f-G-E-S-R` | Giraffe AMB site marker |
| Green sensor box | `a-n-G-E-S` | dronuradaras.lt acoustic sensor |
| Red hostile UAV | `a-h-A-M-F-Q` | Drone detection event |
| White unknown aircraft | `a-u-A-C-F` | Unclassified radar track |

> Position, speed, and course on the radar marker update automatically from the live CAT-34 stream. On a mobile platform, ATAK will show a speed vector and movement trail.

---

## 6. Service Reference

| Service | Script | Zenoh topic (abbreviated) | Trigger |
|---|---|---|---|
| `asterix` | `bridges/asterix_bridge.py` | `…/air/asterix/cat48/unknown/aircraft/tracks/v1` | Streaming UDP |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/air/dronuradaras/acoustic/hostile/uav/tracks/v1` | 10 s REST poll |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/rest/friendly/unit/tracks/v1` | Configurable REST |
| `link16` | `bridges/link16_bridge.py` | `…/air/link16/jreap/*/aircraft/tracks/v1` | Streaming UDP/TCP |
| `mavlink` | `bridges/mavlink_bridge.py` | `…/air/mavlink/mav2/*/uav/tracks/v1` | Streaming UDP/TCP |
| `cot-udp` | `layers/cot_layer.py` | Subscriber — all topics | Event-driven |
| `cot-tcp` | `layers/cot_layer.py` | Subscriber — all topics | Event-driven |
| `track-fusion` | `layers/track_fusion_layer.py` | CAT-48 + CAT-21 subscriber | Event-driven |

### Zenoh topic schema

```
{NAMESPACE}/{DOMAIN}/{SOURCE}/{PROTOCOL}/{AFFILIATION}/{TYPE}/tracks/v1
```

| Field | Values |
|---|---|
| `DOMAIN` | `air`, `land`, `sea`, `space`, `env` |
| `AFFILIATION` | `friendly`, `hostile`, `neutral`, `unknown`, `civ`, `mil` |
| `TYPE` | `aircraft`, `vessel`, `vehicle`, `unit`, `sensor`, `uav`, `radar` |

---

## 7. Operations

### Stopping services

```bash
./stop.sh              # Stop all bridge processes
./stop.sh layers       # Stop output layers only (cot-udp, cot-tcp, track-fusion)
```

### Log monitoring

```bash
tail -f logs/asterix.log          # Giraffe radar — ASTERIX decode + publish
tail -f logs/cot-udp.log          # CoT output — confirms ATAK delivery
tail -f logs/dronuradaras.log     # Drone detection events
tail -f logs/track-fusion.log     # Fused track output
```

### Process health check

```bash
ls .pids/                                          # List running services
kill -0 $(cat .pids/asterix.pid) && echo ok        # Check specific service
```

---

## 8. Troubleshooting

### Zenoh connection failure

**Symptom:** `zenoh.ZError: Unable to connect to any of [tls/zenoh.efdi...]`

```bash
# 1. Verify router is healthy
docker compose -f compose/docker-compose.yml ps zenoh-router

# 2. Verify endpoint variable is set
echo $ZENOH_LOCAL_ENDPOINT   # expected: tcp/127.0.0.1:7448

# 3. Verify certificate files exist
ls $GOAT_CERT_DIR/*.pem
```

If `compose/.env` was loaded with bare `source compose/.env`, variables are not exported to child processes. Use `./start.sh` (which handles this), or:

```bash
set -a && source compose/.env && set +a
```

### No tracks visible in ATAK

```bash
# 1. Confirm cot-udp is running
kill -0 $(cat .pids/cot-udp.pid) && echo running

# 2. Confirm multicast traffic is leaving the host
sudo tcpdump -i any udp and host 239.2.3.1 and port 6969 -c 5

# 3. Confirm ATAK and server are on the same L2 segment
```

### Giraffe radar icon at 0°N 0°E

The radar has not yet transmitted a CAT-34 message with I034/120 (3D-Position). Wait for the first rotation (~4 s), or set fallback coordinates in `.env`:

```bash
grep CAT48_RADAR compose/.env
```

### Drone detections not publishing

The bridge discards detections older than 300 s. Verify API connectivity and data freshness:

```bash
curl -s -H "Origin: https://dronuradaras.lt" \
  https://radar-api.mainline.inc/api/v1/public/detections \
  | python3 -c "
import sys, json, time
d = json.load(sys.stdin).get('detections', [])
now = time.time()
fresh = [x for x in d if (now - x.get('detected_at', 0)/1000) < 300]
print(f'{len(fresh)} fresh / {len(d)} total detections')
"
```

### Duplicate process instances

Caused by running `start.sh` twice without stopping:

```bash
pkill -f "_bridge\.py\|cot_layer\|track_fusion"
rm -f .pids/*.pid
./start.sh
```

### Radar icon disappearing from ATAK

The `asterix` bridge publishes a keepalive every 60 s regardless of track activity. If the icon disappears, the bridge has stopped:

```bash
tail -20 logs/asterix.log | grep -E "keepalive|startup|error"
```

---

## 9. Adding a New Bridge

### File structure

```python
# compose/bridge/bridges/<name>_bridge.py

import json, os, time
import zenoh

ORG       = "<YOUR_NAMESPACE>"
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", os.path.dirname(__file__))

# Copy make_config() from any existing bridge — it is identical across all bridges.

def main():
    session = zenoh.open(make_config())
    topic = f"{ORG}/air/<source>/<protocol>/unknown/aircraft/tracks/v1"
    pub = session.declare_publisher(topic)

    while True:
        for item in fetch_data():
            payload = {
                "_src": "<source>", "_ts": time.time(),
                "lat_deg": item["lat"], "lon_deg": item["lon"],
            }
            pub.put(json.dumps(payload).encode(),
                    encoding=zenoh.Encoding.APPLICATION_JSON)
        time.sleep(POLL_INTERVAL)
```

### Minimum required payload fields

```json
{
  "_src":    "source_name",
  "_ts":     1234567890.123,
  "lat_deg": 54.6712,
  "lon_deg": 25.2791
}
```

Optional but recognised by output layers:

```json
{
  "sensor_id":   "unique_id",
  "callsign":    "display_name",
  "speed_ms":    15.2,
  "heading_deg": 270.0,
  "baro_alt_m":  1500.0
}
```

### Registering in `start.sh`

```bash
# 1. Add to SERVICES array
SERVICES=(... <name> ...)

# 2. Add category
[<name>]="Sensor bridges"

# 3. Add description
[<name>]="Short description"

# 4. Add readiness check (or return 0 if always ready)
<name>) [[ "${MY_ENV_VAR:-}" ]] ;;

# 5. Add launch case
<name>)
    _start <name> bridges/<name>_bridge.py ;;
```

### Adding a CoT type (if needed)

In `layers/cot_layer.py`, add to `_TOPIC_COT`:

```python
"air/**/hostile/uav/**":      ("a-h-A-M-F-Q", AIR_STALE_S),
"land/**/neutral/sensor/**":  ("a-n-G-E-S",   LAND_STALE_S * 2),
```

---

## Changelog

| Date | Author | Change |
|---|---|---|
| 2026-06-22 | — | Initial deployment: Giraffe ASTERIX bridge, `start.sh` launcher |
| 2026-06-22 | — | dronuradaras.lt bridge — acoustic sensors + drone detections |
| 2026-06-22 | — | CoT DETECTION section with audio URL in ATAK remarks |
| 2026-06-22 | — | Radar keepalive + startup publish |

---

*Internal use only — do not distribute outside the project.*
