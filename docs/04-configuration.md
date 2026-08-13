# 04 — Configuration

## Configuration

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
# ── TAK Server ──────────────────────────────────────────────────────────────
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
