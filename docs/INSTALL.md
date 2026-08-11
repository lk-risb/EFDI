# EFDI — Deployment Guide

> **Platform:** Linux · **Zenoh:** 1.9.0 · **Python:** 3.10+

This guide covers deploying the sensor bridge stack on a Linux host. The stack
can ingest mixed ASTERIX categories (the current normalized decoders are
CAT-001, CAT-002, CAT-004, CAT-007, CAT-008, CAT-009, CAT-010, CAT-011, CAT-015,
CAT-016, CAT-017, CAT-018, CAT-019, CAT-020, CAT-021, CAT-023, CAT-025, CAT-032, CAT-034, CAT-048, CAT-062, CAT-063, CAT-065, CAT-150, CAT-205, CAT-240, and CAT-247 — the complete public ASTERIX catalogue), plus dronuradaras.lt acoustic detections, SAPIENT, STANAG 4586/4607/4609/5516, and
SitaWare. All markers are routed through a local Zenoh fabric to TAK and
SitaWare clients.

---

## 1. Prerequisites

### Bare host bootstrap

On Debian 13 or RHEL/Rocky/AlmaLinux 9/10, the only thing you install by
hand is `curl` (usually already present) — everything else is one command:
```bash
curl -fsSL https://raw.githubusercontent.com/risblicencijos/EFDI/main/install.sh | bash
```
`./install.sh` updates the OS (`apt`/`dnf` upgrade) and auto-installs git,
Python 3.10+, Docker Engine + the Compose plugin (from Docker's official
repository, not a distro-bundled package), openssl, and gettext itself on
both Debian (apt) and RHEL/Rocky/Alma (dnf) hosts if any are missing — a
completely bare server with just `sudo` and outbound internet access needs
nothing manual before this. If the OS update needs a reboot (kernel or core
library update), the installer stops and says so — just reboot and re-run
the same command. It also offers to install and connect NetBird or
Tailscale if neither is already up, prompting only for the setup/auth
key — the one thing an installer can't reasonably fabricate on its own.

<details>
<summary>Manual/offline step-by-step (only if you're not running
<code>install.sh</code> — air-gapped install, a different distro, or
debugging what it does)</summary>

#### Choose and size the host

| | Minimum | Recommended |
| --- | --- | --- |
| OS | Debian 13 (trixie), or RHEL 9/10, Rocky Linux 9/10, AlmaLinux 9/10 | Debian 13 (trixie) |
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB free | 40 GB+ free (more if you enable long-term track storage) |
| Network | One interface with outbound internet access | Static or DHCP-reserved address |

Any modern x86_64 or arm64 Linux distribution with a recent kernel and systemd
works; these two families are covered step-by-step below because they are the
most common in government/defense environments. Ubuntu also works (it's the
same apt tooling), but Debian is this project's actual target and what
`install.sh` defaults to — if you use a different distribution entirely,
translate the package-manager commands and the rest of this guide applies
unchanged.

Run every command below as a regular user with `sudo` access — not as `root`
directly, so the final "run Docker as a non-root user" step is meaningful.

#### Update the OS

**Debian:**
```bash
sudo apt update && sudo apt upgrade -y
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf upgrade -y
```

Reboot if the kernel was updated (`sudo reboot`).

#### Install git and basic tools

**Debian:**
```bash
sudo apt install -y git curl ca-certificates
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf install -y git curl ca-certificates
```

Verify: `git --version`

#### Install Python 3.10+

**Debian 13 (trixie)** ships Python **3.13** by default — already well above
EFDI's minimum, no extra step needed beyond making sure venv/pip are present:
```bash
sudo apt install -y python3 python3-venv python3-pip
```

**RHEL/Rocky/AlmaLinux 10** ship Python **3.12** by default — already above
EFDI's minimum, same one-liner as Debian:
```bash
sudo dnf install -y python3 python3-pip
```

**RHEL/Rocky/AlmaLinux 9** ship Python **3.9** by default, which is below
EFDI's minimum. Install 3.11 from the AppStream repository alongside it (this
does **not** replace the system `python3`, so nothing else on the box breaks):
```bash
sudo dnf install -y python3.11 python3.11-pip
```
Use `python3.11` explicitly wherever this repo's scripts say `python3` on a
RHEL 9 host, or set up an alias/venv that points at it.

Verify: `python3 --version` (or `python3.11 --version` on RHEL 9) must
report **3.10 or newer**.

#### Install Docker Engine + the Compose plugin

Use the distribution's official Docker repository, not a distro-bundled
`docker.io`/`podman-docker` package — those are frequently out of date and can
be missing the Compose v2 plugin this repo depends on (`docker compose`, not
the old standalone `docker-compose`).

**Debian:**
```bash
# Add Docker's official GPG key and repository
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update

# Install Docker Engine + Compose plugin
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```
(On Ubuntu, swap `linux/debian` for `linux/ubuntu` in both lines above —
Docker publishes separate repos per distro.)

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

##### Run Docker as a non-root user

Every script in this repo assumes `docker`/`docker compose` work without
`sudo`. Set that up now:
```bash
sudo groupadd docker 2>/dev/null || true   # already exists on most systems
sudo usermod -aG docker "$USER"
newgrp docker                              # activates the new group in this shell
```
Log out and back in (or reboot) so the group membership applies to every new
shell, not just the current one. Verify:
```bash
docker run hello-world
docker compose version
```
Both must succeed **without `sudo`** before continuing.

#### Install NetBird or Tailscale

