# 03 — Bootstrap & Install

## Prerequisites

### Bare host bootstrap

On Debian 13 or RHEL/Rocky/AlmaLinux 9/10, the only thing you install by
hand is `curl` (usually already present) — everything else is one command:
```bash
curl -fsSL https://raw.githubusercontent.com/lk-risb/EFDI/main/install.sh | bash
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
for it and joins during the [Installation](#installation) section below (this is exactly what its
automated Networking step does). Verify only that the binary installed:
```bash
netbird version
tailscale version
```

#### Open firewall ports

`./install.sh` does **not** do this step for you — which ports to open
depends on which sensor bridges you enable, and that's a post-install,
WebUI-driven choice (see [Configuration](04-configuration.md)), not something the installer
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

If every command above succeeds, continue to the [Installation](#installation) section below (the repository
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
| HTTPS 8890 | inbound | Zenoh admin GUI (Caddy-terminated, internal CA — see [Operations](05-launching-and-operations.md)) |
| HTTPS | outbound | dronuradaras.lt APIs |

ATAK/WinTAK clients receive tracks only through a TAK Server (`tak-layer` service); there is no direct multicast/unicast CoT delivery path.

### Certificates

For a standalone/development pod, Zenoh mTLS certs can be self-issued without
an external vendor bundle. `scripts/gen-certs.sh <namespace>` generates (once)
an EFDI development root CA under `compose/certs/efdi/`, then signs a leaf
cert+key for the given namespace. Do not distribute that development root key
to managed routers.

The generated material (`efdi-ca-root.pem`, `<NAMESPACE>-cert.pem`, `<NAMESPACE>-key.pem`) lives at `compose/certs/efdi/` — gitignored, never committed. The ignored bundle directory also keeps `tak/`, `sitaware/`, `efdi-backbone/` (goat backbone, Desert Bread CA), and `efdi-ltu/` (LTU sandbox) identities separate — see the certificate profile legend in [§3.2 below](#32-generate-certificates). Default path is set by `start.sh`; override with `BUNDLE_DIR` in `compose/.env` if you'd rather keep it outside the repo entirely. Managed deployments use the delegated-CA workflow in the [Operations](05-launching-and-operations.md) section and keep CA private keys under a separate mode-700 runtime directory.

---

## Installation

### 3.1 Clone the repository

On a fresh host with nothing installed yet, one command clones the repo and
runs the installer, which auto-installs every prerequisite from the [Prerequisites](#prerequisites) section itself:

```bash
curl -fsSL https://raw.githubusercontent.com/lk-risb/EFDI/main/install.sh | bash
```

Equivalent to cloning first, then running the same script locally:

```bash
git clone <repo-url> EFDI
cd EFDI
./install.sh
```

### 3.1a Choose Production or Testing mode

`install.sh` asks this early, before certificates:

```text
Production  — requires certs from scripts/gen-certs.sh (mTLS, fabric connectivity)
Testing     — generates self-signed certs, local Zenoh only (no fabric)
```

**Testing mode** is for trying EFDI out on one box with no real fabric
connection: it auto-generates a namespace and self-signed certs, and Zenoh
runs over plain TCP with no mTLS. It also changes two paths you'll need when
troubleshooting:

```text
BUNDLE_DIR     = <repo>/compose/test-certs   (not compose/certs/)
POD_STATE_DIR  = <repo>/.test-pod-state      (a sibling of compose/, not inside it)
```

If you're looking for logs, PID files, or the rendered Zenoh config on a
testing-mode install, check `<repo>/.test-pod-state/` (e.g.
`.test-pod-state/logs/sitaware_layer.log`), **not** `compose/state/` — the
default `compose/state/` path only applies to a production install where
`POD_STATE_DIR` was left unset. Confirm which one a given box actually uses
with `grep '^POD_STATE_DIR=' compose/.env`.

**Production mode** requires real certificates from `scripts/gen-certs.sh`
(or a partner-issued bundle) and enforces mTLS — see
[§3.2](#32-generate-certificates) below.

Both modes now always prompt for the Zenoh WebUI admin username and
password during install (this used to be skipped in testing mode with an
auto-generated password that got silently scrubbed from `.env` after first
login, with no other record of it anywhere — always setting it yourself
avoids that trap). `reinstall.sh` can also reset these credentials later if
you forget them, or use `health.sh`'s interactive troubleshooting menu — see
[Operations](05-launching-and-operations.md).

### 3.2 Generate certificates

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

### 3.3 Create the Python virtual environment

`start.sh` creates the venv automatically on first run. To create it manually:

```bash
python3 -m venv compose/venv
compose/venv/bin/pip install -r compose/requirements.txt
```

> The `eclipse-zenoh` version must be **exactly 1.9.0** — minor version mismatches introduce breaking API changes.

Always install into this venv, never the system `python3` directly. Running
`pip install` against the system interpreter on a modern Debian/Ubuntu fails
with `error: externally-managed-environment` (PEP 668) — that error is the
system protecting itself, not a bug to route around with
`--break-system-packages`. Every host-native service this pod runs (bridges,
layers, protocol translators) already expects `compose/venv`, so fixing the
error the "quick" way would leave those services running against a
different Python environment than the one you just modified.

`install.sh` also `chgrp`s/`chmod`s a handful of individually bind-mounted
state files and directories (`namespace-prefix`, `data-topic-prefix`,
`$BUNDLE_DIR/efdi`, `$POD_STATE_DIR/integrations/tak`) so the `zenoh-admin`
container — which always runs as a fixed non-root uid `10001` — can write to
them. If a *later* WebUI save (config, TAK/SitaWare credentials) fails with
`Permission denied` on one of these paths, `health.sh`'s interactive menu
(option 3) detects and fixes it automatically; see
[Troubleshooting](11-troubleshooting.md) for the manual `chgrp`/`chmod` if
you need it immediately.

### 3.4 Start the Zenoh router

```bash
docker compose -f compose/docker-compose.yml up -d zenoh-router
```

Verify the container is healthy before proceeding:

```bash
docker compose -f compose/docker-compose.yml ps zenoh-router
# "Status" column must read "healthy"
```

---
