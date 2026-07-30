# EFDI — Deployment Guide

> **Platform:** Linux · **Zenoh:** 1.9.0 · **Python:** 3.10+

This guide covers deploying the sensor bridge stack on a Linux host. The stack
can ingest mixed ASTERIX categories (the current normalized decoders are
CAT-010, CAT-020, CAT-021, CAT-034, CAT-048, and CAT-062), plus
dronuradaras.lt acoustic detections, SAPIENT, STANAG 4586/4609, and
SitaWare. All markers are routed through a local Zenoh fabric to TAK and
SitaWare clients.

---

> **Starting from a bare host with nothing installed?** Read
> [`HOST_SETUP.md`](HOST_SETUP.md) first — it walks through installing
> Docker, Python, git, and NetBird from scratch on Ubuntu or RHEL-family
> Linux. Skip it if those are already installed and working.

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

ATAK devices must be on the same L2 segment as the server for multicast delivery. Cross-VLAN or cross-subnet deployments require a TAK Server (`cot-bridge` service).

### Certificates

For a standalone/development pod, Zenoh mTLS certs can be self-issued without
an external vendor bundle. `scripts/gen-certs.sh <namespace>` generates (once)
an EFDI development root CA under `compose/certs/efdi/`, then signs a leaf
cert+key for the given namespace. Do not distribute that development root key
to managed routers.

The generated material (`efdi-ca-root.pem`, `<NAMESPACE>-cert.pem`, `<NAMESPACE>-key.pem`) lives at `compose/certs/efdi/` — gitignored, never committed. The ignored bundle directory also keeps `tak/`, `sitaware/`, `efdi-backbone/` (goat backbone, Desert Bread CA), and `efdi-ltu/` (LTU sandbox) identities separate — see `compose/certs/README.md`. Default path is set by `start.sh`; override with `BUNDLE_DIR` in `compose/.env` if you'd rather keep it outside the repo entirely. Managed deployments use the delegated-CA workflow in section 10 and keep CA private keys under a separate mode-700 runtime directory.

---

## 2. Installation

### 2.1 Clone the repository

```bash
git clone <repo-url> EFDI
cd EFDI
```

### 2.2 Generate certificates

```bash
scripts/gen-certs.sh <namespace>   # e.g. scripts/gen-certs.sh 0123456789abcdef0123456789abcdef
```

This produces:

```text
compose/certs/
├── efdi/                     # this pod's own EFDI CA + identity (do not rename)
├── efdi-ltu/                 # LTU sandbox: asusrog client cert + EFDI LTU Root CA
├── efdi-backbone/                 # goat backbone identity (Desert Bread CA)
├── sitaware/                 # SitaWare feed CA and server identity
└── tak/                      # TAK Server identity
```

See `compose/certs/README.md` for the full legend (which cert authenticates to
which fabric). The whole directory is gitignored.

The LTU participant key is encrypted and its leaf file does not contain the
intermediate CA. Run `scripts/connect-ltu.sh` from a terminal when switching to
that fabric. It asks for the key passphrase with hidden input, verifies the
downloaded public intermediate against the pinned LTU root, and writes only a
full client chain plus an unencrypted runtime key under ignored
`compose/state/zenoh/tls/ltu/`. Zenoh has no private-key passphrase setting; do
not point the router directly at the encrypted source key.

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

# ── Generic UDP ingress (safe ASTERIX CAT-34/48 auto-dispatch) ──────────────
UDP_INGRESS_PORT=50000
UDP_INGRESS_BIND=0.0.0.0
UDP_INGRESS_ALLOW_SOURCE=
# Backward-compatible aliases:
ASTERIX_PORT=
ASTERIX_BIND=0.0.0.0
ASTERIX_CATEGORIES=34,48
ASTERIX_MULTICAST_GROUP=       # optional
ASTERIX_MULTICAST_INTERFACE=0.0.0.0
ASTERIX_ALLOW_SOURCE=          # optional sender IPv4 address or CIDR