EFDI pods reach the fabric and each other over a mesh VPN — NetBird or
Tailscale, either works. Install whichever your organization uses (both ship
their own repository via these scripts, so there's no separate apt/dnf setup):
```bash
curl -fsSL https://pkgs.netbird.io/install.sh | sh      # NetBird
curl -fsSL https://tailscale.com/install.sh | sh        # Tailscale
```
Do **not** join a network yet — the setup/auth key comes from whoever
administers your organization's account, and `./install.sh` itself prompts
for it and joins during **§2 Installation** below (this is exactly what its
automated Networking step does). Verify only that the binary installed:
```bash
netbird version
tailscale version
```

#### Open firewall ports

`./install.sh` does **not** do this step for you — which ports to open
depends on which sensor bridges you enable, and that's a post-install,
WebUI-driven choice (see **§3 Configuration**), not something the installer
can decide up front. Open only what this host needs inbound; everything
else in the port table below is outbound and needs no firewall rule on
this host (a receiving radar/sensor's firewall is a different concern).
The authoritative, current port list is the **Network** table just below —
open the ones your deployment actually uses (most pods do not run every
sensor bridge).

**Debian (ufw):**
```bash
sudo apt install -y ufw   # not installed by default on Debian, unlike Ubuntu
sudo ufw allow 8890/tcp comment 'EFDI admin GUI'
sudo ufw allow 50048/udp comment 'EFDI CAT-048 example — adjust to your sensors'
# repeat for whichever UDP/TCP ports your integrations use, per the table below
```

**RHEL/Rocky/AlmaLinux (firewalld):**
```bash
sudo firewall-cmd --permanent --add-port=8890/tcp
sudo firewall-cmd --permanent --add-port=50048/udp
sudo firewall-cmd --reload
```

If the host sits behind a separate network firewall or security group (cloud,
on-prem appliance), the same ports need opening there too — this step only
covers the host's own local firewall.

#### You're ready

At this point you should be able to run, all without `sudo`:
```bash
git --version
python3 --version      # 3.10+
docker run hello-world
docker compose version
netbird version         # or: tailscale version
```

If every command above succeeds, continue to **§2** below (the repository
clone and pod bootstrap). If anything failed, re-run the matching step above
before moving on — nothing later in the setup can fix a missing dependency
here.

</details>

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
| TCP `<TAK_PORT>` (mTLS, default 8089) | outbound | CoT delivery to TAK Server |
| TCP 7448 | localhost | Local Zenoh router |
| TCP 7447 TLS | outbound | Remote Zenoh router (requires NetBird) |
| HTTPS 8890 | inbound | Zenoh admin GUI (Caddy-terminated, internal CA — see §10) |
| HTTPS | outbound | dronuradaras.lt APIs |

ATAK/WinTAK clients receive tracks only through a TAK Server (`tak-layer` service); there is no direct multicast/unicast CoT delivery path.

### Certificates

For a standalone/development pod, Zenoh mTLS certs can be self-issued without
an external vendor bundle. `scripts/gen-certs.sh <namespace>` generates (once)
an EFDI development root CA under `compose/certs/efdi/`, then signs a leaf
cert+key for the given namespace. Do not distribute that development root key
to managed routers.

The generated material (`efdi-ca-root.pem`, `<NAMESPACE>-cert.pem`, `<NAMESPACE>-key.pem`) lives at `compose/certs/efdi/` — gitignored, never committed. The ignored bundle directory also keeps `tak/`, `sitaware/`, `efdi-backbone/` (goat backbone, Desert Bread CA), and `efdi-ltu/` (LTU sandbox) identities separate — see the certificate profile legend in §2.2 below. Default path is set by `start.sh`; override with `BUNDLE_DIR` in `compose/.env` if you'd rather keep it outside the repo entirely. Managed deployments use the delegated-CA workflow in section 10 and keep CA private keys under a separate mode-700 runtime directory.

---

## 2. Installation

### 2.1 Clone the repository

On a fresh host with nothing installed yet, one command clones the repo and
runs the installer, which auto-installs every prerequisite from §1 itself:

```bash
curl -fsSL https://raw.githubusercontent.com/risblicencijos/EFDI/main/install.sh | bash
```

Equivalent to cloning first, then running the same script locally:

```bash
git clone <repo-url> EFDI
cd EFDI
./install.sh
```

### 2.2 Generate certificates

```bash
scripts/gen-certs.sh <namespace>   # e.g. scripts/gen-certs.sh 0123456789abcdef0123456789abcdef
```

This produces:

```text
compose/certs/
├── efdi/                     # internal router identity: namespace leaf + EFDI CA
├── efdi-backbone/            # Backbone identity: cert.pem, key.pem, ca-roots.pem
├── efdi-ltu/                 # LTU sandbox: client.pem, client.key, ca.crt
├── sitaware/                 # SitaWare feed CA and server identity
└── tak/                      # TAK Server identity
```

The empty profile layout and each profile's README placeholder are tracked;
every certificate, private key, chain, and generated credential below
`compose/certs/` is gitignored — never force-add one.

| folder | purpose | preparation |
|---|---|---|
| `efdi/` | this router's local EFDI identity | staged by `../examples/first-boot.sh`, or `scripts/gen-certs.sh <namespace>` for a dev identity |
| `efdi-ltu/` | LTU fabric client/server identity | run `scripts/connect-ltu.sh` |
| `efdi-backbone/` | Backbone client identity | staged by `../examples/first-boot.sh` |

These three profiles use different certificate authorities — a certificate
from one profile cannot authenticate to another. `tak/` and `sitaware/` are
sibling directories for those integrations' credentials; both stay entirely
local, with no tracked placeholder. `BUNDLE_DIR` may point at an equivalent
directory outside the repository — the same fixed filenames apply there.

**`efdi/` — local EFDI identity.** Required filenames: `efdi-ca-root.pem`
(public trust root), `<PARTNER_NAMESPACE>-cert.pem` (this router's
certificate), `<PARTNER_NAMESPACE>-key.pem` (matching private key). Set
`PARTNER_NAMESPACE` in `compose/.env`, then run `../examples/first-boot.sh`, or
generate a local development identity directly with
`scripts/gen-certs.sh <PARTNER_NAMESPACE>`. Private CA keys and serial files
are optional provisioning material and must also remain untracked.

**`efdi-ltu/` — LTU fabric identity.** Required filenames: `client.pem`
(LTU-issued client certificate; add `serverAuth` too if other routers dial
this one), `client.key` (matching private key, may be passphrase protected),
`ca.crt` (LTU fabric trust root). The LTU participant key is encrypted and its
leaf file does not contain the intermediate CA. Run `scripts/connect-ltu.sh`
from a terminal when switching to that fabric:

```bash
EFDI_LTU_PARTNER_NAMESPACE=<ltu-issued-slot-id> ./scripts/connect-ltu.sh
```

It asks for the key passphrase with hidden input, verifies the downloaded
public intermediate against the pinned LTU root and the remote endpoint
names, and writes only a full client chain plus an unencrypted runtime key
under ignored `compose/state/zenoh/tls/ltu/`. Zenoh has no private-key
passphrase setting; do not point the router directly at the encrypted source
key. The slot ID becomes the exact data root (`<slot-id>/**`) — do not prepend
a legacy vendor prefix and do not publish to the wildcard itself. For
unattended first boot, also provide `client-chain.pem` (leaf followed by its
intermediate) and make `client.key` an unencrypted runtime key so
`../examples/first-boot.sh` can discover and stage the profile automatically. Never
put a key passphrase in `.env`.

**`efdi-backbone/` — Backbone fabric identity.** Required filenames:
`cert.pem` (Backbone-issued client certificate), `key.pem` (matching private
key), `ca-roots.pem` (Backbone trust chain). `../examples/first-boot.sh` validates
that the bundle is complete and stages protected runtime copies; the Zenoh
WebUI `Backbone` preset then selects those copies and the matching Backbone
endpoint atomically.

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

## 5. ATAK Setup

### UDP multicast (same-subnet deployments)

1. **Settings → Network → Multicast** — enable multicast receiver
2. Verify `239.2.3.1:6969` appears in the address list
3. Tracks should appear within one poll cycle (≤ 10 s for drone detections, ≤ 60 s for radar keepalive)

### TAK Server

Set `TAK_HOST` and `TAK_PORT` in `.env`, then select `tak-layer` in the launcher.

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

`sitaware-hq-nvg` is the native Python output for an HQ-only deployment. It subscribes to EFDI tracks, keeps a bounded live snapshot, and exposes NVG 2.0.2 over a read-only HTTP(S) endpoint. SitaWare Headquarters polls it through **SitaWare Communication → NVG → NVG Import Subscriptions**. There is no separate NVG-XML ingest bridge — SitaWare ingress goes through the `sitaware` REST service instead.

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
> retired. Full explanation: [§7 Integrations → Egress topic views](#egress-topic-views-sapient-json-proto-raw).

| Service | Script | Zenoh topic (abbreviated) | Trigger |
| --- | --- | --- | --- |
| `asterix` | `protocols/vendors/asterix/cat.py` | `…/raw/asterix/catNN` and category-specific normalized ASTERIX topics | ASTERIX vendor's CAT protocol bundle: mixed UDP ingress plus per-category translators |
| `dronuradaras` | `bridges/dronuradaras_bridge.py` | `…/land/dronuradaras/acoustic/neutral/sensor/{type}/{id}/sapient` | 60 s online-only device poll with offline eviction / 10 s detection poll |
| `sitaware` | `bridges/sitaware_bridge.py` | `…/land/sitaware/c2/friendly/unit/{type}/{id}/sapient` | Configurable REST poll |
| `nffi` | `protocols/random/nffi.py` | `…/land/nato/c2/friendly/unit/{type}/{id}/sapient` | Complete XML documents under `…/raw/nffi/*` in Zenoh |
| `stanag` | `protocols/vendors/stanag/stanag.py --proto {4586,4607,4609,5516}` | `…/raw/stanag_4609/klv`, `…/air/stanag_4609/camera/unknown/uav`, STANAG 4586 track topics, and `…/{air,sea,land}/stanag_5516/c2/**` | Launcher starts each configured `--proto` directly |
| `sapient-raw`, `stanag4586-raw`, `stanag5516-raw` | `bridges/*_bridge.py` | `…/raw/<protocol>/<source>` | Optional socket ingress; matching protocol runs with `*_ZENOH_RAW=1` |
| `cap` | `protocols/random/cap.py` | `…/land/cap/c2/neutral/sensor/{type}/{id}/sapient` | Complete CAP 1.2 XML on `…/raw/cap/**` |
| `geojson` | `protocols/random/geojson_features.py` | `…/land/ogc/c2/neutral/zone/{type}/{id}/sapient` | GeoJSON/OGC Features on `…/raw/geojson/**` |
| `mqtt` | `protocols/random/mqtt_json.py` | `…/land/mqtt/iot/unknown/sensor/{type}/{id}/sapient` | Vendor JSON on `…/raw/mqtt/**` (bridge forwards any payload verbatim) |
| `sensorthings` | `protocols/random/sensorthings.py` | `…/land/sensorthings/iot/neutral/sensor/{type}/{id}/sapient` | Observations on `…/raw/sensorthings/**` |
| `sparkplug` | `protocols/vendors/sparkplug/sparkplug.py` | `…/land/sparkplug/iot/unknown/sensor/{type}/{id}/sapient` | Sparkplug B protobuf on `…/raw/mqtt/spBv1.0/**` |
| `spectrum` / `sensor-health` / `mission-route` | Matching `protocols/random/*.py` | `…/land/spectrum/**`, `…/land/health/**`, `…/air/mission/**` | JSON on their `…/raw/**` topics |
| `tak_layer` | `layers/tak_layer.py` | Subscriber — all topics | Event-driven |
| `tak-bridge` | `bridges/tak_bridge.py` | Subscriber — all topics | TAK-visible CoT ingress |
| `sitaware-hq-nvg` | `layers/sitaware_layer.py` | Subscriber — all track topics | Pull-based NVG snapshot |
| `track-fusion` | `protocols/fusion.py` | CAT-48 + CAT-21 subscriber | Event-driven |

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

## 7. Integrations

> **Want to connect a new sensor?** This page is the reference for what's
> already wired. For the step-by-step "how do I add one" walkthrough, see
> [§9 Adding a New Sensor or Protocol](#9-adding-a-new-sensor-or-protocol) below.

EFDI separates source-specific collectors, reusable protocol translators, and
TAK/SitaWare output layers:

- `compose/bridges/` polls or connects to a named product/service.
- `compose/protocols/` contains one independently launched wire/API protocol per
  file. ASTERIX categories are separate because their UAPs and editions differ.
- `compose/layers/` connects normalized Zenoh data to TAK/CoT and SitaWare/NVG.

Once an inbound script publishes a normalized topic, the running CoT and NVG
layers subscribe automatically. Receiver and detection systems normally attach
to a nearby Zenoh router and publish there; the router relays their data. Most
router hosts therefore need no receiver hardware or vendor driver.

### Protocol connection requirements

ASTERIX category numbers do not define TCP or UDP port numbers. The radar or
surveillance gateway management interface must be configured with the EFDI
host as its destination and with the same transport/port selected below. EFDI
uses UDP 50034 for CAT-034 and UDP 50048 for CAT-048 as deterministic local
conventions; these are not EUROCONTROL or Saab defaults. UDP 50000 is the
generic raw ingress. `udp_ingress_bridge.py` preserves every datagram and safely
publishes complete ASTERIX frames unchanged to `…/raw/asterix/catNN`; every
category translator remains a separate process and subscribes only to its own
topic. `ASTERIX_CATEGORIES` selects which categories are auto-dispatched.
Dedicated UDP/TCP inputs remain active at the same time.

When the radar-side laptop already publishes complete frames through another
Zenoh router, set `ASTERIX_ZENOH_UPSTREAM_ENDPOINT` and optionally
`ASTERIX_ZENOH_UPSTREAM_ROOT`. `asterix_bridge.py` subscribes to every
`…/raw/asterix/catN` topic at that router, verifies that the topic category and
ASTERIX header agree, and republishes the unchanged frame locally. The same
category translators then decode it; the bridge itself does not interpret a
UAP. Plaintext `tcp/...:7448` is for isolated testing only.

The mixed bridge also supports `ASTERIX_BIND`, `ASTERIX_MULTICAST_GROUP`,
`ASTERIX_MULTICAST_INTERFACE`, and an IPv4/CIDR `ASTERIX_ALLOW_SOURCE` filter.
Before configuring a new feed, observe it without Zenoh publication:

```bash
python3 tools/asterix_probe.py --port 30001
```

The probe reports sender IP, destination port, category, first-FRN SAC/SIC when
present, frame counts, and rate. For a multicast feed, add `--multicast-group`
and `--multicast-interface`.

`asterix_probe.py` lives in `tools/`, alongside `asterix_relay.py`, rather than
in `compose/` — nothing in `tools/` is imported by the pod, started by
`start.sh`/`run.sh`, or built into any image. These are standalone operator
utilities run by hand, usually from a laptop or the PC wired to a radar, while
commissioning a feed: `compose/bridges/` and `compose/protocols/` are the
running data plane, `tools/` is field tooling. `asterix_relay.py` forwards
ASTERIX UDP datagrams unchanged, byte-for-byte, from a local port to a remote
`IP:PORT` — run it on the machine connected to the radar when that machine is
on the NetBird mesh but the radar itself is not routable from the pod:

```bash
python3 tools/asterix_relay.py --dest 100.x.y.z:30048
```

`tests/test_asterix_raw_pipeline.py` exercises the framing logic these tools
share with the decoders, so changes here are caught by the normal test run.

| Protocol script | Transport role | Required partner/runtime configuration | Current contract |
|---|---|---|---|
| `vendors/asterix/cat.py --category 1` | UDP listener or TCP server | Producer sends to `CAT1_PORT`; set `CAT1_RADAR_LAT/LON` to georeference polar/cartesian-only plots/tracks | EUROCONTROL CAT-001 Ed.1.4 monoradar plot/track reports (legacy, superseded by CAT-048 for most modern radars) |
| `vendors/asterix/cat.py --category 2` | UDP listener or TCP server | Producer sends to `CAT2_PORT` | EUROCONTROL CAT-002 Ed.1.2 monoradar service messages (north marker, sector crossing, station status) |
| `vendors/asterix/cat.py --category 4` | UDP listener or TCP server | Producer sends to `CAT4_PORT` | EUROCONTROL CAT-004 Ed.1.13 safety net alerts (STCA/MSAW/APW/RIMCA/...) |
| `vendors/asterix/cat.py --category 7` | UDP listener or TCP server | Producer sends to `CAT7_PORT`; set `CAT7_RADAR_LAT/LON` to georeference polar/cartesian-only reports | EUROCONTROL CAT-007 Ed.1.12 directed interrogation messages (military Mode 4/5/S interrogation control; downlink and uplink UAPs) |
| `vendors/asterix/cat.py --category 8` | UDP listener or TCP server | Producer sends to `CAT8_PORT` | EUROCONTROL CAT-008 Ed.1.3 monoradar derived weather information (weather-image vectors/contours) |
| `vendors/asterix/cat.py --category 9` | UDP listener or TCP server | Producer sends to `CAT9_PORT` | EUROCONTROL CAT-009 Ed.2.1 composite weather reports (merged multi-radar weather picture) |
| `vendors/asterix/cat.py --category 10` | UDP listener or TCP server | Producer sends to `CAT10_PORT`; set airport reference coordinates if reports use only local X/Y or polar positions | EUROCONTROL CAT-010 Ed.1.1, airport surface targets/status |
| `vendors/asterix/cat.py --category 11` | UDP listener or TCP server | Producer sends to `CAT11_PORT`; set `CAT11_SITE_LAT/LON` to georeference cartesian-only reports | EUROCONTROL CAT-011 Ed.1.3 A-SMGCS system tracks (fused airport-surface aircraft + vehicles with flight-plan correlation) |
| `vendors/asterix/cat.py --category 15` | UDP listener or TCP server | Producer sends to `CAT15_PORT`; set `CAT15_SITE_LAT/LON` to georeference range/azimuth-only reports | EUROCONTROL CAT-015 Ed.1.2 independent non-cooperative surveillance (passive/multi-static) target reports |
| `vendors/asterix/cat.py --category 16` | UDP listener or TCP server | Producer sends to `CAT16_PORT` | EUROCONTROL CAT-016 Ed.1.0 independent non-cooperative surveillance system configuration reports (the INCS ground system's own site position/transmitter/receiver config, sister status category to CAT-015) |
| `vendors/asterix/cat.py --category 17` | UDP listener or TCP server | Producer sends to `CAT17_PORT` | EUROCONTROL CAT-017 Ed.1.3 Mode S Surveillance Coordination Function messages (legacy inter-radar cluster/hand-over protocol; "Track Data" messages carry a position, network-management messages don't) |
| `vendors/asterix/cat.py --category 18` | UDP listener or TCP server | Producer sends to `CAT18_PORT`; set `CAT18_SITE_LAT/LON` to georeference the local polar/cartesian-only position items | EUROCONTROL CAT-018 Ed.1.8 Mode S Datalink Function messages (GDLP/interrogator uplink-downlink coordination: aircraft reports, uplink packet/broadcast/GICB-extraction requests and acknowledgements) |
| `vendors/asterix/cat.py --category 19` | UDP listener or TCP server | Producer sends to `CAT19_PORT` | EUROCONTROL CAT-019 Ed.1.3 MLT system status |
| `vendors/asterix/cat.py --category 20` | UDP listener or TCP server | Producer sends to `CAT20_PORT` and confirms Edition 1.11 | EUROCONTROL CAT-020 Ed.1.11 MLAT reports |
| `vendors/asterix/cat.py --category 21` | UDP listener or TCP server | ADS-B gateway sends to `CAT21_PORT` and confirms Edition 2.7 | EUROCONTROL CAT-021 Ed.2.7 ADS-B reports |
| `vendors/asterix/cat.py --category 23` | UDP listener or TCP server | Producer sends to `CAT23_PORT` | EUROCONTROL CAT-023 Ed.1.3 CNS/ATM ground station service messages (ADS-B/TIS-B/FIS-B/GRAS/MLT station status) |
| `vendors/asterix/cat.py --category 25` | UDP listener or TCP server | Producer sends to `CAT25_PORT` | EUROCONTROL CAT-025 Ed.1.6 CNS/ATM ground system status reports (successor/companion to CAT-023: split system/service status, per-component status list, service statistics, site position) |
| `vendors/asterix/cat.py --category 32` | UDP listener or TCP server | Producer sends to `CAT32_PORT` | EUROCONTROL CAT-032 Ed.1.2 Miniplan Reports to an SDPS (FPPS/SDPS flight-plan-to-track-number correlation; no position field exists in this category) |
| `vendors/asterix/cat.py --category 34` | UDP listener or TCP server | Radar sends CAT-034 alone to `CAT34_PORT` (EFDI convention: UDP 50034) | EUROCONTROL CAT-034 Ed.1.29 radar service messages |
| `vendors/asterix/cat.py --category 48` | UDP listener or TCP server | Radar sends CAT-048 alone to `CAT48_PORT` (EFDI convention: UDP 50048); local polar positions require `CAT48_RADAR_LAT/LON` | EUROCONTROL CAT-048 Ed.1.32 targets |
| `vendors/asterix/cat.py --category 62` | TCP client or UDP listener | Set `CAT62_HOST/PORT`, or `CAT62_UDP=1`; confirm Edition 1.21 | EUROCONTROL CAT-062 Ed.1.21 system tracks |
| `vendors/asterix/cat.py --category 63` | UDP listener or TCP server | Producer sends to `CAT63_PORT` | EUROCONTROL CAT-063 Ed.1.7 sensor status reports (the sensors feeding a CAT-062 tracker) |
| `vendors/asterix/cat.py --category 65` | UDP listener or TCP server | Producer sends to `CAT65_PORT` | EUROCONTROL CAT-065 Ed.1.6 SDPS service status reports (the SDPS-side companion to CAT-062, same relationship CAT-019 has to CAT-020) |
| `vendors/asterix/cat.py --category 150` | UDP listener or TCP server | Producer sends to `CAT150_PORT` | EUROCONTROL CAT-150 Ed.3.0 MADAP Plan Server Flight Data Message (Maastricht UAC legacy flight-plan distribution/correlation/conflict data; no position field in this edition) |
| `vendors/asterix/cat.py --category 205` | UDP listener or TCP server | Producer sends to `CAT205_PORT`; set `CAT205_SITE_LAT/LON` to georeference cartesian-only reports | EUROCONTROL CAT-205 Ed.1.0 Radio Direction Finder reports (RDF network triangulating a radio transmitter's position, typically an aircraft's VHF radio) |
| `vendors/asterix/cat.py --category 240` | UDP listener or TCP server | Producer sends to `CAT240_PORT` | EUROCONTROL CAT-240 Ed.1.3 Radar Video Transmission (raw pre-plot-extraction signal-level video, not a target report; messages can carry up to ~64KB of video data) |
| `vendors/asterix/cat.py --category 247` | UDP listener or TCP server | Producer sends to `CAT247_PORT` | EUROCONTROL CAT-247 Ed.1.3 Version Number Exchange (a source reports which edition of each ASTERIX category it transmits) |
| `vendors/sapient/flex335.py` | TCP listener or client | Edge node connects to `SAPIENT_LISTEN_PORT`, or set middleware `SAPIENT_HOST/PORT`; remote listeners require an allowed source CIDR | BSI FLEX 335 v2 framing and public SAPIENT protobuf subset |
| `nffi.py` | Zenoh subscriber/translator | Publisher writes one complete XML document under `…/raw/nffi/{source-id}` | NATO NFFI / ADatP-36 (STANAG 5527) XML subset |
| `vendors/stanag/stanag.py --proto 4586` | TCP client | Set CUCS/VSM `STANAG4586_HOST/PORT`; validate the VSM ICD before selecting `STANAG4586_PROFILE=legacy_ed3_approx` | Historical deployment layout, disabled by default; not claimed as a generic STANAG 4586 decoder |
| `vendors/stanag/stanag.py --proto 4607` | Zenoh raw subscriber | A bridge places complete packets on `…/raw/stanag_4607/**`; the STANAG defines the message, not the bearer | NATO GMTI (Ground Moving Target Indicator) Format — Mission/Dwell/Job Definition/Platform Location segments, one track per Target Report |
| `vendors/stanag/stanag.py --proto 4609` | SRT/KLV input | Set `STANAG4609_SRT_URL` for the motion-imagery metadata stream | MISB ST 0601 KLV local-set subset over STANAG 4609 motion imagery; SRT is the configured transport, not part of the KLV schema |
| `vendors/stanag/stanag.py --proto 5516` | UDP listener | Set `STANAG5516_PORT` (default 3010); gateway sends JREAP-C-encapsulated Link 16 J-series | MIL-STD-6016F / STANAG 5516 Ed.5 J2.2/J2.5/J3.2/J3.5/J3.7 subset over JREAP-C (MIL-STD-3011) |

`stanag.py` merges all four STANAG variants EFDI speaks into one file (decode
and, where applicable, encode together) — `--proto {4586,4607,4609,5516}` selects
which one a given process runs; see `proto/stanag.proto` for the wire
message shapes.

All twenty-seven ASTERIX translators also accept `--zenoh-raw` (or their corresponding
`CATNN_ZENOH_RAW=1`) for an exact complete frame on `…/raw/asterix/catNN`.
Launchers select that mode automatically for categories listed in
`ASTERIX_CATEGORIES` whenever generic UDP ingress or the upstream Zenoh
ASTERIX bridge is configured.

VERA-NG passive sensors that provide CAT-34 and CAT-48 use this same raw
ASTERIX path; they do not need a VERA-specific bridge. Give every reporting
source a unique SAC/SIC pair and leave `CAT34_RADAR_NAME` blank when Giraffe
and VERA sources share one feed, so their site, status, coverage, and target
state remain independently identified as `RADAR SACx/SICy`. Prefer the live
CAT-34 I034/120 site position and I034/100 coverage values. Before operational
use, capture representative frames and confirm the producer's CAT-34/CAT-48
editions and UAP against the configured Ed.1.29/Ed.1.32 decoders. A passive
sensor must not be given a synthetic rotating sweep: EFDI only renders sweep
motion when the source actually sends the applicable CAT-34 timing messages.

ASTERIX is a bit-level surveillance exchange family; category and edition must
match the producer. EUROCONTROL publishes CAT-010 for surface movement,
CAT-021 for ADS-B target reports, CAT-062 for system tracks, and CAT-240 for raw
radar video. CAT-240 is not a map-track feed and needs radar-video processing
before TAK/SitaWare publication. See the [EUROCONTROL ASTERIX catalogue](https://www.eurocontrol.int/asterix),
[CAT-010 Ed.1.1 specification](https://www.eurocontrol.int/sites/default/files/service/content/documents/nm/asterix/cat010-asterix-monoradar-surface-movement-data-part-7.pdf),
[CAT-021](https://www.eurocontrol.int/publication/cat021-eurocontrol-specification-surveillance-data-exchange-asterix-part-12-category-21),
[CAT-062](https://www.eurocontrol.int/publication/cat062-eurocontrol-specification-surveillance-data-exchange-asterix-part-9-category-062),
and [CAT-240](https://www.eurocontrol.int/publication/cat240-eurocontrol-specification-surveillance-data-exchange-asterix).

### Egress topic views: `/sapient`, `/json`, `/proto`, `/raw`

Every decoded track is published on one object key with four sibling views.
They carry the same event at different fidelity, so a consumer subscribes to
exactly one view and ignores the rest. Nothing is implicit — a consumer reading
a key always knows what the bytes are.

```
{prefix}/{pod}/{domain}/{source}/{modality}/{affiliation}/{entity}/{type}/{id}/{view}
```

| View | Topic suffix | Zenoh encoding | Payload |
|---|---|---|---|
| SAPIENT | `…/{id}/sapient` | `application/protobuf` | BSI Flex 335 v2 `SapientMessage`. **The fabric contract.** |
| JSON | `…/{id}/json` | `application/json` | Flat JSON object. Only the fields the decoder models. |
| Protobuf | `…/{id}/proto` | `application/protobuf` | Typed message from the protocol's `.proto` (`compose/protocols/`). |
| Raw | `…/{id}/raw` | `application/protobuf` | `RawEnvelope` wrapping the **original wire bytes**, unmodified. |

**Which view to use.** Default to `/sapient`: it is the agreed contract for data
leaving the fabric. Use `/proto` when you need full per-protocol sensor detail
SAPIENT does not model, and `/raw` when you need a field EFDI does not decode at
all, or want to run the vendor's own decoder over the exact bytes. `/json` is
for humans and for consumers that cannot link a protobuf runtime.

**Native payloads are byte-exact.** They are re-wrapped, never re-encoded:

- ASTERIX — one standalone data block per record: the CAT byte and 2-byte
  length header are re-added, so any off-the-shelf ASTERIX decoder reads it.
- SAPIENT — the original BSI Flex 335 v2 `SapientMessage`, with the 32-bit
  length prefix already stripped.
- STANAG 4609 — the raw MISB KLV packet.

The `RawEnvelope` (`../compose/protocols/proto/raw_envelope.proto`) carries
`protocol`, `profile` (e.g. `cat048`, `misb-st0601`), `content_type`, and the
`payload` bytes.

**Fidelity caveat.** `/sapient`, `/json` and `/proto` are only as complete as
the decoder. When a value cannot be represented in the target contract it is
dropped from that view and a `protobuf encode failed …` line is logged — one
view failing never blocks the others. `/raw` is the only view that can never
lose a field.

All four views sit under the pod's first-party publish prefix, so the existing
`${DATA_TOPIC_ROOT}/**` router ACL already covers them — adding a view needs no
ACL change.

#### External catalog compatibility

Some upstream portals distinguish the certificate-backed slot from a
human-readable vendor alias. Treat the authenticated `whoami`/identity response
as authoritative: the alias is display metadata and must not replace the
certificate slot in Zenoh keys unless the upstream ACL explicitly says so.

The trial portal registry accepts exact topic keys, not Zenoh `*` or `**`
expressions. High-rate collection publishers therefore keep object identity in
the payload and use stable sibling keys:

```text
.../aircraft/tracks/v1          JSON
.../aircraft/sapient/tracks/v1  BSI FLEX 335 v2 SAPIENT protobuf
.../aircraft/proto/tracks/v1    source-specific protobuf
```

`publish_collection()` enforces one encoding per exact key for ADS-B and fused
aircraft collections. Per-object topics remain useful inside fabrics whose
catalog supports patterns, but must not be the only output when the external
catalog requires exact registration.

### Vendored third-party schemas

`compose/protocols/vendors/sapient/sapient_msg/` carries the BSI Flex 335 v2.0 (SAPIENT) `.proto`
schemas verbatim from [github.com/dstl/SAPIENT-Proto-Files](https://github.com/dstl/SAPIENT-Proto-Files)
(`bsi_flex_335_v2_0/`) — **do not edit these files**; they are upstream
contracts, and a local edit would silently diverge EFDI's wire format from the
standard it claims to speak. Re-vendor from upstream instead. Licensed Apache
License 2.0 (see `sapient_msg/LICENCE.txt`, which permits use, modification,
and redistribution including commercial/defence use as long as the licence and
copyright notices are retained); the British Standards Institution retains
ownership and copyright of BSI Flex 335, with publication rights held by BSI
Standards Ltd.

It lives under `compose/protocols/vendors/sapient/` rather than directly under
`compose/protocols/proto/` because that directory holds EFDI's *own*
contracts, while this is someone else's — it carries its own package
(`sapient_msg.bsi_flex_335_v2_0`) and internal import paths of the form
`sapient_msg/bsi_flex_335_v2_0/<file>.proto` that only resolve if this
directory is its own protoc include root, so `scripts/generate-protobuf.sh`
passes it as a second `-I` root alongside `-I compose`. EFDI both reads and
writes SAPIENT: `compose/protocols/vendors/sapient/flex335.py` decodes
incoming SAPIENT with a hand-written protobuf reader (field numbers verified
against these files) and encodes outbound tracks into a real
`SapientMessage`/`DetectionReport` in the same file, so a consumer needs to
understand only SAPIENT rather than every source protocol.

### Source-specific bridges

| Bridge | Endpoint behavior | Configuration needed |
|---|---|---|
| Generic UDP | Preserves every datagram and safely auto-dispatches complete ASTERIX frames | `UDP_INGRESS_PORT`, optional bind/multicast/source filter, and ASTERIX dispatch categories |
| dronuradaras.lt | Polls its fixed public HTTPS API | None |
| meteo.lt | Polls the fixed public HTTPS API | Optional places/rate |
| SitaWare HQ REST inbound | Polls deployment-specific resource | URL, credentials, and the real `SITAWARE_API_PATH`; there is no universal units URL |
| Track fusion | Subscribes to local Zenoh topics | No external endpoint; starts working when normalized tracks arrive |

#### Radar operator UDP relay

Copy `scripts/radar_udp_relay.py` to the radar operator's Windows computer.
It has no third-party dependencies. If the radar sends UDP to local port
50048, for example, run:

```powershell
py .\radar_udp_relay.py --listen-port 50048
```

The relay forwards every datagram unchanged to `asusrog.efdi.ltu:50000`.
Override `--destination-host` when mesh DNS is unavailable. Configure this
router with `UDP_INGRESS_PORT=50000`. The generic receiver preserves every
datagram on its raw Zenoh topic and only auto-dispatches protocols whose framing
is unambiguous. UDP 50034 and 50048 remain separate deterministic CAT-034 and
CAT-048 listeners.

On the EFDI laptop, inspect traffic without taking ownership of the UDP socket:

```bash
./scripts/capture-radar-udp.sh
./scripts/capture-radar-udp.sh any giraffe-50000.pcap
```

The first command displays packet bytes; the second saves a full packet capture
for offline decoder work. Both use tcpdump and can run while the generic UDP
ingress is bound to port 50000.

### Output layers

| Layer | Automatic input | External configuration |
|---|---|---|
| CoT/TAK output | Subscribes to matching normalized Zenoh topics | TAK TCP/mTLS host, or ATAK/WinTAK UDP destination |
| CoT receiver | Converts an attached TAK or SitaWare CoT stream into Zenoh | Listen port or remote host; TAK uses TAK-issued mTLS credentials |
| SitaWare HQ NVG | Maintains an automatic normalized-track snapshot | HQ is configured to poll the EFDI URL; TLS and dedicated credentials are required outside an isolated lab |

### C2 to Zenoh and back

Output and input are separate services. Enabling a TAK or SitaWare output does
not silently enable the reverse path.

#### TAK Server

For Zenoh → TAK, configure `TAK_HOST/TAK_PORT` and select `tak_layer`
(`layers/tak_layer.py`). It subscribes to normalized Zenoh topics and emits CoT
two ways: UDP multicast to `239.2.3.1:6969` for LAN ATAK clients, and TCP/mTLS to
a TAK Server. TAK-issued client credentials are required when `TAK_TLS=1`. For
TAK → Zenoh, select `tak-bridge` (`bridges/tak_bridge.py`), which normalizes an
inbound CoT stream back onto the fabric. Prefer a stable DNS `TAK_HOST`; if the
TAK server certificate has a different legacy DNS SAN, set
`TAK_TLS_SERVER_NAME` to that SAN so hostname verification remains enabled.

#### SitaWare

For Zenoh → SitaWare HQ, select `sitaware_layer` (`layers/sitaware_layer.py`) and configure
an HQ NVG Import Subscription to poll the authenticated NVG 2.0.2 feed it serves.

For SitaWare HQ → Zenoh, obtain the real REST resource from the deployment ICD:

```dotenv
SITAWARE_URL=https://sitaware.example
SITAWARE_USER=<runtime-user>
SITAWARE_PASS=<runtime-secret>
SITAWARE_API_PATH=/<documented-resource>
SITAWARE_TLS_VERIFY=1
```

Select `sitaware`; it publishes normalized units below
`…/{domain}/sitaware/c2/{affiliation}/{entity}/{type}/{id}/sapient`.

The current runtime keeps the SitaWare HQ REST and NVG paths separate. If the
deployment exports NFFI instead, publish complete NFFI XML documents under
`…/raw/nffi/{source-id}` and run the independent `nffi` translator.

All resulting records stay in the producing pod's namespace. Authorized
federation routes may relay that namespace to other partner routers, whose TAK
and SitaWare output layers consume the normalized topics automatically. An
adapter must never write directly into another partner's namespace.

Operator-side configuration is documented step-by-step in the
[§8 C2 ↔ Zenoh bidirectional runbook](#8-c2--zenoh-bidirectional-runbook). In brief,
TAK Server requires a dedicated client identity, the correct IN/OUT groups and
a TAK-issued certificate; SitaWare HQ NVG input is created under **SitaWare
Communication → NVG → NVG Import Subscriptions**; and a licensed SitaWare CoT
Gateway must be given one TCP role, the EFDI endpoint, an approved export-layer
set, and an explicit exclusion for `EFDI Live Tracks`. Product screens not
present in the installed license/release cannot be substituted with a guessed
REST path.

### Client SDKs — connecting to the pod (`clients/`)

This section is for the people **consuming** a pod: publishing data to the
EFDI fabric and receiving data from it, in their own language and tooling —
partners integrating against your pod, not sensors/protocols wired into it
(that's the rest of this document). The code lives in `clients/`:

```text
clients/
├── connect/             minimal "cert bundle -> Zenoh session" helper per language
├── examples/
│   ├── modern/          idiomatic pub/sub/request-reply per language
│   ├── military-legacy/ older toolchains, offline/air-gapped, file/HTTP fallbacks
│   └── bridges/         use a protocol you already speak — no Zenoh code in your app
└── README.md
```

| You are… | Use |
|---|---|
| A modern dev (Python/TS/Go/Rust/Java/C++) | `examples/modern/<lang>/` |
| On an older / less-common stack (C, Java 8, .NET Framework, MATLAB) | `examples/military-legacy/` |
| Speaking a protocol you already have (HTTP, files) — no Zenoh code | `examples/bridges/` |
| Just want the minimal connect snippet | `connect/<lang>/` |

#### The model in 30 seconds

The pod runs a **Zenoh router**; a client talks to it as a **Zenoh client over
mTLS**. Three operations, that's the whole API:

1. **Publish** (`put`) to keys under **your namespace** — e.g. `release/<you>/sensors/temp`.
2. **Subscribe** (`sub`) to keys you're allowed to read — your own, plus
   `release/<partner>/**` for data a partner sends you (bilateral relationships).
3. **Query** (`get`) for the latest/historical value of a key (optional).

Keys are slash-paths (`a/b/c`); subscriptions use `*` (one segment) and `**` (any depth).

Every example reads the same five things from **environment variables**, so
credentials are never hardcoded:

| Env var | What it is | Example |
|---|---|---|
| `EFDI_ROUTER` | the pod's Zenoh endpoint | `tls/127.0.0.1:7447` (pod on your box) |
| `EFDI_CERT` | your mTLS client certificate (PEM) | `/etc/efdi/mycert.pem` |
| `EFDI_KEY` | your mTLS private key (PEM) | `/etc/efdi/mykey.pem` |
| `EFDI_CA` | the CA root that signs the router (PEM) | `/etc/efdi/ca-root.pem` |
| `PARTNER_NAMESPACE` | the prefix you own (publish under this) | `release/acme` |

`scripts/gen-certs.sh <namespace>` writes these into `compose/certs/`
(`<namespace>-cert.pem`, `<namespace>-key.pem`, `efdi-ca-root.pem`); for a
downstream consumer the EFDI administrator hands over a copy of the same three
files out-of-band. If the pod is on the consumer's own machine, `EFDI_ROUTER`
is `tls/127.0.0.1:7447`; over the mesh, it's that host's mesh IP.

Targets **Zenoh 1.9.0** everywhere (the fleet-pinned version — see
`compose/docker-compose.yml`); use the matching-major client library
(`eclipse-zenoh`/`zenoh-c`/`zenoh-cpp`/`zenoh-go`/`zenoh-java`/`zenoh`
crate/`zenoh-ts`, all 1.x).

#### The one connection gotcha (every native binding hits this)

Zenoh's TLS config must be inserted as **one whole block** at
`transport/link/tls`, with **`enable_mtls: true`**. Setting the sub-keys one
at a time (`transport/link/tls/connect_certificate`, etc.) silently does
**not** turn on the client-cert send path on Zenoh 1.x — the session opens but
the router rejects the client, or it connects read-only. Every `connect/`
helper builds the *entire* block (`root_ca_certificate` / `connect_certificate`
/ `connect_private_key` / `enable_mtls` / `verify_name_on_connect`) as one
document and applies it in a single call — the language-specific mechanism
differs (`zc_config_from_str` in C, `Config::from_str` in C++,
`InsertJson5("transport/link/tls", …)` in Go, `Config.fromJson5` in Java,
`insert_json5(...)` in Rust, one `conf.insert_json5("transport/link/tls", …)`
in Python) but the rule is the same everywhere.

Also: when the router cert's SAN binds an **IP/mesh address** rather than the
DNS name being dialed, set `verify_name_on_connect`/`EFDI_VERIFY_NAME` to
`false` (the pod's local router at `127.0.0.1` needs this; a DNS-named remote
router keeps it `true`).

#### Bridges — talk to the pod in a protocol you already speak

A **bridge** is a small process that is itself a Zenoh mTLS client but exposes
a **different protocol** to the consumer's application — HTTP, a watched
directory. The application never links a Zenoh library and contains no Zenoh
code; it speaks the protocol it already knows, and the bridge does the Zenoh
part. This is the path for legacy/defense shops that cannot or will not link
`eclipse-zenoh`: MATLAB, PLCs, old .NET Framework, Java 8, air-gapped systems —
anything that can make an HTTP request or write a file.

| Use a **native client** (`connect/` + `examples/modern/`) | Use a **bridge** |
|---|---|
| Can link a Zenoh client (Python/Go/Rust/Java/C++) | Can't link one (toolchain, policy, certification) |
| Want lowest latency, full pub/sub/query | Want zero Zenoh code in the app |
| Modern language, controls the build | MATLAB / PLC / old .NET / air-gapped / file-only |
| Long-lived in-process subscriptions | "fire an HTTP call" or "drop a file" is all there is |

A bridge holds the consumer's mTLS client identity, so its plaintext side
(HTTP, a watched directory) is an unauthenticated door into the fabric — run
it **co-located with the consuming app, bound to `127.0.0.1` only**. If the
app is on another host, put the bridge next to *that* app instead, pointed at
the pod's mesh IP — the trust boundary moves to the bridge↔pod link (still
mTLS), but the plaintext side must never be exposed to an untrusted network.
Both bridges below are stdlib + `eclipse-zenoh` only (no web framework, no
file-watcher library) and ship an optional `Dockerfile` to run as a compose
sidecar.

**`bridges/file-drop/`** — exchange data as files in a directory: the most
universal path, for MATLAB, PLCs/SCADA, legacy .NET, shell pipelines, and
fully air-gapped edges. A file written under `OUTBOX_DIR` is published under a
key formed from its path relative to the outbox (`OUTBOX_DIR/sensors/temp` →
`<namespace>/sensors/temp`); the file then moves to `OUTBOX_DIR/.sent/`.
Inbound samples matching `SUB_KEYEXPR` are written into `INBOX_DIR` as files
named by their key (slashes → `__`) plus a millisecond timestamp, written
atomically (temp name, then rename) so a poller never reads a half-written
file. Poll-based, stdlib only (no inotify dependency); tune `POLL_SECONDS`
(default 1s); leave `SUB_KEYEXPR` empty to disable the inbound half.

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
export OUTBOX_DIR=./outbox INBOX_DIR=./inbox SUB_KEYEXPR='release/<partner>/**'
python3 bridge.py
```

**`bridges/rest-http/`** — plain HTTP, for `curl`, MATLAB (`webwrite`), old
.NET (`HttpClient`), Java 8 (`HttpURLConnection`), shell scripts, or a PLC's
HTTP block. Binds `127.0.0.1` only by default (`BRIDGE_BIND`/`BRIDGE_PORT`).

```sh
pip install eclipse-zenoh
export EFDI_ROUTER=tls/127.0.0.1:7447 EFDI_CERT=... EFDI_KEY=... EFDI_CA=... PARTNER_NAMESPACE=release/acme
python3 bridge.py                 # serves on http://127.0.0.1:8080

curl -X POST http://127.0.0.1:8080/pub/sensors/temp -d '21.5'                 # publish
curl 'http://127.0.0.1:8080/sub/sensors/temp?count=3'                          # receive N (blocks)
curl -N http://127.0.0.1:8080/stream/sensors/temp                              # SSE stream
WEBHOOK_URL=https://my-system.local/ingest WEBHOOK_KEYEXPR='release/<partner>/**' python3 bridge.py  # outbound webhook
```

A bare path (`sensors/temp`) is scoped under the caller's namespace; a full
key the caller has read rights to (e.g. `release/<partner>/...`) passes
through as-is. Received text comes back as `"text"` in the JSON response, or
`"b64"` if the bytes aren't valid UTF-8.

#### Military / legacy / less-common stacks (`examples/military-legacy/`)

For pinned JDK 8, .NET Framework 4.x, MATLAB, C89/C99, and **air-gapped**
shops that can't reach the internet, can't `pip install`, and often can't link
a native Zenoh client at all. Work top to bottom, stop at the first row that's true:

| If… | Use | Why |
|---|---|---|
| A native Zenoh binding builds and links (have `zenoh-c`, a C compiler, policy allows it) | **native** — `c99/` | lowest latency, full pub/sub/query, no extra process |
| Can make an **HTTP request** (any language) | **REST bridge** — `bridges/rest-http/` + the `java8/`, `dotnet-framework/`, `matlab/` examples | zero Zenoh code; works on any stack with an HTTP client |
| Can only **read/write files** (locked-down box, SCADA/PLC, shell pipeline) | **file-drop bridge** — `bridges/file-drop/` + `matlab/receive_filedrop.m` | the most universal path — if it can write a file, it can publish |

```text
legacy app  ──HTTP / files──▶  bridge (localhost, holds mTLS)  ──Zenoh mTLS──▶  pod router
```

The Java/.NET/MATLAB examples are **not** Zenoh clients — ~80-line programs
using nothing but the language's stdlib against the local bridge; they compile
with tools already on the box (`javac`, `csc.exe`, MATLAB's editor), no Maven,
NuGet, or Gradle.

**Offline / air-gapped, once, for all four stacks:**

1. Get the pieces in over sneakernet: the pod itself (handed over by the
   operator), the mTLS cert bundle (`mtls.cert.pem`, `mtls.key.pem`,
   `ca-roots.pem`, namespace), and — only for a bridge's Python runtime — a
   vendored `eclipse-zenoh` wheelhouse:
   ```sh
   # on a CONNECTED machine matching the air-gapped box's OS/arch/python:
   pip download eclipse-zenoh==1.9.0 -d zenoh-wheelhouse/
   # carry zenoh-wheelhouse/ across, then on the AIR-GAPPED box:
   pip install --no-index --find-links zenoh-wheelhouse/ eclipse-zenoh==1.9.0
   ```
   The legacy app itself needs nothing vendored — that's the point of going
   through a bridge.
2. Everything is localhost: the pod, the bridge, and the app all run on one
   box. No DNS, no proxy, no internet; the only hop is bridge→pod
   (`tls/127.0.0.1:7447`).
3. **Clock sync is the silent killer.** mTLS rejects certificates whose
   validity window doesn't contain *now*. A box with a dead RTC or no NTP will
   drift, and the **bridge's** session to the pod fails handshake with a
   confusing "certificate not yet valid/expired" — even though the
   app→bridge HTTP call looks fine. Symptom: the bridge logs a TLS error on
   startup and never prints `bridge on http://…`. Fix the clock first
   (`date`; `sudo date -s '2026-06-02 14:30:00'` on Linux, `w32tm /resync` or
   manual on Windows) before debugging anything else — one fix covers pod +
   bridge since they share the box.

**`military-legacy/c99/`** — pure C99, just libc + `libzenohc` + a `Makefile`
(no CMake required for the examples themselves, though building `zenoh-c`
needs it), against [`zenoh-c`](https://github.com/eclipse-zenoh/zenoh-c)
1.9.0 directly (no bridge — this is a native client). Each example is a
single self-contained `.c` with the connect logic inlined. Requires
`zenoh-c` built with `-DZENOHC_BUILD_WITH_UNSTABLE_API=ON` (the
`zc_config_from_str` entry point the one-block mTLS config needs is gated
behind the unstable API) — built from source, from a prebuilt GitHub Releases
artifact, or fully vendored (`cargo vendor`) for offline builds.

```sh
make                        # dynamic link
make static                 # static-link libzenohc.a -> single self-contained binary
                             # (macOS static linking also needs -framework Security -framework CoreFoundation)
./publish                   # one JSON sample; ./publish 50 200 for 50 samples, 200ms apart
./subscribe                 # everything under your namespace; ./subscribe 'release/<partner>/**'
```

**`military-legacy/java8/`** — JDK 8 (the modern `zenoh-java` binding needs
JDK 17+), via the REST bridge over `java.net.HttpURLConnection` only. No
Maven/Gradle/jars.

```sh
javac Publish.java Subscribe.java
java Publish sensors/temp '{"temp_c":21.5}'
java Subscribe sensors/temp stream          # follow continuously
```

**`military-legacy/dotnet-framework/`** — .NET Framework 4.x (4.5–4.8, not
modern .NET), via the REST bridge over `System.Net.HttpWebRequest` (present
since Framework 2.0; more predictable than `HttpClient` for the open-ended SSE
stream). Build with `csc.exe` directly, or the included classic (non-SDK)
`.csproj` via `msbuild` — neither touches NuGet.

```bat
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /out:EfdiBridgeClient.exe Program.cs
EfdiBridgeClient.exe pub sensors/temp {"temp_c":21.5}
EfdiBridgeClient.exe stream sensors/temp
```

**`military-legacy/matlab/`** — MATLAB via `webwrite`/`webread` (REST bridge,
`publish.m`/`receive_rest.m`) or plain file I/O (file-drop bridge,
`receive_filedrop.m`) for the most locked-down boxes — no toolbox, no MEX, no
network call at all in the file-drop path.

```matlab
publish('sensors/temp', '{"temp_c":21.5}')
s = receive_rest('sensors/temp', 'Count', 5, 'TimeoutSec', 60);
receive_filedrop('./inbox', 'Callback', @(key,bytes) disp(key))
```

#### Modern language bindings (`examples/modern/`)

Idiomatic pub/sub/request-reply per language, each pairing the official Zenoh
binding with a small `connect/<lang>/` helper that applies the one-block mTLS
config above. All target Zenoh 1.9.0; if a symbol doesn't resolve on a
different pinned minor, re-check that tag's own upstream examples — that's
true for every language below and isn't repeated per-entry.

**`modern/python/`** — official `eclipse-zenoh`. `pip install eclipse-zenoh`,
then `python3 publish.py` / `python3 subscribe.py` / `python3 request_reply.py
{serve,get}`. On Windows use `python` (not `python3`) inside the venv or it
hits the system interpreter and raises `ModuleNotFoundError`.

**`modern/cpp/`** — official [`zenoh-cpp`](https://github.com/eclipse-zenoh/zenoh-cpp),
a **header-only wrapper over `zenoh-c`** — install `zenoh-c` 1.9.0 first
(unstable API on), then `zenoh-cpp` 1.9.0, then CMake-build the examples.
`find_package(zenohcxx)` failing means zenoh-cpp isn't on
`CMAKE_PREFIX_PATH`; linking failing on `libzenohc` means zenoh-c isn't
installed — neither is an API problem.

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build
./build/publish; ./build/subscribe
```

**`modern/go/`** — the official binding (landed with Zenoh 1.9.x
"Longwang", April 2026) is a **cgo wrapper over `zenoh-c`**, not pure Go —
install `zenoh-c` 1.9.0 first (unstable API on), `CGO_ENABLED=1` required,
cross-compiling is awkward. Import path is
`github.com/eclipse-zenoh/zenoh-go/zenoh` (the old top-level/`zenoh-net`
0.4.x API is abandoned since 2020 — do not use it). No pure-Go/no-cgo option
exists today; use a bridge instead if that's a hard requirement.

```sh
go run publish.go; go run subscribe.go 'release/<partner>/**'
```

**`modern/java/`** — official `zenoh-java` (JDK 17+ — a Kotlin/JVM binding).
Unlike Go, the published `zenoh-java-jvm` artifact **bundles the native
library as a JAR resource**, so a normal Gradle dependency
(`org.eclipse.zenoh:zenoh-java-jvm:1.9.0`) is enough — no separate `zenoh-c`
install. Generate the wrapper once with `gradle wrapper`, then:

```sh
./gradlew run -Pmain=Publish
./gradlew run -Pmain=Subscribe --args="release/<partner>/**"
```

**`modern/rust/`** — the official [`zenoh`](https://crates.io/crates/zenoh)
crate — **pure Rust** (the reference implementation, no C library to
install), async (tokio). `cargo build` pulls `zenoh =1.9.0` + tokio.

```sh
cargo run --bin publish; cargo run --bin subscribe -- 'release/<partner>/**'
```

**`modern/typescript/`** — official `@eclipse-zenoh/zenoh-ts`. **This one is
architecturally different from the rest:** it does not open a direct Zenoh
session over mTLS. It talks to a `zenoh-plugin-remote-api` loaded inside
`zenohd` over **WebSocket** (`ws://`/`wss://`); `Config` takes only a locator
string, with no client-cert/TLS block on the TS side at all. The mesh-side
mTLS is configured in the **pod's `zenohd`**, on the links the router itself
makes — the plugin is the trust boundary between the WebSocket client and the
mesh. The pod operator must enable the plugin (`plugins.remote_api.websocket_port`
in the `zenohd` config; not on by default); front it with `wss://` for
anything but loopback. If a typed native API isn't specifically needed, the
REST/WebSocket bridge above is often the simpler path for Node/browser
consumers.

Env vars differ from the native bindings: `EFDI_WS` (preferred; e.g.
`ws/127.0.0.1:10000`) or `EFDI_ROUTER` as a fallback (host reused with port
`10000`); `EFDI_CERT`/`EFDI_KEY`/`EFDI_CA` are unused unless the plugin is
fronted with `wss://` on a private CA, in which case `NODE_EXTRA_CA_CERTS`
(Node) trusts it (browsers need the CA in the OS/browser trust store).

zenoh-ts targets browser/Deno first; under **Node** it needs a global
`WebSocket`, shimmed via the `ws` package (Node 22+ ships one natively, making
the shim a no-op; Node 18/20 need it) and loaded before the example via
`tsx`. Deno needs no polyfill (`deno run --allow-net --allow-env --allow-read
subscribe.ts`) and is the upstream-blessed runtime if Node shimming proves
fragile. Key resolution goes through a **WASM** module — ensure the bundler/
runtime can load `.wasm` (tsx/Deno handle this by default).

```sh
npm install
npm run publish; npm run subscribe -- 'release/<partner>/**'
```

### Next protocol candidates

| Priority | Protocol | Use | Gate before implementation |
|---|---|---|---|
| High | ONVIF Profile M | Camera analytics objects, metadata, geolocation, and events | Device profile, discovery/auth method, sample metadata stream. [ONVIF Profile M](https://www.onvif.org/profiles/profile-m/) |
| Medium | VITA 49.2 | Raw RF/spectrum observations | DSP/geolocation stage that converts samples into map-ready bearings/positions. [VITA Radio Transport](https://www.vita.com/page-1855484) |
| Medium | STANAG 4607 / 4676 | GMTI and NATO track exchange | Licensed ICD/profile and representative messages; do not infer layouts |
| Vendor-specific | Acoustic/RF counter-UAS API | Bearings, classifications, tracks, sensor health | Vendor ICD/API schema, coordinate frame, time base, lifecycle, and authentication |

SAPIENT is the preferred public, vendor-neutral counter-UAS sensor interface:
the MOD-owned architecture is standardized as BSI FLEX 335 and publishes its
protobuf schemas. See the [official SAPIENT guidance](https://www.gov.uk/guidance/sapient-autonomous-sensor-system)
and [Dstl schemas](https://github.com/dstl/SAPIENT-Proto-Files). Its TCP
framing is the four-byte little-endian protobuf length used by the
[official BSI FLEX 335 v2 test harness](https://github.com/dstl/BSI-Flex-335-v2-Test-Harness/blob/main/SAPIENTMessageProcessor/ByteDataMessageBuilder.cs).

### Hackathon partner intake checklist

Before connecting a feed, obtain:

- protocol, category, edition/profile, transport, and stream framing;
- producer IP/port or URL/broker plus who initiates the connection;
- authentication/TLS method without committing any credentials;
- representative messages or a sanitized PCAP covering create/update/delete;
- coordinate reference, origin/datum, altitude reference, angles, and units;
- timestamps/time zone, update rate, stable identifiers, and stale/delete rules;
- classification/affiliation semantics and confidence scale;
- expected maximum message size, object count, and rate.

If any of category/edition, framing, or coordinate reference is unknown, the
translator must reject or quarantine the feed rather than silently guess.

---

## 8. C2 ↔ Zenoh bidirectional runbook

The directions are independent. Complete only the paths exposed and licensed
by the actual deployment, then select their services in `./start.sh`.

### 8.1 Verify the common Zenoh side

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

### 8.2 Zenoh → TAK Server

Configure the TAK TCP destination and select `tak-layer`:

```dotenv
TAK_HOST=<tak-server>
TAK_PORT=8089
TAK_TLS=1
TAK_TLS_SERVER_NAME=<dns-san-in-tak-server-certificate>
TAK_CERT=/runtime/path/tak-client.pem
TAK_KEY=/runtime/path/tak-client-key.pem
TAK_CA=/runtime/path/tak-ca.pem
```

These must be TAK-issued credentials. The Zenoh certificate is not valid for
TAK Server. `TAK_HOST` is the stable dial hostname; when the installed TAK
server certificate uses a different legacy DNS SAN, set
`TAK_TLS_SERVER_NAME` to that SAN instead of disabling hostname verification.
For lab plaintext TCP use the deployment's configured TCP port and
leave `TAK_TLS=0`. `tak-layer` egress is one-way; enable `tak-bridge` for a
return feed.

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
   paths above, select `tak-layer` in `./start.sh`, and confirm the identity appears
   as connected in TAK Server.

### 8.3 TAK Server → Zenoh

Use the same TAK-issued client identity for the reverse CoT feed, typically the
dedicated `efdi-bridge` account/certificate. Select `tak-bridge` and point it
at the TAK Server CoT endpoint:

```dotenv
TAK_HOST=<tak-server>
TAK_PORT=8089
TAK_TLS=1
TAK_TLS_SERVER_NAME=<dns-san-in-tak-server-certificate>
TAK_CERT=/runtime/path/efdi-bridge.pem
TAK_KEY=/runtime/path/efdi-bridge-key.pem
TAK_CA=/runtime/path/tak-ca.pem
```

The bridge uses the same TAK session model as a normal client: if the server
authorizes the identity for both directions, it can publish into TAK and
subscribe to server-visible CoT at the same time. The bridge republishes the
received `<event>...</event>` frames into Zenoh and marks them as TAK ingress so
the outbound CoT layer does not loop them straight back into the server.

### 8.4 Zenoh → SitaWare HQ

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

### 8.5 SitaWare HQ → Zenoh

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

### 8.6 Share C2-origin data with partners

Do not rewrite the record into another partner's namespace. Confirm that the
origin namespace is permitted by the router/federation policy and that the
receiving partner subscribes to it. Their `cot-*` or `sitaware-hq-nvg` output
layers will translate authorized normalized topics in the same way as locally
generated sensor data.

### 8.7 Operational-persona test exercise

Use four separate identities or clients in a test. These are operational
personas, not replacements for the Zenoh Admin panel's `superadmin`, `admin`,
and `readonly` roles.

| Persona | Test client and action | EFDI services | Expected result |
| --- | --- | --- | --- |
| C2 operator | A TAK/WinTAK/ATAK or SitaWare HQ operator account observes the configured CoT output. | `tak-layer` and/or `sitaware-hq-nvg`. | Normalized EFDI tracks appear in the authorized C2 system. |
| Sensor publisher | A receiver/detection system attached to a local Zenoh router publishes complete frames/documents to that protocol's `…/raw/<protocol>/<source-id>` topic. For a lab publisher, an admin can generate a script in **Publish Script** after entering that publisher's current router endpoint. | The matching protocol translator and desired C2 output layers. | The translator creates normalized EFDI tracks; the C2 systems show derived markers, not the raw frame. |
| Fabric admin | A separate Zenoh Admin panel account manages router/federation configuration only. | Infrastructure/admin UI; no sensor or C2 feed is required. | May perform its assigned panel actions but is not an operational TAK/SitaWare identity. |

For a first exercise, use a dedicated TAK-issued service identity for `tak-layer`
and confirm the authorized C2 system receives normalized EFDI tracks. Keep raw
sensor publication on a distinct sensor identity/topic; it must not impersonate
an operator identity.

The current router ACL is namespace-scoped, not yet persona/certificate-scoped.
The four test clients prove data flow and C2 behaviour; they do **not** prove
least-privilege Zenoh authorization between personas. Enforced persona access
needs a subsequent certificate-subject ACL design with separate client
credentials and topic permissions.

> **ASTERIX editions:** the implemented standard UAPs are CAT-010 1.1,
> CAT-020 1.11, CAT-021 2.7, CAT-034 1.29, CAT-048 1.32, and CAT-062 1.21.
> Confirm the producer edition before connecting it; a different or
> vendor-specific UAP needs an explicit decoder profile.

### Zenoh topic schema

```text
{NAMESPACE}/{DOMAIN}/{SOURCE}/{MODALITY}/{AFFILIATION}/{ENTITY}/{TYPE}/{ID}/{VIEW}
```

| Field | Values |
| --- | --- |
| `DOMAIN` | `air`, `land`, `sea`, `space`, `env` |
| `AFFILIATION` | `friendly`, `hostile`, `neutral`, `unknown`, `civ`, `mil` |
| `TYPE` | `aircraft`, `vessel`, `vehicle`, `unit`, `sensor`, `uav`, `radar` |

---

## 9. Adding a New Sensor or Protocol

This is the step-by-step path from "I have a new sensor/feed" to "it shows up
in TAK and SitaWare automatically." It assumes the pod is already installed
and running (§§1-6 above).

Read [§7 Integrations](#7-integrations) first if you haven't — it explains
the fabric this walkthrough plugs into (the topic taxonomy, the four output
views, what's already wired). This section is the concrete "now build one"
steps; that one is the reference for what already exists.

### 9.0 Decide: bridge, or protocol?

- **`compose/bridges/`** — your new integration *connects to a product or
  service*: it polls an HTTP API, opens a TCP socket to a vendor box, listens
  on a UDP port for a specific device. One file per external thing it talks to.
- **`compose/protocols/`** — your new integration *decodes an already-defined
  wire format* that isn't tied to one vendor (a standard, a spec, a schema).

Most new sensors are bridges — a physical or networked device this router
connects to directly. If in doubt, pick `bridges/`; it's the more common case
and nothing downstream cares which directory a script lives in.

### 9.1 Do you need a new message schema, or does an existing one fit?

If your sensor reports a moving object — position, optionally speed/heading/
altitude/identity — it almost certainly fits the existing generic
`NormalizedTrack` schema (`../compose/protocols/proto/normalized_track.proto`)
and you need **no new protobuf work at all**. Skip to step 2.

Only define a new `.proto` message if your data has structured fields
`NormalizedTrack` genuinely can't express (e.g. a multi-point area/zone, or
a domain-specific compound value). If so:

1. Add a new `.proto` file under `compose/protocols/proto/` — every
   EFDI-authored schema lives there, regardless of which translator owns it
   (an actual vendored/licensed third-party schema, like the SAPIENT or
   Sparkplug B wire contracts, is the one exception and stays under
   `compose/protocols/vendors/<name>/` next to its own LICENSE) —
   modeled on an existing one — `geojson_features.proto` is a short example.
2. Regenerate the Python bindings: `scripts/generate-protobuf.sh` (needs
   `grpc_tools.protoc` + `protobuf` — already in `compose/requirements.txt`).
   This writes into `compose/generated/`, which is gitignored — every
   developer/deployment regenerates it locally, nothing generated is
   committed.

### 9.2 Write the script

Every bridge/protocol script follows the same shape. This is the complete,
working reference — `compose/protocols/random/geojson_features.py` (127
lines) — trimmed to the parts that matter:

```python
from namespace_prefix import topic_root
from gateway import open_session, publish_dual
# Reuse the generic schema — no new .proto needed for a plain moving object:
from protocols.proto.normalized_track_pb2 import NormalizedTrack

TOPIC_ROOT = topic_root()
OUTPUT_TOPIC = TOPIC_ROOT + "/<domain>/<your-source-name>/<modality>/<affiliation>/<entity>"

def normalize(raw: dict) -> dict | None:
    """Turn one of your sensor's records into the shared track shape.
    Required: _ts (epoch seconds), _src (your source name), uid (stable per-object id).
    Everything else is optional — only set what you actually have."""
    return {
        "_ts": time.time(),
        "_src": "your-sensor-name",
        "uid": "YOURSENSOR-" + raw["id"],
        "lat_deg": raw["lat"],
        "lon_deg": raw["lon"],
        # optional: "speed_ms", "heading_deg", "baro_alt_m", "callsign", ...
    }

def run() -> None:
    session = open_session()
    for raw in your_data_source():          # poll an API, read a socket, etc.
        record = normalize(raw)
        if record:
            publish_dual(session, OUTPUT_TOPIC, record, NormalizedTrack)
```

Your script never imports `zenoh` itself — `gateway.py` is the only module that
does. If you need to subscribe to a raw input topic instead of polling, use
`gateway.subscribe(session, topic, callback)` the same way.

`publish_dual` does the rest: it publishes all four fabric views (`/sapient`,
`/json`, `/proto`, `/raw`) from that one call — see
[§7 Integrations → "Egress topic views"](#egress-topic-views-sapient-json-proto-raw)
for what each view is for. You never publish to TAK or SitaWare directly —
`tak_layer`/`sitaware_layer` subscribe to every normalized topic on the fabric
automatically, so a correctly-published track appears in both without any
further code.

**Topic path.** Follow the taxonomy from §7 Integrations:
`{domain}/{source}/{modality}/{affiliation}/{entity}` — e.g. `land` (or `air`/
`sea`), your sensor's short name, what kind of sensing it is, `neutral` if you
don't have real affiliation data, and what the object is (`vehicle`,
`vessel`, `unit`, ...). Look at a few existing topics
(`docs/topic-taxonomy.md`) for the pattern before inventing a new shape.

**Configuration — nothing hardcoded.** Any host, port, URL, or credential your
script needs comes from an environment variable, never a literal in the code
(`compose/bridges/sitaware_bridge.py` is a good example of an all-env-driven
bridge). Add each new variable to `compose/.env.example` with a one-line
comment explaining what it's for — that file is the single source of truth
for what a deployment can configure, and it's what the next administrator
reads to know what to fill in.

**Verify it compiles:**
```bash
python3 -m py_compile compose/bridges/your_new_bridge.py
```

### 9.3 Register it with the launcher

Four small edits to `start.sh`, following the existing `geojson` entry as the
template (search for `geojson` in `start.sh` to see all four at once):

1. **`SERVICES` array** — add your service's short name to the list.
2. **`SVC_CAT`** — which category it shows under in the menu/WebUI
   (`"Sensor bridges"`, `"Protocols"`, etc.).
3. **`SVC_DESC`** — a one-line human description.
4. **`svc_ready()`** — when is it safe/meaningful to start? If it needs no
   configuration to be useful, add your name to a `return 0` case alongside
   `cap`/`geojson`/etc. If it needs an env var set first (a URL, a host), gate
   on that instead — e.g. `admin-control`'s gate checks a secret key is set;
   yours might check `[[ -n "${YOUR_SENSOR_URL:-}" ]]`.
5. **Launch case** — add `_start your-service-name path/to/your_script.py` in
   the big `case` block that actually launches services.

### 9.4 Verify end-to-end

```bash
./start.sh --service your-service-name
```
Then confirm data is actually flowing — subscribe to your topic with any
Zenoh client (the repo's `clients/examples/` has ready-made subscribe
scripts) and confirm records arrive. If TAK or SitaWare output is enabled,
open ATAK/WinTAK or the SitaWare map and confirm your object appears with no
further configuration — that's the proof the fabric contract was followed
correctly.

### 9.5 New CoT symbol needed? (TAK output only)

If your sensor's affiliation/entity combination doesn't already map to a CoT
type, add it to `_TOPIC_COT` in `compose/layers/tak_layer.py`:
```python
"air/**/hostile/uav/**":      ("a-h-A-M-F-Q", AIR_STALE_S),
"land/**/neutral/sensor/**":  ("a-n-G-E-S",   LAND_STALE_S * 2),
```
The key is a topic-suffix glob; the value is the MIL-STD-2525C/APP-6 CoT type
code and a staleness window. Most new sensors already match an existing
pattern — only add one if your topic path genuinely doesn't.

### 9.6 Document it

Add a row to the relevant table in §7 Integrations (under
"Source-specific bridges" or the protocol table) describing what it needs
configured. This is what makes the *next* administrator's job the same
one-read, no-guessing experience this doc gave you.

### Checklist before you call it done

- [ ] No literal host/port/URL/credential in the script — everything is an
      env var, documented in `compose/.env.example`.
- [ ] `python3 -m py_compile` passes.
- [ ] Registered in all four `start.sh` places (`SERVICES`, `SVC_CAT`,
      `SVC_DESC`, `svc_ready`) plus the launch case.
- [ ] Confirmed the topic on the fabric, then confirmed it in TAK/SitaWare
      with no code changes to either.
- [ ] A row added to §7 Integrations.

---

## 10. Operations

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

## 11. Troubleshooting

### 11.1 Symptom-first fixes

Symptom-first fixes for the most common deployment problems. For
infrastructure-level lessons learned (DNS, TLS profiles, atomic writes —
things that don't fit a single symptom), see §11.2 Gotchas below.

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
# 1. Confirm tak-layer is running
kill -0 $(cat $POD_STATE_DIR/.pids/tak-layer.pid) && echo running

# 2. Confirm the TAK Server connection is established
ss -tn "( dport = :$TAK_PORT )"

# 3. Confirm TAK_HOST/TAK_PORT/TAK_TLS in .env match the TAK Server's actual endpoint
```

### CAT-34 radar marker is missing

The radar has not transmitted CAT-34 I034/120 (3D-Position), so EFDI cannot
place the site safely. Check the CAT-34 log for `has no site position`. Prefer
enabling I034/120 on the radar/gateway; for a single radar only, set fallback
coordinates in `.env`:

```bash
grep CAT34_RADAR compose/.env
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

SitaWare units without a valid 15-character SIDC are routed to `…/land/sitaware/c2/unknown/unit/…` and rendered as unknown ground units (`a-u-G-U-C`). Check the raw SIDC value in the log:

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
pkill -f "_bridge\.py\|tak_layer\|track_fusion"
rm -f $POD_STATE_DIR/.pids/*.pid
./start.sh
```

### Radar icon disappearing from ATAK

The `asterix` bridge publishes a keepalive every 60 s regardless of track activity. If the icon disappears, the bridge has stopped:

```bash
tail -20 $POD_STATE_DIR/logs/asterix.log | grep -E "keepalive|startup|error"
```

### 11.2 Gotchas

This is the *operational/infrastructure* companion to
[`../.ai/.claude/CLAUDE.md`](../.ai/.claude/CLAUDE.md)'s ASTERIX bit-level decode gotchas, and to
§11.1 Troubleshooting's symptom-first fixes. Everything
here was a real, confirmed issue hit while running this pod — read it before
debugging something that looks like one of these symptoms, so the same
diagnosis doesn't have to be re-earned.

### NetBird split-DNS is invisible inside containers

**Symptom:** A router config points at a mesh hostname (e.g.
`zenoh2.efdi.ltu`); the container never even attempts a connection — no
socket, no TLS error, just silence.

**Cause:** `network_mode: host` shares the network *namespace*, not
`/etc/resolv.conf`. NetBird's split-DNS resolver for its mesh domain runs on
the **host** only; a container still gets Docker's own generated resolver
(typically your LAN's DNS), which has never heard of the mesh domain. The
hostname resolves on the host (`getent hosts` works fine there) and silently
fails to resolve in the container — which looks identical to "nothing is
trying to connect."

**Fix:** Add explicit `extra_hosts` entries mapping each mesh hostname to its
current NetBird IP in the container's compose service. Domain names stay in
the application config; only the container's local hosts-resolution needs
the mapping. Re-add/update these if NetBird ever reassigns the IPs.

### A TLS/mTLS identity profile must match the endpoint it dials

**Symptom:** A fabric connection attempt produces no error and no link —
looks identical to the DNS issue above, or to a firewall block.

**Cause:** Each remote fabric (backbone, a partner's sandbox, this pod's own
local mesh) is signed by a **different CA**. Pointing the right endpoint at
the wrong certificate identity fails the mTLS handshake, and depending on the
failure mode this can look like nothing happened at all rather than a clear
rejection.

**Fix:** Endpoint and TLS identity profile are one atomic choice, never
adjusted independently. If your tooling offers presets, bundle the endpoint
and the matching certificate profile into a single preset rather than two
separate fields a person can mismatch.

### A bind-mounted single file breaks atomic writes

**Symptom:** A config-apply endpoint that writes a small state file (e.g. a
namespace-prefix file) fails with `OSError: [Errno 16] Device or resource
busy`, even though writing the main config file right next to it works fine.

**Cause:** The standard "atomic write" pattern is write-to-temp-file then
`os.replace(temp, target)` — the rename is what guarantees a reader never
sees a half-written file. That rename fails when `target` is itself a
single-file Docker bind mount (`-v host/file:/container/file`): the path *is*
a mount point, and you cannot rename over a mount point. A directory-mounted
file doesn't have this problem, because the rename happens inside the
mounted directory, not over the mount itself.

**Fix:** Fall back to an in-place rewrite (open-write-fsync, no rename) when
`os.replace` fails with `EBUSY`. It's not atomic, but it's the only option
for a bind-mounted single file, and it beats failing the whole apply for an
unrelated file.

### Identically-named duplicate function definitions silently shadow

**Symptom:** A decoder/handler looks obviously wrong when you read it (wrong
field width, wrong scale, a bug that should be very visible in output) — but
production data coming out the other end looks fine.

**Cause:** Python allows redefining a function at module scope with no
warning. If a file has `def handler(...)` twice, the **second** definition
silently wins — the first becomes 100% dead code that still *looks* live
(same indentation, no guard, often even both correctly documented). No
linter in this repo's toolchain flags this by default. This is exactly how a
genuinely broken code path can sit in a file for a long time without ever
affecting anything, and it can cost real debugging time when the "obviously
buggy" copy is the one a human reads first.

**Fix:** Before trusting that a function you're reading is the one that
actually runs, confirm at runtime: `inspect.getsourcelines(module.the_func)`
tells you which definition's line number is actually bound. If a repo has
grown organically (categories/variants added over time, each with "their
own" copy of similar logic), grep for the function name across the whole
file — not just the one you found first — whenever something doesn't add up.

### A service bundle needs its own status aggregation

**Symptom:** A WebUI or status endpoint shows a multi-process bundle
(several children under one logical "service") as permanently stopped, even
though every child process is actually running.

**Cause:** Generic per-service status logic that checks for one pidfile named
after the service will never find it if the bundle launcher writes one
pidfile *per child* instead (e.g. `asterix-cat10.pid`, `asterix-cat48.pid`,
...). The bundle itself has no pidfile, so it always reads "stopped."

**Fix:** A bundle service needs bespoke status logic that enumerates and
aggregates its children's pidfiles, reporting running/degraded/stopped based
on how many are alive — not a naive single-pidfile check.

### A code fix isn't live until the running process restarts

**Symptom:** You fix a bug (in a decoder, in an admin API, anywhere), confirm
the file changed on disk, and the running system's behavior doesn't change —
or a WebUI keeps listing services/data that were just removed from the code.

**Cause:** Editing a `.py` file has zero effect on an already-running
interpreter holding the old bytecode in memory. This sounds obvious stated
plainly, but it's an easy thing to forget mid-investigation when several
files are being edited in sequence and it's not obvious *which* running
process is stale.

**Fix:** After any fix to a long-running service's code, restart that
specific process (not just recompile/test it) before concluding the fix
didn't work, and before reporting a symptom as still-unresolved.

---

## 12. Zenoh Admin GUI

A web GUI for operating the pod without SSH — status, starting/stopping
bridges and layers, configuration, the certificate authority, and branding.
Moved to its own document: **[ZENOH_ADMIN.md](ZENOH_ADMIN.md)**.

---

## 13. Continuous Integration

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
| 2026-07-05 | Removed the `gps-ew` bridge (GPSJam-based) — gpsjam.org has no public API for its own processed data, so this bridge never actually worked; removed from `start.sh` and `tak_layer.py` rather than left silently broken |
| 2026-07-05 | Fixed cross-source/cross-pod duplicate tracks in SitaWare: `nato_sitaware_layer.py`'s `_uid()` baked the source name into the track ID (unlike `tak_layer.py`'s already-correct version), so the same aircraft from two sources got two different SitaWare tracks |
| 2026-07-05 | `dronuradaras_bridge.py` was changed to publish every positioned registered sensor; superseded by the 2026-07-15 online-only operator policy below |
| 2026-07-05 | Added `.github/workflows/ci.yml`: compile-checks bridges/layers, type-checks + builds the zenoh-admin frontend, builds both Docker images on every push/PR |
| 2026-07-05 | Added `shellcheck` and `compose-validate` CI jobs; fixed the one real finding (`compose/rebuild.sh` missing `cd ... \|\| exit`) and silenced a false-positive (`SC2163` on the intentional "export by dynamic name" idiom in `start.sh`/`stop.sh`/`run.sh`) |
| 2026-07-10 | Fixed `nato_sitaware_layer.py` reusing the inbound `sitaware_bridge.py`'s env var names (`SITAWARE_URL`/`USER`/`PASS`) — renamed to `SITAWARE_NVG_*` since HQ (inbound) and Edge (outbound) are usually separate hosts/credentials |
| 2026-07-10 | Wired `nffi` into `start.sh` — it existed in the repo but was never registered as a launchable service |
| 2026-07-10 | Zenoh admin GUI: added a "Connected routers" panel — parses `router/transport/unicast/*` entries already present in the admin-space query used for the subscriber/queryable lists, no new ACL or query needed |
| 2026-07-10 | Zenoh admin GUI: ported the TAK-hud visual language (`hud-card`, `hud-frame`/reticle corners, `hud-glass` sidebar, `hud-grid-bg` backdrop, accent-glow buttons, staggered fade-in) into `index.css`/`Layout.tsx`/dashboard |
| 2026-07-11 | Zenoh admin GUI: full TAK port (not just style) — runtime branding via DB-backed store, theme toggle, notifications bell, username-change, all routes retrofitted with light/dark variants |
| 2026-07-11 | Zenoh admin panel HTTPS: uvicorn now binds `127.0.0.1:8895` only; new `zenoh-admin-proxy` (Caddy) terminates real TLS on `:8890` via Caddy's internal CA, `on_demand` issuance (operators reach it by raw IP, no SNI) |
| 2026-07-11 | `BUNDLE_DIR`/`POD_STATE_DIR` defaults moved from `$HOME/goat-bundle`/`$HOME/goat-moon` to `compose/certs/`/`compose/state/` (in-repo, gitignored) — scattered state across `$HOME` made cleanup unreliable |
| 2026-07-11 | Added `dev.sh`: disposable local MariaDB + directly-run uvicorn for zenoh-admin UI preview only, bypassing zenoh-router/certs/fabric entirely |
| 2026-07-11 | Removed the external "goat" vendor entirely: certs are now self-issued via `scripts/gen-certs.sh` (EFDI root CA, no portal/CBOR bundle), containers renamed `goat-moon-*` → `efdi-pod-*`, `GOAT_CERT_DIR` env var renamed `EFDI_CERT_DIR`, `../examples/first-boot.sh` rewritten to read `compose/.env` directly and drop the `goat-clientd` wrapper (NetBird is called natively — it was always EFDI's own asset, not vendor lock-in), `profiles/` directory removed (orphaned by the rewrite) |
| 2026-07-15 | `dronuradaras_bridge.py` now publishes only devices explicitly reported as `is_online=true`; offline devices emit deletion events so CoT, SitaWare Edge, and the HQ NVG snapshot evict cached markers |
| 2026-07-17 | Added deterministic ASTERIX category listener conventions: CAT-010/020/021/034/048/062 use UDP 50010/50020/50021/50034/50048/50062 by default; these are EFDI conventions, not vendor defaults |
| 2026-07-17 | Added Zenoh-native CAP, GeoJSON/OGC, spectrum, sensor-health, mission-route, and raw-ingress translation paths |
| 2026-07-17 | Security refresh: Vite upgraded, Compose images pinned/refreshed, Python image OS packages upgraded, and authenticated SitaWare/UTM endpoints restricted to HTTPS |
| 2026-07-18 | Added TAK-style Runtime Control for native bridge/protocol/layer lifecycle, bounded logs, endpoint/topic/port editing, write-only credentials, a localhost admin-control agent, and a live Vite dev stack with aligned API/proxy ports |
| 2026-08-02 | Merged `HOST_SETUP.md`, `INTEGRATIONS.md`, `C2_RUNBOOK.md`, `ADDING_A_SENSOR.md`, `TROUBLESHOOTING.md`, and `GOTCHAS.md` into this document (§§1, 7-9, 11) — one deployment guide instead of eight; `ZENOH_ADMIN.md` stays separate |
| 2026-08-02 | Added BDS 1,0/1,7 (Data Link Capability / Common Usage GICB Capability) decoding to the 7 ASTERIX categories that already reuse BDS 3,0/4,0/5,0/6,0 GICB-extraction helpers (CAT-010/011/018/020/021/048/062), sourced from pyModeS |
| 2026-08-02 | Renamed `layers/cot_layer.py` → `layers/tak_layer.py` and `layers/nvg_layer.py` → `layers/sitaware_layer.py` (vendor-named egress, matching `tak_bridge.py`/`sitaware_bridge.py`'s ingress naming); removed the unused `cot-udp`/`cot-udp-tak` UDP multicast/unicast launcher entries and the `nvg_bridge.py` NVG-XML ingress bridge (SitaWare ingress is REST-only now) |
| 2026-08-02 | Consolidated every EFDI-authored `.proto` schema under `compose/protocols/proto/` (was split across `compose/protocols/random/`, `compose/protocols/vendors/proto/`, and `compose/protocols/vendors/sparkplug/`); vendored third-party schemas (SAPIENT `sapient_msg/`, Sparkplug B) stay under their own `vendors/<name>/` directory |

---

*Internal use only — do not distribute outside the project.*
