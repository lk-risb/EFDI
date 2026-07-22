# EFDI — Deployment Guide

> **Platform:** Linux · **Zenoh:** 1.9.0 · **Python:** 3.10+

This guide covers deploying the sensor bridge stack on a Linux host. The stack
can ingest mixed ASTERIX categories (the current normalized decoders are
CAT-010, CAT-020, CAT-021, CAT-034, CAT-048, and CAT-062), plus
dronuradaras.lt acoustic detections, Link-16, MAVLink, and SitaWare. An
optional bridge can also ingest declared Lithuanian UTM flights when an
authorized Oro navigacija JSON/GeoJSON export is supplied. This is not a
national live Remote ID feed. All markers are routed through a local Zenoh
fabric to TAK and SitaWare clients.

---

## 1. Prerequisites

### Software

| Dependency | Minimum | Verify |
| --- | --- | --- |
| Python | 3.10 | `python3 --version` |
| Docker Engine | 24.0 | `docker --version` |
| Docker Compose | 2.20 | `docker compose version` |
| Git | any | `git --version` |

### Network

| Port / address | Direction | Purpose |
| --- | --- | --- |
| UDP 50010 (`CAT10_PORT`) | inbound | EFDI CAT-010 convention; configure producer destination to match |
| UDP 50020 (`CAT20_PORT`) | inbound | EFDI CAT-020 convention; configure producer destination to match |
| UDP 50021 (`CAT21_PORT`) | inbound | EFDI CAT-021 convention; configure producer destination to match |
| UDP 50034 (`CAT34_PORT`) | inbound | EFDI CAT-034 convention; configure radar destination to match |
| UDP 50048 (`CAT48_PORT`) | inbound | EFDI CAT-048 convention; configure radar destination to match |
| UDP 50062 (`CAT62_PORT`) | inbound | EFDI CAT-062 convention; configure producer destination to match |
| UDP multicast `239.2.3.1:6969` | outbound | CoT delivery to ATAK |
| UDP `<TAK_UDP_PORT>` (default 8087) | outbound | Optional direct CoT unicast to WinTAK/ATAK |
| TCP 7448 | localhost | Local Zenoh router |
| TCP 7447 TLS | outbound | Remote Zenoh router (requires NetBird) |
| HTTPS 8890 | inbound | Zenoh admin GUI (Caddy-terminated, internal CA — see §10) |
| HTTPS | outbound | dronuradaras.lt APIs |
| HTTPS | outbound | Authorized `utm.ans.lt` JSON/GeoJSON export (optional) |

ATAK devices must be on the same L2 segment as the server for multicast delivery. Cross-VLAN or cross-subnet deployments require a TAK Server (`cot-bridge` service).

### Certificates

For a standalone/development pod, Zenoh mTLS certs can be self-issued without
an external vendor bundle. `scripts/gen-certs.sh <namespace>` generates (once)
an EFDI development root CA under `compose/certs/efdi/`, then signs a leaf
cert+key for the given namespace. Do not distribute that development root key
to managed routers.

The generated material (`efdi-ca-root.pem`, `<NAMESPACE>-cert.pem`, `<NAMESPACE>-key.pem`) lives at `compose/certs/efdi/` — gitignored, never committed. The ignored bundle directory also keeps `tak/`, `sitaware/`, `tests/`, and `zenoh-sandbox/` identities separate. Default path is set by `start.sh`; override with `BUNDLE_DIR` in `compose/.env` if you'd rather keep it outside the repo entirely. Managed deployments use the delegated-CA workflow in section 10 and keep CA private keys under a separate mode-700 runtime directory.

---

## 2. Installation

### 2.1 Clone the repository

```bash
git clone <repo-url> EFDI
cd EFDI
```

### 2.2 Generate certificates

```bash
scripts/gen-certs.sh <namespace>   # e.g. scripts/gen-certs.sh 1851281db70ccc0409dad4ecfc874cf5
```

This produces:

```text
compose/certs/
├── efdi/                     # EFDI Zenoh CA and pod identities
├── sitaware/                 # SitaWare feed CA and server identity
├── tak/                      # TAK Server identity
├── tests/                    # test child/grandchild identities
└── zenoh-sandbox/             # legacy sandbox Zenoh identity
```

`<NAMESPACE>` must match `PARTNER_NAMESPACE` in `compose/.env`.

```bash
# Verify
ls compose/certs/efdi/*.pem
chmod 600 compose/certs/efdi/*-key.pem
```

### 2.3 Create the Python virtual environment

`start.sh` creates the venv automatically on first run. To create it manually:

```bash
python3 -m venv compose/venv
compose/venv/bin/pip install -r compose/requirements.txt
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
# Defaults to compose/certs/ (in-repo, gitignored) if left unset — override only
# to keep certs outside the repo entirely.
#BUNDLE_DIR=/home/<user>/efdi-certs

# ── Runtime state (logs, PID files, Zenoh config/certs) ─────────────────────
# Defaults to compose/state/ (in-repo, gitignored) if left unset.
#POD_STATE_DIR=/var/lib/efdi-pod

# ── Mixed ASTERIX UDP ingress (Giraffe example: CAT-34/48) ──────────────────
# For one mixed ASTERIX UDP feed, list every category present in the stream.
# The independent category translators consume their raw Zenoh topics.
# CAT-010/020/021/062 may be added to this example when present.
ASTERIX_PORT=                  # suggested combined-feed convention: 50000
ASTERIX_BIND=0.0.0.0
ASTERIX_CATEGORIES=34,48
ASTERIX_MULTICAST_GROUP=       # optional
ASTERIX_MULTICAST_INTERFACE=0.0.0.0
ASTERIX_ALLOW_SOURCE=          # optional sender IPv4 address or CIDR

# Separate publisher streams can continue using these direct listeners.
CAT10_PORT=50010               # EFDI private-range convention; configure producer output
CAT20_PORT=50020               # EFDI private-range convention; configure producer output
CAT21_PORT=50021               # EFDI private-range convention; configure producer output
CAT34_PORT=50034               # EFDI private-range convention; configure radar output
CAT48_PORT=50048               # EFDI private-range convention; configure radar output
CAT62_PORT=50062               # EFDI private-range convention; configure producer output
CAT48_RADAR_SAC=<SAC>          # ASTERIX Source Area Code
CAT48_RADAR_SIC=<SIC>          # ASTERIX Source Identification Code
CAT48_RADAR_NAME=Giraffe AMB   # Callsign displayed in ATAK
```

> **Radar position (lat/lon):** The bridge reads radar position automatically from each CAT-34 north marker via ASTERIX field I034/120. You do **not** need to configure coordinates for a static or mobile radar — position, speed, and course update live. Set `CAT48_RADAR_LAT` / `CAT48_RADAR_LON` only as a fallback for radar systems that do not transmit I034/120, or to show an immediate ATAK marker before the first CAT-34 message arrives.

> **ASTERIX ports:** ASTERIX specifies the message format, not a registered
> network port. In the radar/gateway management interface, set the EFDI host as
> the destination and use the EFDI category convention: CAT-010→UDP 50010,
> CAT-020→50020, CAT-021→50021, CAT-034→50034, CAT-048→50048, CAT-062→50062.
> These are EFDI conventions, not known vendor factory defaults. Confirm
> transport, category edition, combined/separate streams, and vendor framing in
> the ICD.

For a combined stream, set `ASTERIX_PORT` to its actual destination port. The
`asterix` bundle receives the socket once and republishes intact frames to
`…/raw/asterix/cat34` and `…/raw/asterix/cat48`; the per-category translators
decode only their category. When `ASTERIX_PORT` is set, it takes precedence
only for categories named in `ASTERIX_CATEGORIES`. Inspect an unknown feed first:

```bash
python3 tools/asterix_probe.py --port 30001
```

### Optional fields