# Optional sensor-side Zenoh router carrying complete raw ASTERIX frames:
ASTERIX_ZENOH_UPSTREAM_ENDPOINT=  # e.g. tcp/zenoh2.example:7448 for isolated testing
ASTERIX_ZENOH_UPSTREAM_ROOT=      # defaults to this pod's complete topic root

# Separate publisher streams can continue using these direct listeners.
CAT10_PORT=50010               # EFDI private-range convention; configure producer output
CAT20_PORT=50020               # EFDI private-range convention; configure producer output
CAT21_PORT=50021               # EFDI private-range convention; configure producer output
CAT34_PORT=50034               # EFDI private-range convention; configure radar output
CAT48_PORT=50048               # EFDI private-range convention; configure radar output
CAT34_RADAR_LAT=               # Single-radar fallback; live I034/120 is preferred
CAT34_RADAR_LON=               # Single-radar fallback; live I034/120 is preferred
CAT34_RADAR_NAME=              # Blank = distinct RADAR SACx/SICy labels; set for one radar
CAT34_RADAR_RANGE_M=           # Operator-confirmed maximum; live I034/100 wins
CAT62_PORT=50062               # EFDI private-range convention; configure producer output
CAT48_RADAR_SAC=<SAC>          # ASTERIX Source Area Code
CAT48_RADAR_SIC=<SIC>          # ASTERIX Source Identification Code
```

> **Radar position (lat/lon):** The bridge reads each radar position from
> CAT-34 I034/120 and keeps it separately by SAC/SIC, so multiplexed VERA-NG
> or other multi-radar feeds do not overwrite one another. Set
> `CAT34_RADAR_LAT` / `CAT34_RADAR_LON` only as a single-radar fallback when
> the feed omits I034/120. Without either source, EFDI logs the missing
> SAC/SIC position and deliberately withholds the site marker instead of
> placing it at 0°N 0°E.

> **ASTERIX ports:** ASTERIX specifies the message format, not a registered
> network port. In the radar/gateway management interface, set the EFDI host as
> the destination and use the EFDI category convention: CAT-010→UDP 50010,
> CAT-020→50020, CAT-021→50021, CAT-034→50034, CAT-048→50048, CAT-062→50062.
> These are EFDI conventions, not known vendor factory defaults. Confirm
> transport, category edition, combined/separate streams, and vendor framing in
> the ICD.

Port 50000 accepts generic UDP and preserves every datagram under
`…/raw/udp/ingress`. Complete ASTERIX frames are additionally published to
`…/raw/asterix/cat34` and `…/raw/asterix/cat48`; the per-category translators
decode only their category. Dedicated category ports remain active. Do not send
the same frames to both paths unless duplicates are acceptable. Inspect an
unknown feed first:

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
  [30] [✓] cot-udp        CoT → ATAK UDP multicast 239.2.3.1:6969
  [31] [ ] cot-udp-tak    CoT → WinTAK/ATAK UDP unicast
  [32] [✓] cot-bridge     CoT → TAK Server TCP
  [33] [ ] tak-bridge     TAK Server CoT ingress               will prompt for address
  [34] [ ] sitaware-hq-nvg EFDI tracks → SitaWare HQ pull feed   SITAWARE_HQ_NVG_ENABLE=0
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
| EFDI tracks polled by SitaWare HQ | `zenoh mission-route` |
| All ready inputs + TAK Server | `a`, then deselect `cot-udp` |
| Radar only, no TAK output (debug) | `zenoh asterix` |

Processes are tracked via PID files in `$POD_STATE_DIR/.pids/` and log to `$POD_STATE_DIR/logs/<service>.log`.

After a successful launch, `start.sh` remembers the selected services and the last TAK/SitaWare endpoint addresses in `$POD_STATE_DIR/launcher-state.env` (mode 600). It also merges any currently running PID-managed services into that selection. On the next interactive launch it displays the complete restored selection and auto-starts it after five seconds; press `c` during the countdown to change it. It never stores passwords, API keys, or certificate material there. Explicit values in `compose/.env` take precedence over remembered addresses.

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
SITAWARE_URL_FALLBACK=https://swhq.efdi.ltu:10006 # optional stable mesh-DNS path
SITAWARE_USER=<username>
SITAWARE_PASS=<password>
SITAWARE_API_PATH=/<documented-resource-path>
SITAWARE_POLL_S=10   # optional — poll interval in seconds (default 10)
```

