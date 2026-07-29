# Troubleshooting

Symptom-first fixes for the most common deployment problems. For
infrastructure-level lessons learned (DNS, TLS profiles, atomic writes —
things that don't fit a single symptom), see [`GOTCHAS.md`](GOTCHAS.md).

## Zenoh connection failure

**Symptom:** `zenoh.ZError: Unable to connect to any of [tls/zenoh.efdi...]`

```bash
# 1. Verify router is healthy
docker compose -f compose/docker-compose.yml ps zenoh-router

# 2. Verify endpoint variable is set
echo $ZENOH_LOCAL_ENDPOINT   # expected: tcp/127.0.0.1:7448

# 3. Verify certificate files exist
ls $EFDI_CERT_DIR/*.pem
```

If `compose/.env` was loaded with bare `source compose/.env`, variables are not exported to child processes. Use `./start.sh` (which handles this), or:

```bash
set -a && source compose/.env && set +a
```

## No tracks visible in ATAK

```bash
# 1. Confirm cot-udp is running
kill -0 $(cat $POD_STATE_DIR/.pids/cot-udp.pid) && echo running

# 2. Confirm multicast traffic is leaving the host
sudo tcpdump -i any udp and host 239.2.3.1 and port 6969 -c 5

# 3. Confirm ATAK and server are on the same L2 segment
```

## Giraffe radar icon at 0°N 0°E

The radar has not yet transmitted a CAT-34 message with I034/120 (3D-Position). Wait for the first rotation (~4 s), or set fallback coordinates in `.env`:

```bash
grep CAT48_RADAR compose/.env
```

## Drone detections not publishing

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

## SitaWare units not appearing in ATAK

**1. Verify the bridge is running and polling:**

```bash
tail -f $POD_STATE_DIR/logs/sitaware.log
# Expected: "SitaWare poll: N units published" every SITAWARE_POLL_S seconds
```

**2. Verify credentials and endpoint:**

```bash
curl -s -u "$SITAWARE_USER:$SITAWARE_PASS" "$SITAWARE_URL/..." | python3 -m json.tool | head -20
```

**3. SIDC not mapped — unit appears with wrong icon or not at all:**

SitaWare units without a valid 15-character SIDC are routed to `…/land/sitaware/c2/unknown/unit/…` and rendered as unknown ground units (`a-u-G-U-C`). Check the raw SIDC value in the log:

```bash
grep "sidc=" $POD_STATE_DIR/logs/sitaware.log | head -10
```

## EFDI tracks not appearing in SitaWare HQ

```bash
tail -f $POD_STATE_DIR/logs/sitaware-hq-nvg.log
curl -u "$SITAWARE_HQ_NVG_USER:$SITAWARE_HQ_NVG_PASS" \
  -o /dev/null -w '%{http_code} %{content_type}\n' \
  "http://127.0.0.1:${SITAWARE_HQ_NVG_PORT:-8088}${SITAWARE_HQ_NVG_PATH:-/nvg}"
```

Expected status is `200 application/xml`. In the HQ NVG manager, verify the subscription is unpaused, connected, polling the EFDI host address (not the HQ address), and targets `efdi-live / EFDI Live Tracks`. If TLS is configured, omit `-k` after the issuing CA is trusted. A local `200` plus an HQ connection failure indicates routing, Windows firewall, Linux firewall, or certificate trust—not an NVG conversion failure.

The **Latest replication** timestamp must advance. If it remains old and
**Reload** reports an unknown error, test the same URL from PowerShell on the HQ
host. A connection failure is routing/firewall; HTTP 401 is missing or stale
subscription credentials; success only with `-k` means the feed CA is not
trusted by the account/service performing the import. Fix replication before
replacing a legacy layer, otherwise the replacement layer will remain empty.

The authenticated health endpoint provides server-side evidence without
logging credentials or NVG payloads:

```bash
curl -ksS -u "$SITAWARE_HQ_NVG_USER:$SITAWARE_HQ_NVG_PASS" \
  "https://127.0.0.1:${SITAWARE_HQ_NVG_PORT:-8088}/healthz" | python3 -m json.tool
```

- `successful_requests` remains zero: HQ has not reached the feed.
- `unauthorized_requests` increases: HQ reached it with missing/stale Basic
  credentials.
- `successful_requests` increases while HQ remains Pending: investigate NVG
  parsing or the selected target layer rather than routing or authentication.

Feed access logs contain only the outcome, track count, and client address and
are rate-limited to one line per minute for successful and unauthorized pulls.

## Duplicate process instances

Caused by running `start.sh` twice without stopping:

```bash
pkill -f "_bridge\.py\|cot_layer\|track_fusion"
rm -f $POD_STATE_DIR/.pids/*.pid
./start.sh
```

## Radar icon disappearing from ATAK

The `asterix` bridge publishes a keepalive every 60 s regardless of track activity. If the icon disappears, the bridge has stopped:

```bash
tail -20 $POD_STATE_DIR/logs/asterix.log | grep -E "keepalive|startup|error"
```