```bash
# ── TAK Server (use cot-bridge service instead of cot-udp) ─────────────────────
TAK_HOST=127.0.0.1
TAK_PORT=8087

# ── SitaWare HQ friendly-force tracking (inbound REST pull) ─────────────────
SITAWARE_URL=https://sitaware.example.com
SITAWARE_USER=
SITAWARE_PASS=
SITAWARE_API_PATH=              # required, deployment-specific REST resource

# ── NATO NFFI / ADatP-36 (STANAG 5527) XML already carried by Zenoh ────────
NFFI_INPUT_TOPIC=               # optional; default: …/raw/nffi/*

# ── SitaWare HQ (outbound NVG feed polled by an HQ Import Subscription) ─────
SITAWARE_HQ_NVG_ENABLE=0
SITAWARE_HQ_NVG_BIND=127.0.0.1  # set to the EFDI LAN IP or 0.0.0.0 for HQ
SITAWARE_HQ_NVG_PORT=8088
SITAWARE_HQ_NVG_PATH=/nvg
SITAWARE_HQ_NVG_USER=
SITAWARE_HQ_NVG_PASS=
SITAWARE_HQ_NVG_TLS_CERT=
SITAWARE_HQ_NVG_TLS_KEY=

# ── Link-16 JREAP-C ─────────────────────────────────────────────────────────
LINK16_PORT=                   # Leave empty if no Link-16 source
# Link-16 currently accepts JREAP-C UDP only; TCP needs the gateway framing ICD.

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

```text
╔══════════════════════════════════════════════════════════════════╗
║           EFDI Bridge Launcher  —  select services to start      ║
╚══════════════════════════════════════════════════════════════════╝

  Infrastructure
  ──────────────────────────────────────────────────────────
  [ 1] [✓] zenoh          Zenoh message router (Docker)          ready

  Open-data bridges
  ──────────────────────────────────────────────────────────
  [ 2] [✓] airplaneslive  Airplanes.live ADS-B aircraft          ready
  [ 3] [✓] adsblol        ADSB.lol open-data aircraft            ready
  [ 4] [ ] aisstream      AISstream live vessel positions        will prompt for API key
  [ 5] [✓] aprs           APRS-IS stations, vehicles, vessels    ready
  [ 6] [✓] openmeteo      Open-Meteo weather stations            ready
  [ 7] [✓] meteolt        meteo.lt weather stations              ready
  [ 8] [ ] utm-ans        Lithuanian UTM declared UAV flights    UTM_ANS_API_URL not set

  Sensor bridges
  ──────────────────────────────────────────────────────────
  [ 9] [ ] sitaware       SitaWare HQ documented JSON resource   will prompt for address+login
  [10] [✓] dronuradaras   dronuradaras.lt drone detection        ready
  [11] [ ] dji-cloud      DJI Cloud API aircraft                 DJI_MQTT_HOST not set
  [12] [✓] asterix        ASTERIX family bundle                 ready
  [13] [✓] track-fusion   Radar/ADS-B track correlation          ready

  Protocols
  ──────────────────────────────────────────────────────────
  [14] [ ] link16         Link-16 JREAP-C datalink               LINK16_PORT not set
  [15] [ ] mavlink        MAVLink UAV telemetry                  MAVLINK_PORT not set
  [16] [✓] opendroneid    Raw Open Drone ID Zenoh translator     ready
  [17] [ ] vmf            VMF MIL-STD-47001C messages            VMF_PORT not set
  [18] [✓] nffi           NATO NFFI XML Zenoh translator         ready
  [19] [ ] sapient        SAPIENT / BSI Flex 335                 will prompt for address
  [20] [✓] stanag         STANAG family bundle                   ready
  [21] [ ] mavlink-raw    MAVLink socket → Zenoh raw             MAVLINK_RAW_PORT not set
  [22] [ ] link16-raw     Link-16 socket → Zenoh raw             LINK16_RAW_PORT not set
  [23] [ ] vmf-raw        VMF socket → Zenoh raw                 VMF_RAW_PORT not set
  [24] [ ] sapient-raw    SAPIENT socket → Zenoh raw             SAPIENT_RAW_PORT not set
  [25] [ ] stanag4586-raw STANAG 4586 socket → Zenoh raw         STANAG4586_RAW_PORT not set

  Zenoh-native translators
  ──────────────────────────────────────────────────────────
  [28] [✓] cap            CAP 1.2 XML → alerts                   ready
  [29] [✓] geojson        GeoJSON/OGC Features → areas           ready
  [30] [✓] ais-nmea       AIS NMEA → vessel tracks               ready
  [31] [✓] spectrum       RF spectrum observations               ready
  [32] [✓] sensor-health  Sensor health/heartbeat records       ready
  [33] [✓] mission-route  UAV routes and corridors              ready

  TAK and SitaWare layers
  ──────────────────────────────────────────────────────────

  Output layers
  ──────────────────────────────────────────────────────────
  [34] [✓] cot-udp        CoT → ATAK UDP multicast 239.2.3.1:6969
  [35] [ ] cot-udp-tak    CoT → WinTAK/ATAK UDP unicast
  [36] [✓] cot-bridge     CoT → TAK Server TCP
  [37] [ ] tak-bridge     TAK Server CoT ingress               will prompt for address
  [38] [ ] sitaware-hq-nvg EFDI tracks → SitaWare HQ pull feed   SITAWARE_HQ_NVG_ENABLE=0
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
| Giraffe ASTERIX + ATAK multicast | `zenoh asterix cot-udp` |
| Giraffe + drone detection + ATAK | `zenoh dronuradaras asterix cot-udp` |
| Giraffe + SitaWare + ATAK multicast | `zenoh sitaware asterix cot-udp` |
| AIS vessels polled by SitaWare HQ | `zenoh aisstream` |
| EFDI tracks polled by SitaWare HQ | `zenoh mission-route` |
| All ready inputs + TAK Server | `a`, then deselect `cot-udp` |
| Radar only, no TAK output (debug) | `zenoh asterix` |

Processes are tracked via PID files in `$POD_STATE_DIR/.pids/` and log to `$POD_STATE_DIR/logs/<service>.log`.

After a successful launch, `start.sh` remembers the selected services and the last TAK/SitaWare endpoint addresses in `$POD_STATE_DIR/launcher-state.env` (mode 600). It also merges any currently running PID-managed services into that selection. On the next interactive launch it displays the complete restored selection and auto-starts it after five seconds; press `c` during the countdown to change it. It never stores passwords, API keys, or certificate material there. Explicit values in `compose/.env` take precedence over remembered addresses.

`aisstream` requires an AISstream API key. Select the `aisstream` service and enter the key
at its hidden prompt for a one-run secret, or set `AISSTREAM_KEY` only in the
ignored runtime file `compose/.env`. The key is passed through the environment,
not a command-line argument, and is never copied into launcher memory.

---

## 5. ATAK Setup

### UDP multicast (same-subnet deployments)

1. **Settings → Network → Multicast** — enable multicast receiver
2. Verify `239.2.3.1:6969` appears in the address list
3. Tracks should appear within one poll cycle (≤ 10 s for drone detections, ≤ 60 s for radar keepalive)

### TAK Server (cross-subnet / cross-VLAN)

Set `TAK_HOST` and `TAK_PORT` in `.env`, then select `cot-bridge` instead of `cot-udp` in the launcher.

### Direct WinTAK/ATAK UDP (no TAK Server)

Set `TAK_UDP_HOST=<client-ip>` and `TAK_UDP_PORT=<port>` in `compose/.env`, select `cot-udp-tak`, and configure a matching UDP input on the client. Allow that inbound UDP port through the client firewall. This sends CoT directly to one client and does not use TAK Server certificates.

### SitaWare HQ REST tracking (optional inbound adapter)

Use `sitaware` only when the target deployment documents a compatible JSON unit resource and authentication method. A `/rest/v2/*` servlet mapping does not imply that `/rest/v2/units` exists; that guessed resource returns 404 on the verified HQ 6.22 installation.

Leave `SITAWARE_URL`/`SITAWARE_USER`/`SITAWARE_PASS` unset in `.env` and the launcher prompts for the server address and login (username, then hidden password input) each time you select `sitaware` — or pre-fill them in `.env` to skip the prompt. (A second address can still be set via `SITAWARE_URL_FALLBACK` directly in `.env` for a genuine LAN-vs-mesh split — the interactive prompt only asks for one.)

**`.env` fields:**

