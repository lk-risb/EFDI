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

# ── Admin panel database data directory ─────────────────────────────────────
# Where the zenoh-admin panel's PostgreSQL container stores its data.
# Defaults to <POD_STATE_DIR>/zenoh-admin/postgres.
#EFDI_DB_DATA_DIR=/var/lib/efdi-pod/zenoh-admin/postgres

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

# Per-category radar/site metadata — still read from the single UDP_INGRESS_PORT
# stream above; these are decode-time fields, not separate listeners/ports.
CAT34_RADAR_LAT=               # Single-radar fallback; live I034/120 is preferred
CAT34_RADAR_LON=               # Single-radar fallback; live I034/120 is preferred
CAT34_RADAR_NAME=              # Blank = distinct RADAR SACx/SICy labels; set for one radar
CAT34_RADAR_RANGE_M=           # Operator-confirmed maximum; live I034/100 wins
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
> network port. There are no separate per-category listeners — every category
> arrives as one generic UDP dump on `UDP_INGRESS_PORT` (50000). In the
> radar/gateway management interface, set the EFDI host and this single port
> as the destination for all ASTERIX traffic; confirm transport, category
> edition, and vendor framing in the ICD.

Port 50000 accepts generic UDP and preserves every datagram under
`…/raw/udp/ingress`. Complete ASTERIX frames are additionally published to
`…/raw/asterix/cat34` and `…/raw/asterix/cat48`; the per-category translators
decode only their category from that single stream. Inspect an unknown feed
first:

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

### Shared storage (JuiceFS) and the database

If `POD_STATE_DIR` is moved onto a shared filesystem such as JuiceFS (see the
host-level setup in the separate `EFDI-Docs` repo's `02-postgres.md` and
`03-juicefs.md`), keep the following in mind:

- **`EFDI_DB_DATA_DIR` must stay on local disk, never on `POD_STATE_DIR`.**
  JuiceFS mounts run with writeback caching, which breaks the `fsync`
  durability every database relies on — and if `POD_STATE_DIR` and the
  JuiceFS metadata store are the same PostgreSQL instance, putting the pod's
  own database there creates a circular boot dependency. `install.sh` and
  `update.sh` both refuse to start if `EFDI_DB_DATA_DIR` resolves onto a FUSE
  filesystem.
- **Docker must start after the mount.** If `dockerd` starts before the
  JuiceFS mount is up, every `${POD_STATE_DIR}/…` bind mount resolves against
  an empty directory and Docker silently creates a placeholder there instead
  of failing loudly. Order the two with a systemd drop-in:
  ```ini
  # /etc/systemd/system/docker.service.d/10-juicefs.conf
  [Unit]
  After=juicefs.service
  Requires=juicefs.service
  ```
- **uid 10001 ownership still works.** The `chgrp 10001` / `chmod 664|775`
  handling `install.sh`/`reinstall.sh`/`update.sh` apply to
  `namespace-prefix`, `data-topic-prefix`, and the TAK/bundle directories
  works the same on JuiceFS as on local disk, provided the mount uses
  `allow_other` (see `03-juicefs.md`'s systemd unit) — verify this explicitly
  on first deployment rather than assuming it.
- **Other embedded state under `POD_STATE_DIR`** — `zenoh/rocksdb`
  (currently unused; no per-prefix storages are configured by default) and,
  if the `managed-ca` profile is enabled, step-ca's own local database —
  carry the same local-disk-only caveat as `EFDI_DB_DATA_DIR` the moment
  they are actually used. Neither is addressed by this change.

---
