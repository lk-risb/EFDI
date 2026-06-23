# EFDI Moon-Pod — Installation

Pulls data from radars and drone detection sensors, pushes icons to ATAK.

---

## Requirements

- Linux, Python 3.10+, Docker 24+, Docker Compose 2.20+
- Android device with ATAK CIV on the same network
- `goat-bundle` certificates — get from your EFDI admin

---

## 1. Clone

```bash
git clone <repo-url> efdi-moon-pod
cd efdi-moon-pod
```

## 2. Certificates

Place your bundle at `~/goat-bundle/`:

```
~/goat-bundle/
├── efdi-ca-root.pem
├── <YOUR-ID>-cert.pem
└── <YOUR-ID>-key.pem
```

`<YOUR-ID>` is the hex namespace string from your admin (e.g. `1851281db70ccc0409dad4ecfc874cf5`).

> Do not put these in the repo. `~/goat-bundle/` is the right place.

## 3. Config

```bash
cp compose/.env.example compose/.env
nano compose/.env
```

Key values:

```bash
BUNDLE_DIR=/home/<you>/goat-bundle

# Giraffe radar
CAT48_PORT=30048
CAT48_RADAR_LAT=54.9639
CAT48_RADAR_LON=24.0848
CAT48_RADAR_SAC=122
CAT48_RADAR_SIC=65

# TAK server (optional)
TAK_HOST=127.0.0.1
TAK_PORT=8087
```

## 4. Run

```bash
./start.sh
```

Toggle services by number, Enter to launch. Recommended for Giraffe + drone detection + ATAK:

```
1 (zenoh)  2 (asterix)  7 (dronuradaras)  8 (cot-udp)
```

## 5. ATAK

Settings → Network → Multicast → enable, add `239.2.3.1:6969`.

| Icon | Meaning |
|------|---------|
| Blue | Giraffe radar |
| Green | Acoustic sensor node |
| Red | Detected drone |

---

## Stop

```bash
./stop.sh
```

## Logs

```bash
tail -f logs/asterix.log
tail -f logs/cot-udp.log
tail -f logs/dronuradaras.log
```

---

## Troubleshooting

**Nothing in ATAK** — computer and phone on same subnet? Multicast enabled in ATAK? `cot-udp` running?

**"Unable to connect" on start** — Zenoh Docker container not healthy:
```bash
docker compose -f compose/docker-compose.yml up -d zenoh-router
```

**Radar at 0°N 0°E** — `CAT48_RADAR_LAT/LON` missing from `.env`.

**No drone detections** — detections older than 5 min are ignored. Verify API access:
```bash
curl -s -H "Origin: https://dronuradaras.lt" \
  https://radar-api.mainline.inc/api/v1/public/devices | python3 -m json.tool
```

**Duplicate processes** — ran `start.sh` twice:
```bash
pkill -f "asterix_bridge\|cot_layer\|dronuradaras"
rm -f .pids/*.pid && ./start.sh
```