```bash
SITAWARE_URL=https://<sitaware-host>
SITAWARE_URL_FALLBACK=https://<netbird-mesh-ip>   # optional second path
SITAWARE_USER=<username>
SITAWARE_PASS=<password>
SITAWARE_API_PATH=/<documented-resource-path>
SITAWARE_POLL_S=10   # optional — poll interval in seconds (default 10)
```

The bridge reads MIL-STD-2525B SIDC codes from SitaWare and routes each unit to the correct Zenoh topic by affiliation and battle dimension:

| SIDC affiliation | SIDC dimension | Zenoh topic path | ATAK CoT type |
| --- | --- | --- | --- |
| Friendly / Assumed Friendly | Ground (G) | `…/land/sitaware/rest/friendly/unit/…` | `a-f-G-U-C` |
| Hostile | Ground (G) | `…/land/sitaware/rest/hostile/unit/…` | `a-h-G-U-C` |
| Neutral | Ground (G) | `…/land/sitaware/rest/neutral/unit/…` | `a-n-G-U-C` |
| Friendly | Air (A) | `…/air/sitaware/rest/friendly/aircraft/…` | `a-f-A-M-F` |
| Hostile | Air (A) | `…/air/sitaware/rest/hostile/aircraft/…` | `a-h-A-M-F` |
| Friendly | Sea (S) | `…/sea/sitaware/rest/friendly/vessel/…` | `a-f-S-X-L` |
| Hostile | Sea (S) | `…/sea/sitaware/rest/hostile/vessel/…` | `a-h-S-X-L` |
| Friendly / Hostile / Neutral / Unknown | Space (P) | `…/space/sitaware/rest/<affiliation>/satellite/…` | matching `a-<affiliation>-P` |
| Any | Special operations forces (F) | `…/land/sitaware/rest/<affiliation>/unit/…` | matching ground-unit type |

### NATO NFFI friendly-force protocol translator

`nffi` subscribes to complete NFFI XML documents that a partner receiver or detection system has already published under `…/raw/nffi/{source-id}` in Zenoh. It translates every unit to `…/land/nato/nffi/friendly/unit/tracks/v1`. It owns no TCP client, listener, endpoint, or framing convention. A product-specific connection must live in a separate `_bridge.py` after its endpoint and ICD are known.

NFFI friendly-force interoperability is ADatP-36 / STANAG 5527. STANAG 4677 is the separate dismounted-soldier interoperability family; a 4677 JDSSDM-over-NFFI profile would need a separate, profile-specific implementation.

**`.env` fields:**

```bash
NFFI_INPUT_TOPIC=               # optional; default: …/raw/nffi/*
```

### SitaWare Headquarters (outbound NVG pull feed)

`sitaware-hq-nvg` is the native Python output for an HQ-only deployment. It subscribes to EFDI tracks, keeps a bounded live snapshot, and exposes NVG 2.0.2 over a read-only HTTP(S) endpoint. SitaWare Headquarters polls it through **SitaWare Communication → NVG → NVG Import Subscriptions**. This is separate from the legacy outbound NVG adapter above.

Create an HQ layer first:

```text
Suggested Layer Key: blank
Name:                EFDI Live Tracks
Path:                /efdi-live
Type:                NVG
Persist tracks:      off
```

Configure the feed in `compose/.env`:

```bash
SITAWARE_HQ_NVG_ENABLE=1
SITAWARE_HQ_NVG_BIND=0.0.0.0   # or the EFDI LAN/Tailscale IP if you prefer a pinned listener
SITAWARE_HQ_NVG_PORT=8088
SITAWARE_HQ_NVG_PATH=/nvg
SITAWARE_HQ_NVG_USER=<dedicated-feed-user>
SITAWARE_HQ_NVG_PASS=<dedicated-random-password>
SITAWARE_HQ_NVG_TLS_CERT=/path/to/server-cert.pem
SITAWARE_HQ_NVG_TLS_KEY=/path/to/server-key.pem
SITAWARE_HQ_NVG_STALE_S=120
SITAWARE_HQ_NVG_MAX_TRACKS=10000
```

Start `sitaware-hq-nvg` from `./start.sh`, or use `./run.sh all`. Test from the HQ Windows host without printing operational data:

```powershell
curl.exe -k -u "<feed-user>:<feed-password>" -sS -o NUL `
  -w "HTTP %{http_code} %{content_type}`n" `
  https://<efdi-linux-ip-or-tailscale-ip>:8088/nvg
```

Use `-k` only for the initial connectivity check. Install the feed certificate's issuing CA in the HQ Windows trust store before normal operation.

Create the HQ import subscription:

```text
Subscription Name:         EFDI Live Tracks
Remote Endpoint:           https://<efdi-linux-ip-or-tailscale-ip>:8088/nvg
Target Layer:              efdi-live / EFDI Live Tracks
Request NVG periodically:  yes
Polling Interval:          10 seconds
Reconnect Delay:           90 seconds
Authentication:            enabled, using the dedicated feed credentials
Pause Subscription:        no
```

The endpoint accepts GET/HEAD only. It requires Basic authentication by default, bounds the cache, removes tracks not refreshed within `SITAWARE_HQ_NVG_STALE_S`, and gives each published NVG object a matching `TimeSpan` expiry. When present in the source, standard NVG modifiers and bounded `ExtendedData` carry callsign, registration/ICAO, aircraft or vessel type, squawk, route, source, APRS path/comment, vessel IDs, sensor identity, and other safe scalar fields. The Attributes view reuses the CoT/TAK domain formatter, presenting clean sections rather than raw Python field names. Aircraft expose separate barometric and geometric altitude, primary altitude in metres/feet/flight level, climb/descent rate, selected/target altitude, speed/heading, emergency/autopilot state, and ADS-B quality. Fixed APRS points and dronuradaras.lt detections use the HQ-supported generic neutral equipment-sensor symbol; weather observations use the distinct neutral emplaced-sensor symbol because HQ 6.22 renders standards-native METOC symbols as Unknown. Neither is classified as a military-intelligence unit. It refuses cleartext HTTP on a non-loopback address unless `SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP=1` is explicitly set for an isolated lab. Do not use a Keycloak account or password for this feed.

#### One-time cleanup of legacy HQ objects

An NVG 2.0.2 data document has no per-object delete operation. Removing a
track from the EFDI snapshot therefore does not delete a copy that HQ already
imported. Current EFDI objects carry `TimeSpan/end`, but objects imported by an
older feed without that element can remain indefinitely and cannot be repaired
after EFDI has forgotten their URIs.

To remove those legacy objects without mixing them with live tracks:

1. Confirm the EFDI feed returns HTTP 200 and contains current objects.
2. Pause the existing import subscription.
3. Create a fresh NVG layer with **Persist tracks** set to **off**.
4. Retarget or recreate the subscription against that fresh layer and resume
   polling.
5. Confirm current EFDI objects appear and carry recent timestamps, then delete
   the old layer containing the legacy objects.

Do not clear a shared operational layer, and do not point the SitaWare Edge
per-item push adapter at an HQ endpoint to work around this limitation.

### Icon reference

| ATAK appearance | CoT type | Source |
| --- | --- | --- |
| Blue radar dish (with motion trail if mobile) | `a-f-G-E-S-R` | Giraffe AMB site marker |
| Blue ground unit | `a-f-G-U-C` | SitaWare friendly ground unit |
| Red ground unit | `a-h-G-U-C` | SitaWare hostile ground unit |
| Yellow/green ground unit | `a-n-G-U-C` | SitaWare neutral ground unit |
| Blue aircraft | `a-f-A-M-F` | SitaWare friendly air unit |
| Red aircraft | `a-h-A-M-F` | SitaWare hostile air unit |
| Blue vessel | `a-f-S-X-L` | SitaWare friendly vessel |
| Red vessel | `a-h-S-X-L` | SitaWare hostile vessel |
| Green/yellow/red sensor box (same icon, recolors) | `a-n-G-E-S` / `a-u-G-E-S` / `a-h-G-E-S` | currently-online dronuradaras.lt acoustic sensor — green=idle, yellow=cooling down, red=active detection (last 60s); offline devices are removed |
| White unknown aircraft | `a-u-A-C-F` | Unclassified radar track |

> Position, speed, and course on the radar marker update automatically from the live CAT-34 stream. On a mobile platform, ATAK will show a speed vector and movement trail.