The bridge reads MIL-STD-2525B SIDC codes from SitaWare and routes each unit to the correct Zenoh topic by affiliation and battle dimension:

| SIDC affiliation | SIDC dimension | Zenoh topic path | ATAK CoT type |
| --- | --- | --- | --- |
| Friendly / Assumed Friendly | Ground (G) | `…/land/sitaware/c2/friendly/unit/…` | `a-f-G-U-C` |
| Hostile | Ground (G) | `…/land/sitaware/c2/hostile/unit/…` | `a-h-G-U-C` |
| Neutral | Ground (G) | `…/land/sitaware/c2/neutral/unit/…` | `a-n-G-U-C` |
| Friendly | Air (A) | `…/air/sitaware/c2/friendly/aircraft/…` | `a-f-A-M-F` |
| Hostile | Air (A) | `…/air/sitaware/c2/hostile/aircraft/…` | `a-h-A-M-F` |
| Friendly | Sea (S) | `…/sea/sitaware/c2/friendly/vessel/…` | `a-f-S-X-L` |
| Hostile | Sea (S) | `…/sea/sitaware/c2/hostile/vessel/…` | `a-h-S-X-L` |
| Friendly / Hostile / Neutral / Unknown | Space (P) | `…/space/sitaware/c2/<affiliation>/satellite/…` | matching `a-<affiliation>-P` |
| Any | Special operations forces (F) | `…/land/sitaware/c2/<affiliation>/unit/…` | matching ground-unit type |

### NATO NFFI friendly-force protocol translator

`nffi` subscribes to complete NFFI XML documents that a partner receiver or detection system has already published under `…/raw/nffi/{source-id}` in Zenoh. It translates every unit to `…/land/nato/c2/friendly/unit/{type}/{id}/sapient`. It owns no TCP client, listener, endpoint, or framing convention. A product-specific connection must live in a separate `_bridge.py` after its endpoint and ICD are known.

NFFI friendly-force interoperability is ADatP-36 / STANAG 5527. STANAG 4677 is the separate dismounted-soldier interoperability family; a 4677 JDSSDM-over-NFFI profile would need a separate, profile-specific implementation.

**`.env` fields:**

```bash
NFFI_INPUT_TOPIC=               # optional; default: …/raw/nffi/*
```

### SitaWare Headquarters (outbound NVG pull feed)

`sitaware-hq-nvg` is the native Python output for an HQ-only deployment. It subscribes to EFDI tracks, keeps a bounded live snapshot, and exposes NVG 2.0.2 over a read-only HTTP(S) endpoint. SitaWare Headquarters polls it through **SitaWare Communication → NVG → NVG Import Subscriptions**. The reverse path — HQ's NVG Export Endpoint back into Zenoh — is the `nvg_bridge` service (`bridges/nvg_bridge.py`).

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

The endpoint accepts GET/HEAD only. It requires Basic authentication by default, bounds the cache, removes tracks not refreshed within `SITAWARE_HQ_NVG_STALE_S`, and gives each published NVG object a matching `TimeSpan` expiry. When present in the source, standard NVG modifiers and bounded `ExtendedData` carry callsign, registration/ICAO, aircraft or vessel type, squawk, route, source, vessel IDs, sensor identity, and other safe scalar fields. The Attributes view reuses the CoT/TAK domain formatter, presenting clean sections rather than raw Python field names. Aircraft expose separate barometric and geometric altitude, primary altitude in metres/feet/flight level, climb/descent rate, selected/target altitude, speed/heading, emergency/autopilot state, and ADS-B quality. dronuradaras.lt detections use the HQ-supported generic neutral equipment-sensor symbol; weather observations use the distinct neutral emplaced-sensor symbol because HQ 6.22 renders standards-native METOC symbols as Unknown. Neither is classified as a military-intelligence unit. It refuses cleartext HTTP on a non-loopback address unless `SITAWARE_HQ_NVG_ALLOW_INSECURE_HTTP=1` is explicitly set for an isolated lab. Do not use a Keycloak account or password for this feed.

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

