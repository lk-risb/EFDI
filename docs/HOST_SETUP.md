# Host setup — from a bare Linux box to ready-for-EFDI

Read this **before** [`INSTALL.md`](INSTALL.md). It assumes nothing is
installed yet — only a fresh Linux server with root/sudo access and a network
connection. If Docker, Python, git, and NetBird are already installed and
working, skip straight to `INSTALL.md`.

## 1. Choose and size the host

| | Minimum | Recommended |
| --- | --- | --- |
| OS | Ubuntu 22.04/24.04 LTS, or RHEL 9 / Rocky Linux 9 / AlmaLinux 9 | Ubuntu 24.04 LTS |
| CPU | 2 vCPU | 4 vCPU |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB free | 40 GB+ free (more if you enable long-term track storage) |
| Network | One interface with outbound internet access | Static or DHCP-reserved address |

Any modern x86_64 or arm64 Linux distribution with a recent kernel and systemd
works; these two families are covered step-by-step below because they are the
most common in government/defense environments. If you use a different
distribution, translate the package-manager commands and the rest of this
guide applies unchanged.

Run every command below as a regular user with `sudo` access — not as `root`
directly, so the final "run Docker as a non-root user" step is meaningful.

## 2. Update the OS

**Ubuntu/Debian:**
```bash
sudo apt update && sudo apt upgrade -y
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf upgrade -y
```

Reboot if the kernel was updated (`sudo reboot`).

## 3. Install git and basic tools

**Ubuntu/Debian:**
```bash
sudo apt install -y git curl ca-certificates
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf install -y git curl ca-certificates
```

Verify: `git --version`

## 4. Install Python 3.10+

**Ubuntu 22.04** ships Python 3.10 by default; **Ubuntu 24.04** ships 3.12.
Verify what you have first — `python3 --version` — and only install if it's
older than 3.10:
```bash
sudo apt install -y python3 python3-venv python3-pip
```

**RHEL/Rocky/AlmaLinux 9** ship Python **3.9** by default, which is below
EFDI's minimum. Install 3.11 from the AppStream repository alongside it (this
does **not** replace the system `python3`, so nothing else on the box breaks):
```bash
sudo dnf install -y python3.11 python3.11-pip
```
Use `python3.11` explicitly wherever this repo's scripts say `python3` on a
RHEL-family host, or set up an alias/venv that points at it.

Verify: `python3 --version` (or `python3.11 --version` on RHEL-family) must
report **3.10 or newer**.

## 5. Install Docker Engine + the Compose plugin

Use the distribution's official Docker repository, not a distro-bundled
`docker.io`/`podman-docker` package — those are frequently out of date and can
be missing the Compose v2 plugin this repo depends on (`docker compose`, not
the old standalone `docker-compose`).

**Ubuntu/Debian:**
```bash
# Add Docker's official GPG key and repository
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update

# Install Docker Engine + Compose plugin
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

**RHEL/Rocky/AlmaLinux:**
```bash
sudo dnf -y install dnf-plugins-core
sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

### Run Docker as a non-root user

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

## 6. Install the NetBird client

EFDI pods reach the fabric and each other over a NetBird mesh VPN. Install the
client the same way on either distro (NetBird ships its own repository via
this script, so there's no separate apt/dnf setup):
```bash
curl -fsSL https://pkgs.netbird.io/install.sh | sh
```
Do **not** join a network yet — the setup key comes from whoever administers
your organization's NetBird account, and joining is covered in
[`INSTALL.md`](INSTALL.md). Verify only that the binary installed:
```bash
netbird version
```

## 7. Open firewall ports

Open only what this host needs inbound; everything else in the port table is
outbound and needs no firewall rule on this host (a receiving radar/sensor's
firewall is a different concern). The authoritative, current port list lives
in `INSTALL.md §1 Prerequisites → Network` — open the ones your deployment
actually uses (most pods do not run every sensor bridge).

**Ubuntu/Debian (ufw):**
```bash
sudo ufw allow 8890/tcp comment 'EFDI admin GUI'
sudo ufw allow 50048/udp comment 'EFDI CAT-048 example — adjust to your sensors'
# repeat for whichever UDP/TCP ports your integrations use, per INSTALL.md
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

## 8. You're ready

At this point you should be able to run, all without `sudo`:
```bash
git --version
python3 --version      # 3.10+
docker run hello-world
docker compose version
netbird version
```

If every command above succeeds, continue to [`INSTALL.md`](INSTALL.md) §2
(the repository clone and pod bootstrap). If anything failed, re-run the
matching step above before moving on — nothing later in the setup can fix a
missing dependency here.