---

## 6. Service Reference

| Service | Script | Zenoh topic (abbreviated) | Trigger |
| --- | --- | --- | --- |
| `asterix` | `protocols/asterix.py` | `…/raw/asterix/catNN` and category-specific normalized ASTERIX topics | Family bundle: mixed UDP ingress plus per-category translators |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/status/v1` | 60 s online-only device poll with offline eviction / 10 s detection poll |
| `utm-ans` | `bridges/utm_ans_bridge.py` | `…/air/utm_ans/utm/unknown/uav/tracks/v1` | Authorized JSON/GeoJSON declared-flight poll; requires `UTM_ANS_API_URL` |
| `opendroneid` | `protocols/opendroneid.py` | `…/air/opendroneid/astm-f3411/*/uav/tracks/v1` | Raw receiver publications under `…/raw/opendroneid/**`; no radio on the router host |
| `aisstream` | `bridges/aisstream_ws_bridge.py` | `…/sea/aisstream/ais/civ/vessel/tracks/v1` | Authenticated WSS stream |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/rest/friendly/unit/tracks/v1` | Configurable REST poll |
| `nffi` | `protocols/nffi.py` | `…/land/nato/nffi/friendly/unit/tracks/v1` | Complete XML documents under `…/raw/nffi/*` in Zenoh |
| `link16` | `protocols/link16.py` | `…/air/link16/jreap/*/aircraft/tracks/v1` | Streaming UDP |
| `mavlink` | `protocols/mavlink.py` | `…/air/mavlink/mav2/*/uav/tracks/v1` | Streaming UDP/TCP |
| `stanag` | `protocols/stanag.py` | `…/raw/stanag4609/klv`, `…/air/stanag4609/unknown/uav/tracks/v1`, and STANAG 4586 track topics | Family bundle: 4586 feed plus 4609 SRT/KLV |
| `mavlink-raw`, `link16-raw`, `vmf-raw`, `sapient-raw`, `stanag4586-raw` | `bridges/*_raw_bridge.py` | `…/raw/<protocol>/<source>` | Optional socket ingress; matching protocol runs with `*_ZENOH_RAW=1` |
| `cap` | `protocols/cap.py` | `…/land/cap/neutral/sensor/alerts/v1` | Complete CAP 1.2 XML on `…/raw/cap/**` |
| `geojson` | `protocols/geojson_features.py` | `…/land/ogc/neutral/zone/features/v1` | GeoJSON/OGC Features on `…/raw/geojson/**` |
| `ais-nmea` | `protocols/ais_nmea.py` | `…/sea/ais/nmea/civ/vessel/tracks/v1` | AIVDM/AIVDO on `…/raw/ais/**` |
| `spectrum` / `sensor-health` / `mission-route` | Matching `protocols/*.py` | `…/land/spectrum/**`, `…/land/health/**`, `…/air/mission/**` | JSON on their `…/raw/**` topics |
| `dji-cloud` | `bridges/dji_cloud_api_bridge.py` | `…/air/dji/cloud-api/friendly/uav/tracks/v1` | Source-specific authenticated DJI MQTT 5 bridge |
| `cot-udp` | `layers/cot_layer.py` | Subscriber — all topics | Event-driven |
| `cot-bridge` | `bridges/cot_bridge.py` | Subscriber — all topics | Event-driven |
| `tak-bridge` | `bridges/tak_bridge.py` | Subscriber — all topics | TAK-visible CoT ingress |
| `sitaware-hq-nvg` | `bridges/nvg_bridge.py` | Subscriber — all track topics | Pull-based NVG snapshot |
| `track-fusion` | `bridges/track_fusion_bridge.py` | CAT-48 + CAT-21 subscriber | Event-driven |

### TAK users and external CoT sources

### Zenoh-native raw ingress

For a receiver host that should own the network socket, select the matching
`*-raw` bridge and set its raw port. Select the protocol translator separately
with its `*_ZENOH_RAW=1` setting. For example:

```dotenv
MAVLINK_RAW_PORT=14550
MAVLINK_ZENOH_RAW=1
MAVLINK_RAW_TOPIC=                 # defaults to …/raw/mavlink/<hostname>
```

The raw bridge publishes octets only; it does not classify or alter them. The
MAVLink, Link-16, VMF, SAPIENT/FLEX 335, and STANAG 4586 translators consume
those Zenoh topics and publish normalized JSON. Link-16 TCP remains disabled
until the gateway provides a documented stream-framing ICD. VMF, SAPIENT, and
STANAG 4586 TCP ingress use their documented length/header framing; confirm the
edition and vendor profile before live use.

CAP, GeoJSON, AIS NMEA, spectrum, health, and route translators are idle-safe
Zenoh subscribers. A partner publishes complete JSON/XML/NMEA payloads below
the corresponding `raw/**` topic; no internet URL or receiver is embedded in
the translator.

CoT and SitaWare HQ NVG outputs apply the same scenario affiliation policy:
aircraft in the configured RU/BY ICAO address ranges and vessels with RU/BY
MMSI MIDs are hostile; other public ADS-B/AIS contacts remain neutral. An
origin-country label alone does not override an invalid or missing transponder
identifier.

`tak-bridge` is the inverse CoT path: it connects to a TAK-visible CoT feed
over the documented TCP/TLS session, extracts complete `<event>...</event>`
frames, and republishes normalized JSON into Zenoh. It does not replace the
CoT output layer and it does not use Zenoh as the TAK wire transport.

## C2 ↔ Zenoh bidirectional runbook

The directions are independent. Complete only the paths exposed and licensed
by the actual deployment, then select their services in `./start.sh`.

### 1. Verify the common Zenoh side

Keep every Python adapter pointed at the local router:

```dotenv
ZENOH_LOCAL_ENDPOINT=tcp/127.0.0.1:7448
```

Set `ZENOH_FABRIC_ENDPOINT` only for the `zenoh-router`, or use the
`ZENOH_FABRIC_ENDPOINTS` JSON array for two or more explicitly configured
uplinks. Bridges and layers do not connect directly to changing backbone
addresses. C2-origin records are
published below `{NAMESPACE_PREFIX}/{PARTNER_NAMESPACE}/...`. Federation ACLs
decide which partner routers can receive that namespace.

### 2. Zenoh → TAK Server

Configure the TAK TCP destination and select `cot-bridge`:

```dotenv
TAK_HOST=<tak-server>
TAK_PORT=8089
TAK_TLS=1
TAK_CERT=/runtime/path/tak-client.pem
TAK_KEY=/runtime/path/tak-client-key.pem
TAK_CA=/runtime/path/tak-ca.pem
```

These must be TAK-issued credentials. The Zenoh certificate is not valid for
TAK Server. For lab plaintext TCP use the deployment's configured TCP port and
leave `TAK_TLS=0`; direct `cot-udp`/`cot-udp-tak` output is one-way and does not
provide a return feed.

On the TAK Server side:

1. Sign in to the TAK Server administration UI with an administrator identity.
2. Open **User Management** and create a dedicated EFDI client identity; do not
   reuse a human operator account.
3. Assign the mission groups EFDI must publish to and the mission groups it
   must observe. For the `efdi-bridge` client identity, give the broadest
   authorized visibility the deployment allows so the same CoT session can both
   publish and receive server-visible markers.
4. Use the deployment's certificate/enrollment workflow to issue a client
   certificate for that identity and export its certificate, private key and
   TAK CA chain. Current TAK Server exposes user/group and certificate-manager
   operations in its [official API](https://docs.tak.gov/api/takserver); exact
   buttons differ between file-user, LDAP and external-identity deployments.
5. Place the PEM files in a runtime-only directory on the EFDI host, enter their
   paths above, select `cot-bridge` in `./start.sh`, and confirm the identity appears
   as connected in TAK Server.

### 2b. TAK Server → Zenoh

Use the same TAK-issued client identity for the reverse CoT feed, typically the
dedicated `efdi-bridge` account/certificate. Select `tak-bridge` and point it
at the TAK Server CoT endpoint:

```dotenv
TAK_HOST=<tak-server>
TAK_PORT=8089
TAK_TLS=1
TAK_CERT=/runtime/path/efdi-bridge.pem
TAK_KEY=/runtime/path/efdi-bridge-key.pem
TAK_CA=/runtime/path/tak-ca.pem
```

The bridge uses the same TAK session model as a normal client: if the server
authorizes the identity for both directions, it can publish into TAK and
subscribe to server-visible CoT at the same time. The bridge republishes the
received `<event>...</event>` frames into Zenoh and marks them as TAK ingress so
the outbound CoT layer does not loop them straight back into the server.

### 3. Zenoh → SitaWare HQ

Enable `sitaware-hq-nvg`, configure TLS and dedicated feed credentials, then
create an HQ NVG Import Subscription pointing to the resulting
`SITAWARE_HQ_NVG_PATH`:

```dotenv
SITAWARE_HQ_NVG_ENABLE=1
SITAWARE_HQ_NVG_BIND=<efdi-address>
SITAWARE_HQ_NVG_PORT=8088
SITAWARE_HQ_NVG_PATH=/nvg
SITAWARE_HQ_NVG_USER=<dedicated-feed-user>
SITAWARE_HQ_NVG_PASS=<runtime-secret>
SITAWARE_HQ_NVG_TLS_CERT=/runtime/path/feed-cert.pem
SITAWARE_HQ_NVG_TLS_KEY=/runtime/path/feed-key.pem
```

Inside SitaWare HQ, click **SitaWare Communication → NVG → NVG Import
Subscriptions**, create a subscription, and enter:

```text
Subscription Name:         EFDI Live Tracks
Remote Endpoint:           https://<efdi-address-or-tailscale-ip>:8088/nvg
Target Layer:              efdi-live / EFDI Live Tracks
Request NVG periodically:  yes
Polling Interval:          10 seconds
Reconnect Delay:           90 seconds
Authentication:            enabled; use the dedicated feed user/password
Pause Subscription:        no
```

Create the `EFDI Live Tracks` NVG layer first if it is absent. Trust the feed
certificate's issuing CA in Windows; do not leave certificate verification
disabled after the connectivity test.

### 5. SitaWare HQ → Zenoh

This requires a real JSON unit resource documented for that HQ deployment; do
not guess `/rest/v2/units`. Configure and select `sitaware`:

```dotenv
SITAWARE_URL=https://<hq-server>
SITAWARE_USER=<runtime-user>
SITAWARE_PASS=<runtime-secret>
SITAWARE_API_PATH=/<documented-resource-path>
SITAWARE_POLL_S=10
SITAWARE_TLS_VERIFY=1
```

The bridge publishes below `…/{domain}/sitaware/rest/{affiliation}/{entity}/
tracks/v1`. Verify with:

```bash
tail -f "${POD_STATE_DIR:-compose/state}/logs/sitaware.log"
```

On the SitaWare HQ side, the administrator must enable the licensed API, create
a read-only integration account, and grant that account access to the exact
unit/track resource intended for export. Copy these four values from the
installed product's API/ICD into the handover: base URL, resource path,
authentication method, and response schema/version. There is no safe generic
sequence of public HQ menu clicks for this operation and no universal units
resource; if the administrator cannot identify that screen/resource, do not
enable `sitaware`. Use the deployment's NFFI or CoT Gateway interface instead.

### 5. Share C2-origin data with partners

Do not rewrite the record into another partner's namespace. Confirm that the
origin namespace is permitted by the router/federation policy and that the
receiving partner subscribes to it. Their `cot-*` or `sitaware-hq-nvg` output
layers will translate authorized normalized topics in the same way as locally
generated sensor data.

### 8. Operational-persona test exercise

Use four separate identities or clients in a test. These are operational
personas, not replacements for the Zenoh Admin panel's `superadmin`, `admin`,
and `readonly` roles.

| Persona | Test client and action | EFDI services | Expected result |
| --- | --- | --- | --- |
| C2 operator | A TAK/WinTAK/ATAK or SitaWare HQ operator account observes the configured CoT output. | `cot-bridge` and/or `sitaware-hq-nvg`. | Normalized EFDI tracks appear in the authorized C2 system. |
| Sensor publisher | A receiver/detection system attached to a local Zenoh router publishes complete frames/documents to that protocol's `…/raw/<protocol>/<source-id>` topic. For a lab publisher, an admin can generate a script in **Publish Script** after entering that publisher's current router endpoint. | The matching protocol translator and desired C2 output layers. | The translator creates normalized EFDI tracks; the C2 systems show derived markers, not the raw frame. |
| Fabric admin | A separate Zenoh Admin panel account manages router/federation configuration only. | Infrastructure/admin UI; no sensor or C2 feed is required. | May perform its assigned panel actions but is not an operational TAK/SitaWare identity. |

For a first exercise, use a dedicated TAK-issued service identity for `cot-bridge`
and confirm the authorized C2 system receives normalized EFDI tracks. Keep raw
sensor publication on a distinct sensor identity/topic; it must not impersonate
an operator identity.

The current router ACL is namespace-scoped, not yet persona/certificate-scoped.
The four test clients prove data flow and C2 behaviour; they do **not** prove
least-privilege Zenoh authorization between personas. Enforced persona access
needs a subsequent certificate-subject ACL design with separate client
credentials and topic permissions.

> **ASTERIX editions:** CAT-48 follows EUROCONTROL Edition 1.32 and CAT-34 follows Edition 1.29. CAT-20, CAT-21, and CAT-62 currently use legacy compatibility UAPs and print a warning when enabled; do not connect modern CAT-20 1.9, CAT-21 2.2+, or CAT-62 1.21 feeds until their exact decoder profiles are implemented. Link-16 accepts UDP only because the gateway's TCP framing is not yet documented.

### Zenoh topic schema

```text
{NAMESPACE}/{DOMAIN}/{SOURCE}/{PROTOCOL}/{AFFILIATION}/{TYPE}/tracks/v1
```

| Field | Values |
| --- | --- |
| `DOMAIN` | `air`, `land`, `sea`, `space`, `env` |
| `AFFILIATION` | `friendly`, `hostile`, `neutral`, `unknown`, `civ`, `mil` |
| `TYPE` | `aircraft`, `vessel`, `vehicle`, `unit`, `sensor`, `uav`, `radar` |

---

## 7. Operations

### Stopping services

```bash
./stop.sh              # Stop all bridge processes
./stop.sh layers       # Stop output layers only (cot-udp, cot-bridge, track-fusion)
```

### Log monitoring

```bash
tail -f $POD_STATE_DIR/logs/asterix.log          # Giraffe radar — ASTERIX decode + publish
tail -f $POD_STATE_DIR/logs/cot-udp.log          # CoT output — confirms ATAK delivery
tail -f $POD_STATE_DIR/logs/dronuradaras.log     # Drone detection events
tail -f $POD_STATE_DIR/logs/track-fusion.log     # Fused track output
```

### Process health check

```bash
ls $POD_STATE_DIR/.pids/                                          # List running services
kill -0 $(cat $POD_STATE_DIR/.pids/asterix.pid) && echo ok        # Check specific service
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
ls $EFDI_CERT_DIR/*.pem
```

If `compose/.env` was loaded with bare `source compose/.env`, variables are not exported to child processes. Use `./start.sh` (which handles this), or:

```bash
set -a && source compose/.env && set +a
```

### No tracks visible in ATAK

```bash
# 1. Confirm cot-udp is running
kill -0 $(cat $POD_STATE_DIR/.pids/cot-udp.pid) && echo running

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

### SitaWare units not appearing in ATAK

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

SitaWare units without a valid 15-character SIDC are routed to `…/land/sitaware/rest/unknown/unit/…` and rendered as unknown ground units (`a-u-G-U-C`). Check the raw SIDC value in the log:

```bash
grep "sidc=" $POD_STATE_DIR/logs/sitaware.log | head -10
```

### EFDI tracks not appearing in SitaWare HQ

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

### Duplicate process instances

Caused by running `start.sh` twice without stopping:

```bash
pkill -f "_bridge\.py\|cot_layer\|track_fusion"
rm -f $POD_STATE_DIR/.pids/*.pid
./start.sh
```

### Radar icon disappearing from ATAK

The `asterix` bridge publishes a keepalive every 60 s regardless of track activity. If the icon disappears, the bridge has stopped:

```bash
tail -20 $POD_STATE_DIR/logs/asterix.log | grep -E "keepalive|startup|error"
```

---

## 9. Adding a New Bridge

### File structure

```python
# compose/bridges/<name>_bridge.py

import json, os, time
import zenoh

ORG       = "<YOUR_NAMESPACE>"
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", os.path.dirname(__file__))

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

## 10. Zenoh Admin GUI

A web GUI for viewing router status and editing `zenoh/config.json5` without SSH access, styled after the TAK admin panel (reticle-corner cards, glass sidebar, accent glow, technical grid backdrop).

The Dashboard's "Connected routers" panel lists every other zenoh instance (router or peer) this router has a live link to — pulled from the router's own admin space, same source as the subscriber/queryable topic lists, so it needs no separate configuration beyond the existing `pod-admin-introspect` ACL rule.

### Setup

Add to `compose/.env` (see `compose/.env.example` for the full block):

```bash
ZENOH_ADMIN_DB_USER=zenoh_admin
ZENOH_ADMIN_DB_PASSWORD=<random>
ZENOH_ADMIN_DB_ROOT_PASSWORD=<different-random-value>
ZENOH_ADMIN_DB_PORT=3307                # non-default: avoids clashing with MariaDB/MySQL on 3306
ZENOH_ADMIN_SECRET_KEY=<openssl rand -hex 32>
ZENOH_ADMIN_FIRST_USER=admin
ZENOH_ADMIN_FIRST_PASS=<set once, then blank it out after first login>
```

`ZENOH_ADMIN_FIRST_PASS` only creates the first `superadmin` account if it doesn't already exist — it is safe to blank it out again after the first login (the account persists in MariaDB).

#### One-time PostgreSQL migration

Upgrades preserve the old `${POD_STATE_DIR}/zenoh-admin/pgdata` directory and
create MariaDB in `${POD_STATE_DIR}/zenoh-admin/mariadb`. Stop the admin service,
back up both the pod state and `compose/.env`, add
`ZENOH_ADMIN_DB_ROOT_PASSWORD`, and change an existing
`ZENOH_ADMIN_DB_PORT=5433` to `ZENOH_ADMIN_DB_PORT=3307`. The legacy PostgreSQL
container uses temporary port 55433 during this procedure. Then run the
fail-closed importer:

```bash
cd compose
docker compose stop zenoh-admin zenoh-admin-db
docker compose \
  -f docker-compose.yml \
  -f docker-compose.postgres-migration.yml \
  --profile postgres-migration \
  up --build --abort-on-container-exit --exit-code-from zenoh-admin-db-import \
  zenoh-admin-db zenoh-admin-db-postgres-migration zenoh-admin-db-import
docker compose \
  -f docker-compose.yml \
  -f docker-compose.postgres-migration.yml \
  --profile postgres-migration down
docker compose up -d zenoh-admin-db zenoh-admin zenoh-admin-proxy
```

The importer refuses a non-empty MariaDB target, copies every table in one
transaction, preserves self-references, and verifies row counts before commit.
Do not delete `pgdata` until login, trust inventory, federation, branding, and
audit history have been checked in the rebuilt admin UI. This is a single-node
MariaDB migration; Galera clustering is a separate deployment step.

### Launching

```bash
cd compose
docker compose up -d zenoh-admin-db zenoh-admin zenoh-admin-proxy
```

Then open `https://<pod-host>:8890`.

The panel itself (`zenoh-admin`) binds `127.0.0.1:8895` only — not directly reachable. A Caddy reverse proxy (`zenoh-admin-proxy`) terminates real TLS on `:8890` using Caddy's own internal CA (`local_certs` + `tls internal`, no external ACME/CA dependency), persisted in the `zenoh_admin_caddy_data` volume so the CA survives restarts. Your browser will show a self-signed-certificate warning on first visit — trust Caddy's local CA (or accept the warning) to proceed; there is no public certificate here by design, since this panel isn't meant to be internet-facing.

### Runtime Control page

The TAK-style **Runtime Control** page gives a `superadmin` one place to:

- start, stop, restart, and inspect logs for every registered bridge, protocol translator, raw ingress, and TAK/SitaWare output layer;
- edit endpoints, ports, Zenoh topics, API URLs, and protocol settings;
- show and edit additional deployment-specific `.env` fields already present on the pod;
- enter usernames, passwords, API keys, and tokens without displaying existing secret values.

Native processes remain host PID-managed. `start.sh` and `run.sh all` keep
`admin-control` running on localhost port 18896. The API delegates to those same
launcher scripts rather than creating one container per integration. Set
`EFDI_CONTROL_TOKEN` in `compose/.env` for a bearer token between the admin API
and the local control process. Restart an affected service after saving a
setting so it reads the new environment.

For `./dev.sh up`, the disposable control agent automatically moves to port
18896 when the development/default 8896 is already occupied, and the dev API is
pointed at that selected port.

### Managed router hierarchy and delegated CA

Initialize the first managed router's bounded subordinate CA during an offline
ceremony. The parent/global CA private key is read for this command only and is
not copied into router state:

```bash
scripts/pki/init-router-ca.sh \
  <this-router-namespace> \
  /offline/efdi-global-root.pem \
  /offline/efdi-global-root-key.pem \
  "${POD_STATE_DIR}/pki"
```

Move the global root key back offline immediately. Create the router's non-CA
policy signer, then initialize the optional online leaf issuer beneath the
bounded router CA:

```bash
scripts/pki/init-policy-signer.sh \
  <namespace-prefix>/<this-router-namespace> \
  "${POD_STATE_DIR}/pki/router-ca-cert.pem" \
  "${POD_STATE_DIR}/pki/router-ca-key.pem" \
  "${POD_STATE_DIR}/pki"

scripts/pki/init-step-ca.sh \
  "${POD_STATE_DIR}/pki/router-ca-cert.pem" \
  "${POD_STATE_DIR}/pki/router-ca-key.pem" \
  "${POD_STATE_DIR}/pki/step-ca" \
  <vpn-dns-name-or-ip>
```

The policy key signs delegation and management envelopes but cannot issue
certificates. step-ca receives a generated online intermediate and never keeps
the bounded router-CA key. Set the host paths in `compose/.env`, then restart
`admin-control`:

```bash
EFDI_ROUTER_CA_CERT_PATH=/absolute/runtime/pki/router-ca-cert.pem
EFDI_ROUTER_CA_KEY_PATH=/absolute/runtime/pki/router-ca-key.pem
EFDI_ROUTER_CA_CHAIN_PATH=/absolute/runtime/pki/router-ca-chain.pem
EFDI_POLICY_SIGNER_CERT_PATH=/absolute/runtime/pki/policy-signer-cert.pem
EFDI_POLICY_SIGNER_KEY_PATH=/absolute/runtime/pki/policy-signer-key.pem
EFDI_STEP_CA_STATE_PATH=/absolute/runtime/pki/step-ca
./stop.sh admin-control
./start.sh --service admin-control
```

In **Certificate Authority**, create a single-use invitation for the child
namespace and choose how many further CA levels that child may delegate. The UI
derives the maximum from the issuer certificate; every child depth must be
strictly lower than its parent's X.509 path-length constraint.

On the child, generate and enroll all three identities locally:

```bash
scripts/pki/enroll-router.sh \
  https://<parent-management-host>:8890 \
  <child-namespace> \
  "${BUNDLE_DIR}/efdi" \
  "${POD_STATE_DIR}/pki"
```

The script prompts for the invitation token without placing it in argv and
sends only router-CA, transport, and policy-signer CSRs. The response contains
the complete signed delegation chain, public parent trust, and a one-time link
credential; private keys never leave the child. Configure the printed paths
and run the normal first-boot/rebuild flow. CA private keys remain behind the
localhost host-control boundary.

When the parent has step-ca initialized, enrollment receives a renewable
24-hour transport certificate. On the child, set `EFDI_STEP_CA_URL` to the
parent's VPN URL and optionally override `EFDI_STEP_RENEW_*_PATH`. `start.sh`
and `run.sh` then keep the PID-managed `cert-renewer` running. It checks every
15 minutes, renews within the eight-hour window configured by
`EFDI_STEP_RENEW_BEFORE_SECONDS`, updates the active router certificate,
and restarts the router and admin certificate consumers. Router-CA and policy
authority rotation remains an explicit re-enrollment operation rather than an
automatic online privilege escalation.

Each router can then manage its own direct children. **Zenoh Config** can target
any proven descendant, but the command is signed and forwarded one parent/child
hop at a time. The receiver validates the complete rendered file by starting
the pinned Zenoh binary with networking disabled, atomically activates it,
waits for health, and restores the last-known-good file on failure. **Changes**
shows the revision path and terminal status. Loss of the parent link stops new
upstream commands but does not stop the branch's existing data plane, local
WebUI, or management of its own subnet.

The remote editor always starts from that router's latest reported structured
snapshot. A parent push cannot change the child's identity, listener ports,
fabric CA profile, certificate-name verification policy, or organization
control prefix. Uplink replacement is deliberately two-stage: add the new
endpoint while retaining an existing one, verify the change, then remove the
old endpoint. The child restores its prior config if no remote router session
returns after restart.

Topology and status facts include the complete bounded public delegation proof.
A root verifies every CA signature, policy signer, namespace narrowing, depth,
lifetime, and revocation before displaying a descendant as verified. Generated
ACL activation is deliberately rejected when a root still has an unmanaged
fabric uplink; enroll or migrate that peer before applying managed ACLs.

Run both disposable runtime gates before deployment:

```bash
tests/smoke/loopback.sh
tests/smoke/managed-three-router.sh
```

### Roles

| Role | Dashboard | Config (view) | Config (edit + restart router) | Admin Users |
| --- | --- | --- | --- | --- |
| `readonly` | ✓ | | | |
| `admin` | ✓ | ✓ | | |
| `superadmin` | ✓ | ✓ | ✓ | ✓ |

Saving a config edit first renders the structured fields and validates the full
candidate with the same pinned Zenoh binary used at runtime, with listeners,
connectors, scouting, and plugins disabled for the probe. Only an accepted
candidate is written atomically. The router is restarted and health-checked;
failure restores the last-known-good config and restarts the previous state.

### Config tab fields

The Config tab exposes structured fields, not raw JSON5 — each save re-renders `host/zenoh-router.json5.tmpl` (the same template `first-boot.sh` uses) with the values below, so a saved config can never drift from the template's structure.

| Field | Effect |
| --- | --- |
| Local mTLS port | Mesh-facing listen port for bridges, audit-sink (default 7447) |
| Local TCP port | Plaintext local-only listen port for bridges + this GUI (default 7448) |
| Fabric endpoint | The peer endpoint this pod dials out to — entered as separate Host + Port fields (scheme is always `tls`, never exposed); one-click presets are available for previously-used endpoints |
| Partner namespace | This pod's first-party publish/subscribe prefix (its slot) — **changing this requires the other side of the fabric to also allow the new value in its ACL, or publishes silently stop reaching it** |
| Inbound namespace | Bilateral prefix the fabric publishes TO this pod |
| Verify name on connect | Off by default — the gateway cert SAN binds the mesh IP, not the DNS name dialed; turning this on can break the fabric connection |
| Storage plugin loading | Off means new subscribers no longer get a last-known value via `get()` — publish/subscribe still works |

Three deliberately **not** exposed in the GUI (too easy to lock out every client, including the GUI itself, if misconfigured): `access_control.enabled`, `default_permission`, `enable_mtls`. Edit those directly in `zenoh/config.json5` if ever needed.

#### Endpoint helper usage

The Config page's `Fabric endpoints` section is the helper shown in the screenshot:

- enter a host and port,
- click `Add direct link` to append another `connect.endpoints` entry,
- pick `Root / no upstream` to clear the list, or one of the presets to seed a known endpoint,
- save the config to render the `connect.endpoints` array back into `config.json5`.

The publish-builder has the same shortcut at the raw-config level: `Add to connect.endpoints` inserts a candidate endpoint into the current router config text.

#### Three-router mesh example

For the `zenoh1` / `zenoh2` / `zenoh3` cluster, the consistent pattern is:

```json5
zenoh1: {
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447"] },
  connect: { endpoints: ["tls/zenoh2.efdi.ltu:7447", "tls/zenoh3.efdi.ltu:7447"] },
  transport: { link: { tls: { root_ca_certificate: "/root/.zenoh/certs/efdi_ca.crt", listen_certificate: "/root/.zenoh/certs/zenoh1.pem", listen_private_key: "/root/.zenoh/certs/zenoh1.key", connect_certificate: "/root/.zenoh/certs/zenoh1.pem", connect_private_key: "/root/.zenoh/certs/zenoh1.key", enable_mtls: true, verify_name_on_connect: true } } },
  plugins: { rest: { http_port: 8000 } },
  plugins_loading: { enabled: true },
  access_control: { enabled: true, default_permission: "allow", rules: [], subjects: [], policies: [] }
}
```

```json5
zenoh2: {
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447"] },
  connect: { endpoints: ["tls/zenoh1.efdi.ltu:7447", "tls/zenoh3.efdi.ltu:7447"] },
  transport: { link: { tls: { root_ca_certificate: "/root/.zenoh/certs/efdi_ca.crt", listen_certificate: "/root/.zenoh/certs/zenoh2.pem", listen_private_key: "/root/.zenoh/certs/zenoh2.key", connect_certificate: "/root/.zenoh/certs/zenoh2.pem", connect_private_key: "/root/.zenoh/certs/zenoh2.key", enable_mtls: true, verify_name_on_connect: true } } },
  plugins: { rest: { http_port: 8000 } },
  plugins_loading: { enabled: true },
  access_control: { enabled: true, default_permission: "allow", rules: [], subjects: [], policies: [] }
}
```

```json5
zenoh3: {
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447"] },
  connect: { endpoints: ["tls/zenoh1.efdi.ltu:7447", "tls/zenoh2.efdi.ltu:7447"] },
  transport: { link: { tls: { root_ca_certificate: "/root/.zenoh/certs/efdi_ca.crt", listen_certificate: "/root/.zenoh/certs/zenoh3.pem", listen_private_key: "/root/.zenoh/certs/zenoh3.key", connect_certificate: "/root/.zenoh/certs/zenoh3.pem", connect_private_key: "/root/.zenoh/certs/zenoh3.key", enable_mtls: true, verify_name_on_connect: true } } },
  plugins: { rest: { http_port: 8000 } },
  plugins_loading: { enabled: true },
  access_control: { enabled: true, default_permission: "allow", rules: [], subjects: [], policies: [] }
}
```

If you want a fourth router to join that cluster, add its DNS name or IP to all three `connect.endpoints` lists and make sure the certificate SAN matches the name you dial.

### Isolated test router

For local pub/sub testing without touching the real pod or its fabric connection: `zenoh-router-test`, behind the `test` compose profile (never starts with the rest of the stack).

```bash
cd compose
docker compose --profile test up -d zenoh-router-test
```

Config lives at `${POD_STATE_DIR}/zenoh-test/config.json5` — same certs/namespace/ACL as the real router, but different ports (`7457` mTLS / `7458` TCP, vs. `7447`/`7448`) and **no `connect.endpoints`** (never dials the fabric). Safe to leave running alongside the real router; nothing conflicts.

---

## 11. Continuous Integration

Five workflows in `.github/workflows/` run on every push/PR to `main`:

| Workflow | Checks |
| --- | --- |
| `shellcheck.yml` | Lints every `.sh` script in the repo (`-S warning`) |
| `compose-validate.yml` | Confirms `compose/docker-compose.yml` parses as valid YAML |
| `bridge-syntax.yml` | `py_compile` on every file in `compose/bridges/`, `compose/protocols/`, and `compose/layers/` |
| `zenoh-admin-frontend.yml` | `pnpm type-check` + `pnpm build` for `compose/zenoh-admin/ui` |
| `docker-build.yml` | Builds the flattened `compose/Dockerfile` and the `compose/zenoh-admin` image (no push) |

This catches syntax errors, TypeScript errors, and Dockerfile breakage before merge — it does **not** run the bridges themselves (most need real API keys/network access CI doesn't have).

---

## Changelog

| Date | Change |
| --- | --- |
| 2026-06-14 | Initial commit — forked from official `efdi-moon-pod-main` repository |
| 2026-06-15 | Base bridge adapters wired; repository structure established; README added |
| 2026-06-16 | `airplanes.live` bridge: regional ADS-B feed + worldwide military aircraft tracking |
| 2026-06-16 | Protocol Buffer definitions for track types; contracts now live beside translators in `compose/protocols/` |
| 2026-06-17/18 | Quality-of-life improvements: bridge robustness, layer deduplication, track fusion tuning |
| 2026-06-18 | ASTERIX full-decode design specification document added |
| 2026-06-19/22 | Further bridge and layer improvements; Giraffe ASTERIX bridge complete |
| 2026-06-22 | `dronuradaras.lt` bridge: acoustic sensor network + drone detection events |
| 2026-06-22 | CoT DETECTION section with audio clip URL in ATAK remarks field |
| 2026-06-22 | Radar site marker: startup publish + 60 s keepalive so ATAK never loses the marker |
| 2026-06-23 | Security audit: removed hardcoded API token from `register_topics.sh`; token moved to `$EFDI_PORTAL_KEY` env var |
| 2026-06-23 | Security: personal namespace UUID, email, IP, and vendor slug removed from all tracked files; bridges read `PARTNER_NAMESPACE` from environment |
| 2026-06-23 | Security: `compose/.env` and `register_topics.sh` added to `.gitignore` — credentials stay local only |
| 2026-06-23 | Security: unbounded HTTP body read in `rest-http/bridge.py` capped at 10 MB |
| 2026-06-23 | Documentation overhaul: `INSTALL.md` (English), `DIEGIMAS.md` (Lithuanian), `README.md` rewritten as architecture overview |
| 2026-06-23 | ASTERIX CAT-34 I034/120 decoder: radar self-reports WGS-84 position from live stream — no manual coordinate config required |
| 2026-06-23 | Mobile radar support: position, speed, and course derived from successive I034/120 reports; ATAK shows motion trail on vehicle-mounted radars |
| 2026-07-05 | Zenoh admin GUI: FastAPI + React panel for router status and `config.json5` editing, styled after the TAK admin panel |
| 2026-07-05 | Fixed `zenoh-router.json5.tmpl` drift: template was missing the plaintext `tcp/0.0.0.0:7448` local listen endpoint that the live config already had |
| 2026-07-05 | Zenoh admin GUI config tab: added `verify_name_on_connect` and storage-plugin-loading toggles; fabric endpoint now entered as separate Host/Port fields with one-click presets instead of a raw `tls/host:port` string |
| 2026-07-05 | Zenoh admin GUI: added `/api/health` (CPU/RAM/disk/uptime/load/network/cert-expiry, TAK-admin-panel style) to the dashboard |
| 2026-07-05 | Fixed SPA routing bug: direct navigation/refresh/back-button to any GUI sub-route (`/config`, `/admin-users`) 404'd as raw JSON instead of loading the app — the fallback code caught `fastapi.HTTPException`, but `StaticFiles.get_response` raises `starlette.exceptions.HTTPException` (a different, parent class), so the catch never matched |
| 2026-07-05 | Added isolated `zenoh-router-test` service (`test` compose profile) for local pub/sub testing without touching the real pod or its fabric connection |
| 2026-07-05 | Removed the `gps-ew` bridge (GPSJam-based) — gpsjam.org has no public API for its own processed data, so this bridge never actually worked; removed from `start.sh` and `cot_layer.py` rather than left silently broken |
| 2026-07-05 | Fixed cross-source/cross-pod duplicate tracks in SitaWare: `nato_nvg_layer.py`'s `_uid()` baked the source name into the track ID (unlike `cot_layer.py`'s already-correct version), so the same aircraft from two sources got two different SitaWare tracks |
| 2026-07-05 | `dronuradaras_bridge.py` was changed to publish every positioned registered sensor; superseded by the 2026-07-15 online-only operator policy below |
| 2026-07-05 | Added `.github/workflows/ci.yml`: compile-checks bridges/layers, type-checks + builds the zenoh-admin frontend, builds both Docker images on every push/PR |
| 2026-07-05 | Added `shellcheck` and `compose-validate` CI jobs; fixed the one real finding (`compose/rebuild.sh` missing `cd ... \|\| exit`) and silenced a false-positive (`SC2163` on the intentional "export by dynamic name" idiom in `start.sh`/`stop.sh`/`run.sh`) |
| 2026-07-10 | Fixed `nato_nvg_layer.py` reusing the inbound `sitaware_bridge.py`'s env var names (`SITAWARE_URL`/`USER`/`PASS`) — renamed to `SITAWARE_NVG_*` since HQ (inbound) and Edge (outbound) are usually separate hosts/credentials |
| 2026-07-10 | Wired `nffi` into `start.sh` — it existed in the repo but was never registered as a launchable service |
| 2026-07-10 | Zenoh admin GUI: added a "Connected routers" panel — parses `router/transport/unicast/*` entries already present in the admin-space query used for the subscriber/queryable lists, no new ACL or query needed |
| 2026-07-10 | Zenoh admin GUI: ported the TAK-hud visual language (`hud-card`, `hud-frame`/reticle corners, `hud-glass` sidebar, `hud-grid-bg` backdrop, accent-glow buttons, staggered fade-in) into `index.css`/`Layout.tsx`/dashboard |
| 2026-07-11 | Zenoh admin GUI: full TAK port (not just style) — runtime branding via DB-backed store, theme toggle, notifications bell, username-change, all routes retrofitted with light/dark variants |
| 2026-07-11 | Zenoh admin panel HTTPS: uvicorn now binds `127.0.0.1:8895` only; new `zenoh-admin-proxy` (Caddy) terminates real TLS on `:8890` via Caddy's internal CA, `on_demand` issuance (operators reach it by raw IP, no SNI) |
| 2026-07-11 | `BUNDLE_DIR`/`POD_STATE_DIR` defaults moved from `$HOME/goat-bundle`/`$HOME/goat-moon` to `compose/certs/`/`compose/state/` (in-repo, gitignored) — scattered state across `$HOME` made cleanup unreliable |
| 2026-07-11 | Added `dev.sh`: disposable local Postgres + directly-run uvicorn for zenoh-admin UI preview only, bypassing zenoh-router/certs/fabric entirely |
| 2026-07-11 | Removed the external "goat" vendor entirely: certs are now self-issued via `scripts/gen-certs.sh` (EFDI root CA, no portal/CBOR bundle), containers renamed `goat-moon-*` → `efdi-pod-*`, `GOAT_CERT_DIR` env var renamed `EFDI_CERT_DIR`, `host/first-boot.sh` rewritten to read `compose/.env` directly and drop the `goat-clientd` wrapper (NetBird is called natively — it was always EFDI's own asset, not vendor lock-in), `profiles/` directory removed (orphaned by the rewrite) |
| 2026-07-15 | `dronuradaras_bridge.py` now publishes only devices explicitly reported as `is_online=true`; offline devices emit deletion events so CoT, SitaWare Edge, and the HQ NVG snapshot evict cached markers |
| 2026-07-17 | Replaced FlightRadar24 and OpenSky with the free/open-data ADSB.lol bridge |
| 2026-07-17 | Added deterministic ASTERIX category listener conventions: CAT-010/020/021/034/048/062 use UDP 50010/50020/50021/50034/50048/50062 by default; these are EFDI conventions, not vendor defaults |
| 2026-07-17 | Added Zenoh-native CAP, GeoJSON/OGC, AIS NMEA, spectrum, sensor-health, mission-route, and raw-ingress translation paths |
| 2026-07-17 | Security refresh: Vite upgraded, Compose images pinned/refreshed, Python image OS packages upgraded, and authenticated SitaWare/UTM endpoints restricted to HTTPS |
| 2026-07-18 | Added TAK-style Runtime Control for native bridge/protocol/layer lifecycle, bounded logs, endpoint/topic/port editing, write-only credentials, a localhost admin-control agent, and a live Vite dev stack with aligned API/proxy ports |

---

*Internal use only — do not distribute outside the project.*
