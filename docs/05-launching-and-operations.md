# 05 — Launching & Operations

## Launching the Stack

```bash
./start.sh
```

The interactive launcher displays all services with their readiness state. Toggle by number, then press **Enter** to launch selected services.

```text
╔══════════════════════════════════════════════════════════════════╗
║           EFDI Bridge Launcher  —  select services to start      ║
╚══════════════════════════════════════════════════════════════════╝

  Infrastructure
  ──────────────────────────────────────────────────────────
  [ 1] [✓] zenoh          Zenoh message router (Docker)          ready

  Open-data bridges
  ──────────────────────────────────────────────────────────
  [ 6] [✓] meteolt        meteo.lt weather stations              ready

  Sensor bridges
  ──────────────────────────────────────────────────────────
  [ 8] [ ] sitaware       SitaWare HQ documented JSON resource   will prompt for address+login
  [ 9] [✓] dronuradaras   dronuradaras.lt drone detection        ready
  [10] [✓] asterix        ASTERIX family bundle                  ready
  [11] [✓] track-fusion   Radar/ADS-B track correlation          ready

  Protocols
  ──────────────────────────────────────────────────────────
  [12] [✓] nffi           NATO NFFI XML Zenoh translator         ready
  [13] [ ] sapient        SAPIENT / BSI Flex 335                 will prompt for address
  [14] [✓] stanag         STANAG family bundle                   ready
  [15] [ ] sapient-raw    SAPIENT socket → Zenoh raw             SAPIENT_RAW_PORT not set
  [16] [ ] stanag4586-raw STANAG 4586 socket → Zenoh raw         STANAG4586_RAW_PORT not set

  Zenoh-native translators
  ──────────────────────────────────────────────────────────
  [17] [✓] cap            CAP 1.2 XML → alerts                   ready
  [18] [✓] geojson        GeoJSON/OGC Features → areas           ready
  [27] [✓] spectrum       RF spectrum observations               ready
  [28] [✓] sensor-health  Sensor health/heartbeat records       ready
  [29] [✓] mission-route  UAV routes and corridors              ready

  TAK and SitaWare layers
  ──────────────────────────────────────────────────────────

  Output layers
  ──────────────────────────────────────────────────────────
  [32] [✓] tak-layer      CoT → TAK Server TCP
  [33] [ ] tak-bridge     TAK Server CoT ingress               will prompt for address
  [34] [ ] sitaware-hq-nvg EFDI tracks → SitaWare HQ pull feed   SITAWARE_HQ_NVG_PORT not set
```

**Launcher controls:**

| Input | Action |
| --- | --- |
| `1`–`38` | Toggle individual service (space-separated for multiple) |
| `a` | Select all ready services |
| `n` | Deselect all |
| Enter | Launch selected services |
| `q` | Quit without launching |

**Recommended deployments:**

| Scenario | Selection |
| --- | --- |
| Giraffe ASTERIX + TAK Server | `zenoh asterix tak-layer` |
| Giraffe + drone detection + TAK Server | `zenoh dronuradaras asterix tak-layer` |
| Giraffe + SitaWare + TAK Server | `zenoh sitaware asterix tak-layer` |
| EFDI tracks polled by SitaWare HQ | `zenoh mission-route` |
| All ready inputs + TAK Server | `a` |
| Radar only, no TAK output (debug) | `zenoh asterix` |

Processes are tracked via PID files in `$POD_STATE_DIR/.pids/` and log to `$POD_STATE_DIR/logs/<service>.log`.

After a successful launch, `start.sh` remembers the selected services and the last TAK/SitaWare endpoint addresses in `$POD_STATE_DIR/launcher-state.env` (mode 600). It also merges any currently running PID-managed services into that selection. On the next interactive launch it displays the complete restored selection and auto-starts it after five seconds; press `c` during the countdown to change it. It never stores passwords, API keys, or certificate material there. Explicit values in `compose/.env` take precedence over remembered addresses.

---

## Operations

### Stopping services

```bash
./stop.sh              # Stop all bridge processes
./stop.sh layers       # Stop output layers only (tak-layer, track-fusion)
```

### Log monitoring

```bash
tail -f $POD_STATE_DIR/logs/asterix.log          # Giraffe radar — ASTERIX decode + publish
tail -f $POD_STATE_DIR/logs/dronuradaras.log     # Drone detection events
tail -f $POD_STATE_DIR/logs/track-fusion.log     # Fused track output
```

### Process health check

```bash
ls $POD_STATE_DIR/.pids/                                          # List running services
kill -0 $(cat $POD_STATE_DIR/.pids/asterix.pid) && echo ok        # Check specific service
```

---