Do not clear a shared operational layer to work around this limitation.

### Icon reference

| ATAK appearance | CoT type | Source |
| --- | --- | --- |
| Neutral radar sensor (client-native MIL symbol) | `a-n-G-E-S-R` | CAT-34 site marker, including VERA-NG |
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

> **Topic tiers.** The `…/tracks/v1` paths below are the JSON tier. Each one has
> two protobuf siblings carrying the same event: `…/tracks/v2` (typed message
> from the protocol's `.proto`) and `…/tracks/native/v1` (a `RawEnvelope`
> wrapping the original wire bytes, byte-exact). Prefer `/v2`; use `/native/v1`
> when you need a field EFDI does not decode. `/v1` is legacy and will be
> retired. Full explanation: [INTEGRATIONS.md → Egress topic tiers](INTEGRATIONS.md#egress-topic-tiers-v1-v2-nativev1).

| Service | Script | Zenoh topic (abbreviated) | Trigger |
| --- | --- | --- | --- |
| `asterix` | `protocols/vendors/asterix/cat.py` | `…/raw/asterix/catNN` and category-specific normalized ASTERIX topics | ASTERIX vendor's CAT protocol bundle: mixed UDP ingress plus per-category translators |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | 60 s online-only device poll with offline eviction / 10 s detection poll |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Configurable REST poll |
| `nffi` | `protocols/random/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Complete XML documents under `…/raw/nffi/*` in Zenoh |
| `stanag` | `protocols/vendors/stanag/4586.py` and `4609.py` | `…/raw/stanag_4609/klv`, `…/air/stanag_4609/camera/unknown/uav`, and STANAG 4586 track topics | Launcher starts each configured numbered protocol directly |
| `sapient-raw`, `stanag4586-raw` | `bridges/*_raw_bridge.py` | `…/raw/<protocol>/<source>` | Optional socket ingress; matching protocol runs with `*_ZENOH_RAW=1` |
| `cap` | `protocols/random/cap.py` | `…/land/cap/c2/neutral/sensor/{type}/{id}/sapient` | Complete CAP 1.2 XML on `…/raw/cap/**` |
| `geojson` | `protocols/random/geojson_features.py` | `…/land/ogc/c2/neutral/zone/{type}/{id}/sapient` | GeoJSON/OGC Features on `…/raw/geojson/**` |
| `mqtt` | `protocols/random/mqtt_json.py` | `…/land/mqtt/iot/unknown/sensor/{type}/{id}/sapient` | Vendor JSON on `…/raw/mqtt/**` (bridge forwards any payload verbatim) |
| `sensorthings` | `protocols/random/sensorthings.py` | `…/land/sensorthings/iot/neutral/sensor/{type}/{id}/sapient` | Observations on `…/raw/sensorthings/**` |
| `sparkplug` | `protocols/vendors/sparkplug/sparkplug.py` | `…/land/sparkplug/iot/unknown/sensor/{type}/{id}/sapient` | Sparkplug B protobuf on `…/raw/mqtt/spBv1.0/**` |
| `spectrum` / `sensor-health` / `mission-route` | Matching `protocols/random/*.py` | `…/land/spectrum/**`, `…/land/health/**`, `…/air/mission/**` | JSON on their `…/raw/**` topics |
| `cot-udp` | `layers/cot_layer.py` | Subscriber — all topics | Event-driven |
| `cot_layer` | `layers/cot_layer.py` | Subscriber — all topics | Event-driven |
| `tak-bridge` | `bridges/tak_bridge.py` | Subscriber — all topics | TAK-visible CoT ingress |
| `sitaware-hq-nvg` | `layers/nvg_layer.py` | Subscriber — all track topics | Pull-based NVG snapshot |
| `track-fusion` | `bridges/track_fusion_bridge.py` | CAT-48 + CAT-21 subscriber | Event-driven |

### TAK users and external CoT sources

### Zenoh-native raw ingress

For a receiver host that should own the network socket, select the matching
`*-raw` bridge and set its raw port. Select the protocol translator separately
with its `*_ZENOH_RAW=1` setting. For example:

The raw bridge publishes octets only; it does not classify or alter them. The
SAPIENT/FLEX 335 and STANAG 4586 translators consume those Zenoh topics and
publish normalized JSON. SAPIENT ingress
uses the public BSI Flex 335 v2 protobuf contract. The retained STANAG 4586
binary layout is a historical deployment approximation, not a generic standard
profile: it stays disabled unless `STANAG4586_PROFILE=legacy_ed3_approx` is
explicitly set after validating the layout against the deployed VSM ICD.

CAP, GeoJSON, spectrum, health, and route translators are idle-safe
Zenoh subscribers. A partner publishes complete JSON/XML/NMEA payloads below
the corresponding `raw/**` topic; no internet URL or receiver is embedded in
the translator.

CoT and SitaWare HQ NVG outputs apply the same scenario affiliation policy:
aircraft in the configured RU/BY ICAO address ranges and vessels with RU/BY
MMSI MIDs are hostile; other partner-provided air/sea contacts remain neutral. An
origin-country label alone does not override an invalid or missing transponder
identifier.

`tak-bridge` is the inverse CoT path: it connects to a TAK-visible CoT feed
over the documented TCP/TLS session, extracts complete `<event>...</event>`
frames, and republishes normalized JSON into Zenoh. It does not replace the
CoT output layer and it does not use Zenoh as the TAK wire transport.

## C2 ↔ Zenoh bidirectional runbook

Operator-side setup for both directions of TAK and SitaWare integration —
env vars, TAK Server / SitaWare HQ admin steps, and the persona test
exercise. Moved to its own document: **[C2_RUNBOOK.md](C2_RUNBOOK.md)**.

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

Symptom-first fixes for common deployment problems. Moved to its own
document: **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)**.

## 9. Adding a New Sensor or Protocol

Step-by-step walkthrough, now its own document: **[ADDING_A_SENSOR.md](ADDING_A_SENSOR.md)**.

---

## 10. Zenoh Admin GUI

A web GUI for operating the pod without SSH — status, starting/stopping
bridges and layers, configuration, the certificate authority, and branding.
Moved to its own document: **[ZENOH_ADMIN.md](ZENOH_ADMIN.md)**.

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
| 2026-07-11 | Added `dev.sh`: disposable local MariaDB + directly-run uvicorn for zenoh-admin UI preview only, bypassing zenoh-router/certs/fabric entirely |
| 2026-07-11 | Removed the external "goat" vendor entirely: certs are now self-issued via `scripts/gen-certs.sh` (EFDI root CA, no portal/CBOR bundle), containers renamed `goat-moon-*` → `efdi-pod-*`, `GOAT_CERT_DIR` env var renamed `EFDI_CERT_DIR`, `host/first-boot.sh` rewritten to read `compose/.env` directly and drop the `goat-clientd` wrapper (NetBird is called natively — it was always EFDI's own asset, not vendor lock-in), `profiles/` directory removed (orphaned by the rewrite) |
| 2026-07-15 | `dronuradaras_bridge.py` now publishes only devices explicitly reported as `is_online=true`; offline devices emit deletion events so CoT, SitaWare Edge, and the HQ NVG snapshot evict cached markers |
| 2026-07-17 | Added deterministic ASTERIX category listener conventions: CAT-010/020/021/034/048/062 use UDP 50010/50020/50021/50034/50048/50062 by default; these are EFDI conventions, not vendor defaults |
| 2026-07-17 | Added Zenoh-native CAP, GeoJSON/OGC, spectrum, sensor-health, mission-route, and raw-ingress translation paths |
| 2026-07-17 | Security refresh: Vite upgraded, Compose images pinned/refreshed, Python image OS packages upgraded, and authenticated SitaWare/UTM endpoints restricted to HTTPS |
| 2026-07-18 | Added TAK-style Runtime Control for native bridge/protocol/layer lifecycle, bounded logs, endpoint/topic/port editing, write-only credentials, a localhost admin-control agent, and a live Vite dev stack with aligned API/proxy ports |

---

*Internal use only — do not distribute outside the project.*
