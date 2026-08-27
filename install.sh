#!/usr/bin/env bash
# install.sh — EFDI bridge stack installer
# Configures compose/.env, Python venv, and starts the Zenoh router.
# Run as the deployment user (not root) on a host that already has Docker.

set -euo pipefail

# $USER isn't always exported (minimal containers, some non-login shells) —
# with `set -u` that turns every usage below into a hard crash.
USER="${USER:-$(whoami)}"

REPO_URL="https://github.com/lk-risb/EFDI.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/efdi-router}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "$PWD")"
ENV_FILE="$SCRIPT_DIR/compose/.env"
COMPOSE_FILE="$SCRIPT_DIR/compose/docker-compose.yml"
VENV="$SCRIPT_DIR/compose/venv"

# ── OS package manager detection (Ubuntu/Debian apt, RHEL/Rocky/Alma dnf — the
# two families docs/03-bootstrap-and-install.md documents) — used throughout this script so a
# bare host with none of git/Python/Docker/openssl/gettext pre-installed can
# still complete `./install.sh` unattended, not just error out with a pointer
# to the manual doc. ─────────────────────────────────────────────────────────
PKG_MGR=""
if command -v apt-get >/dev/null 2>&1; then PKG_MGR="apt"
elif command -v dnf >/dev/null 2>&1; then PKG_MGR="dnf"
fi

# Docker publishes separate apt repos per distro (different signing metadata,
# different codename lists) — "apt" alone doesn't distinguish Debian from
# Ubuntu, so read the real distro ID for the one step that needs it.
DISTRO_ID=""
[ -f /etc/os-release ] && DISTRO_ID="$(. /etc/os-release && echo "$ID")"

# pkg_install <apt-package-list> <dnf-package-list> — either list may be empty
# when a package only exists under one distro family.
pkg_install() {
    local apt_pkgs="$1" dnf_pkgs="$2"
    case "$PKG_MGR" in
        apt) [ -n "$apt_pkgs" ] && sudo apt-get update -qq && sudo apt-get install -y -qq $apt_pkgs ;;
        dnf) [ -n "$dnf_pkgs" ] && sudo dnf install -y -q $dnf_pkgs ;;
        *) return 1 ;;
    esac
}

# Match TAK's curl-pipe bootstrap behavior: install into a normal git checkout,
# then re-exec the checked-in installer.
if [ ! -f "$COMPOSE_FILE" ]; then
    echo "Bootstrapping — cloning repo to $INSTALL_DIR ..."
    if ! command -v git >/dev/null 2>&1; then
        pkg_install git git
    fi
    command -v git >/dev/null 2>&1 || {
        echo "git is required to install EFDI and could not be auto-installed (no apt or dnf found)." >&2
        exit 1
    }
    if [ -d "$INSTALL_DIR/.git" ]; then
        git -C "$INSTALL_DIR" pull --ff-only
    else
        git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    git -C "$INSTALL_DIR" submodule update --init --recursive
    exec bash "$INSTALL_DIR/install.sh" </dev/tty
fi

# Safe here (never earlier): bash is now reading this script from a real file,
# not from the pipe `curl | bash` hands it — the branch above already re-execs
# with `</dev/tty` for that case. Reassigning stdin before this point would
# yank the script's own source out from under bash mid-read, breaking curl's
# write with an EPIPE (`curl: (23) Failure writing output to destination`).
[ -t 0 ] || exec < /dev/tty 2>/dev/null || true

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
ok()      { echo -e "${GREEN}[✓]${NC} $*"; }
info()    { echo -e "${CYAN}[*]${NC} $*"; }
warn()    { echo -e "${YELLOW}[!]${NC} $*"; }
err()     { echo -e "${RED}[✗]${NC} $*"; exit 1; }
section() { echo -e "\n${CYAN}── $* ──────────────────────────────────────────────────────${NC}"; }

# A failed `docker compose up` only reports orchestration events
# ("dependency ... is unhealthy"), never the failing container's own
# stderr/stdout. Call this right before err() so the real reason (bad
# config, missing cert, port conflict) is visible in the same run instead
# of needing a manual `docker compose logs` round trip.
dump_service_logs() {
    echo -e "\n${CYAN}[*]${NC} Service logs (diagnosing the failure above):\n"
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps -a 2>&1 || true
    echo ""
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" logs --no-log-prefix --tail=80 2>&1 || true
}

# shellcheck source=scripts/_ask.sh
. "$SCRIPT_DIR/scripts/_ask.sh"

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  EFDI — Bridge Stack Installer${NC}"
echo "  Sensor bridges: ASTERIX · SitaWare · dronuradaras"
echo ""

# ── Existing installation ────────────────────────────────────────────────────
if [ -f "$ENV_FILE" ]; then
    _EXISTING_POD_STATE_DIR="$(env_value POD_STATE_DIR)"
    if [ -n "$_EXISTING_POD_STATE_DIR" ] && [ ! -f "${_EXISTING_POD_STATE_DIR}/zenoh/config.json5" ]; then
        # Reinstall only removes containers/images — it never writes this
        # file, so offering it here is a guaranteed dead end (a prior
        # install attempt never got this far, e.g. was interrupted).
        # Skip straight to the flow that actually generates it instead of
        # asking a question whose "wrong" answer (Reinstall, also the
        # blank-Enter default) leads nowhere.
        warn "compose/.env exists but this deployment was never fully installed"
        warn "(no ${_EXISTING_POD_STATE_DIR}/zenoh/config.json5) — reconfiguring instead of offering Reinstall."
    else
        section "Existing installation"
        echo "  [R] Reinstall (remove local images/containers, keep certs and data)"
        echo "  [C] Full reconfigure"
        echo "  [Q] Cancel"
        read -rp "  Action [R/c/q]: " _EXISTING_ACTION
        case "${_EXISTING_ACTION:-R}" in
            [Rr]*) exec bash "$SCRIPT_DIR/reinstall.sh" ;;
            [Cc]*) ;;
            *) echo "Aborted."; exit 0 ;;
        esac
    fi
fi

# ── Install mode ──────────────────────────────────────────────────────────────
section "Install mode"
echo "  Production  — requires certs from scripts/gen-certs.sh (mTLS, fabric connectivity)"
echo "  Testing     — generates self-signed certs, local Zenoh only (no fabric)"
echo ""
while true; do
    read -rp "$(echo -e "  ${BOLD}Mode${NC} [P]roduction / [T]esting: ")" _MODE
    case "${_MODE:-}" in
        [Pp]*) INSTALL_MODE="production"; break ;;
        [Tt]*) INSTALL_MODE="testing";    break ;;
        *)     echo "    Enter P or T" ;;
    esac
done
echo ""
if [ "$INSTALL_MODE" = "testing" ]; then
    echo -e "  ${YELLOW}Testing mode${NC}: self-signed certs will be generated, Zenoh runs over plain TCP."
    echo "  Bridges connect locally — no EFDI fabric, no certs required."
else
    echo -e "  ${GREEN}Production mode${NC}: certs from scripts/gen-certs.sh required, mTLS enforced."
fi

# ── OS update ─────────────────────────────────────────────────────────────────
section "OS update"
case "$PKG_MGR" in
    apt) sudo apt-get update -qq && sudo apt-get upgrade -y -qq ;;
    dnf) sudo dnf upgrade -y -q ;;
    *) warn "No supported package manager (apt/dnf) found — skipping OS update." ;;
esac

REBOOT_NEEDED=0
[ -f /var/run/reboot-required ] && REBOOT_NEEDED=1
if [ "$PKG_MGR" = "dnf" ] && command -v needs-restarting &>/dev/null; then
    sudo needs-restarting -r &>/dev/null || REBOOT_NEEDED=1
fi
if (( REBOOT_NEEDED )); then
    ok "System updated."
    warn "A reboot is required (kernel or core library update) — reboot, then re-run ./install.sh to continue."
    exit 0
fi
ok "System up to date."

# ── Prerequisites ─────────────────────────────────────────────────────────────
section "Prerequisites"

# Sets PYTHON/PY_VER if a 3.10+ interpreter is found; returns 1 otherwise.
detect_python() {
    for py in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
        if command -v "$py" &>/dev/null; then
            PY_VER=$("$py" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || continue
            PY_MAJ=${PY_VER%%.*}; PY_MIN=${PY_VER#*.}
            if (( PY_MAJ > 3 || (PY_MAJ == 3 && PY_MIN >= 10) )); then
                PYTHON="$py"; return 0
            fi
        fi
    done
    return 1
}

if ! detect_python; then
    info "Python 3.10+ not found — installing…"
    # RHEL 9/Rocky/Alma ship Python 3.9 (too old); install 3.11 from AppStream
    # alongside it rather than replacing the system python3 (matches
    # docs/03-bootstrap-and-install.md's manual steps for this distro family).
    pkg_install "python3 python3-venv python3-pip" "python3.11 python3.11-pip" \
        || err "Python 3.10+ required and no supported package manager (apt/dnf) found — install it manually per docs/03-bootstrap-and-install.md and re-run."
    detect_python || err "Python 3.10+ still not found after installing — install it manually per docs/03-bootstrap-and-install.md and re-run."
fi
ok "Python ${PY_VER} ($PYTHON)"

# Debian/Ubuntu split venv+pip support out of the base python3 package —
# Debian 13 ships python3.13 by itself, so the branch above (which only
# installs python3-venv/python3-pip when Python is entirely missing) never
# runs on a host that already has Python. `python3 -m venv` then silently
# creates a venv missing pip (no error), which only surfaces later as
# "venv/bin/pip: No such file or directory". Ensure both unconditionally —
# idempotent, a no-op if already present.
if ! "$PYTHON" -m ensurepip --version &>/dev/null; then
    info "Installing venv/pip support for ${PYTHON}…"
    pkg_install "python3-venv python3-pip" "python3-pip" \
        || warn "Could not install python3-venv/python3-pip automatically — venv creation below may fail."
fi

DOCKER_JUST_INSTALLED=0
if ! command -v docker &>/dev/null; then
    info "Docker not found — installing from the official Docker repository (not distro-bundled docker.io)…"
    case "$PKG_MGR" in
        apt)
            # Docker publishes a separate apt repo per distro (Ubuntu vs
            # Debian) — default to the Debian repo, since Debian is this
            # project's primary target; only Ubuntu itself gets its own repo.
            _docker_apt_distro="debian"
            [ "$DISTRO_ID" = "ubuntu" ] && _docker_apt_distro="ubuntu"
            sudo install -m 0755 -d /etc/apt/keyrings
            sudo curl -fsSL "https://download.docker.com/linux/${_docker_apt_distro}/gpg" -o /etc/apt/keyrings/docker.asc
            sudo chmod a+r /etc/apt/keyrings/docker.asc
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${_docker_apt_distro} $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
                | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
            sudo apt-get update -qq
            sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            ;;
        dnf)
            sudo dnf -y -q install dnf-plugins-core
            sudo dnf config-manager --add-repo https://download.docker.com/linux/rhel/docker-ce.repo
            sudo dnf install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
            sudo systemctl enable --now docker
            ;;
        *) err "Docker not found and no supported package manager (apt/dnf) found — install it manually per docs/03-bootstrap-and-install.md and re-run." ;;
    esac
    command -v docker &>/dev/null || err "Docker installation failed — install it manually per docs/03-bootstrap-and-install.md and re-run."
    sudo groupadd docker 2>/dev/null || true
    sudo usermod -aG docker "$USER"
    DOCKER_JUST_INSTALLED=1
fi
ok "Docker $(docker --version | awk '{print $3}' | tr -d ,)"

COMPOSE_VER=$(docker compose version --short 2>/dev/null || echo "")
if [ -z "$COMPOSE_VER" ]; then
    info "Docker Compose v2 plugin not found — installing…"
    pkg_install "docker-compose-plugin" "docker-compose-plugin" \
        || err "Docker Compose v2 plugin not found and no supported package manager (apt/dnf) found — install it manually and re-run."
    COMPOSE_VER=$(docker compose version --short 2>/dev/null || echo "")
fi
[ -n "$COMPOSE_VER" ] || err "Docker Compose v2 plugin still not found after install attempt."
ok "Docker Compose $COMPOSE_VER"

if [ "$INSTALL_MODE" = "production" ] && ! command -v envsubst &>/dev/null; then
    info "envsubst not found — installing (gettext)…"
    pkg_install "gettext-base" "gettext" \
        || warn "Could not auto-install envsubst (no apt/dnf found) — install gettext manually before generating Zenoh config."
fi

if [ "$INSTALL_MODE" = "testing" ] && ! command -v openssl &>/dev/null; then
    info "openssl not found — installing…"
    pkg_install "openssl" "openssl" \
        || err "openssl is required to generate test certificates and could not be auto-installed — install it manually and re-run."
fi
if [ "$INSTALL_MODE" = "testing" ]; then
    ok "openssl $(openssl version | awk '{print $2}')"
fi

# The user was just added to the docker group — that membership only applies
# to new login sessions, not this one, so any docker command below would fail
# with a permission error. Rather than fight that with newgrp/sg tricks in a
# non-interactive script, stop here and ask for a fresh session, same as the
# manual steps in docs/03-bootstrap-and-install.md.
if (( DOCKER_JUST_INSTALLED )); then
    echo ""
    ok "Docker installed — added $USER to the docker group."
    warn "Log out and back in (or reboot), then re-run ./install.sh to continue."
    exit 0
fi

# ── Networking (NetBird / Tailscale mesh) ─────────────────────────────────────
# Production mode only — testing mode is explicitly local-only, no fabric.
if [ "$INSTALL_MODE" = "production" ]; then
    section "Networking"
    _NB_IP=$(ip addr show wt0 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -1) || true
    _TS_IP=$(ip addr show tailscale0 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -1) || true

    if [ -n "$_NB_IP" ] || [ -n "$_TS_IP" ]; then
        [ -n "$_NB_IP" ] && ok "NetBird connected ($_NB_IP)"
        [ -n "$_TS_IP" ] && ok "Tailscale connected ($_TS_IP)"
    else
        echo "  EFDI pods reach the fabric and each other over a mesh VPN."
        echo "  Neither NetBird nor Tailscale is connected on this host yet."
        echo ""
        while true; do
            read -rp "$(echo -e "  ${BOLD}Connect now?${NC} [N]etBird / [T]ailscale / [S]kip (manual/offline): ")" _VPN_ACTION
            case "${_VPN_ACTION:-}" in
                [Nn]*)
                    ask_key NETBIRD_SETUP_KEY "NetBird setup key (app.netbird.io → Keys)"
                    # Blank = NetBird's own default (NetBird Cloud, api.netbird.io).
                    # Only self-hosted management servers need this set.
                    ask_key NETBIRD_MGMT_URL "Self-hosted management URL (leave blank for NetBird Cloud)"
                    # Reaching this branch means neither wt0 nor tailscale0 had an
                    # IP (checked above), so any NetBird package already on the
                    # box is a stale/broken leftover, not a live tunnel — safe to
                    # purge before the vendor installer runs, which otherwise
                    # refuses with "NetBird seems to be installed already"
                    # (same reasoning TAK's install.sh uses for this step).
                    if command -v netbird >/dev/null 2>&1 || dpkg -l netbird 2>/dev/null | grep -q '^ii' || rpm -q netbird >/dev/null 2>&1; then
                        info "Removing stale NetBird install…"
                        sudo netbird down 2>/dev/null
                        case "$PKG_MGR" in
                            apt) sudo apt-get purge -y -qq netbird 2>/dev/null ;;
                            dnf) sudo dnf remove -y -q netbird 2>/dev/null ;;
                        esac
                        sudo rm -rf /etc/netbird /var/lib/netbird
                    fi
                    info "Installing NetBird…"
                    # Official vendor installer, fetched fresh over HTTPS — not
                    # checksum-pinned since it's a live, auto-updating script;
                    # the trust boundary is the TLS connection to NetBird's own domain.
                    curl -fsSL https://pkgs.netbird.io/install.sh | sh
                    info "Connecting to NetBird…"
                    _NB_UP_ARGS=(up --setup-key="$NETBIRD_SETUP_KEY")
                    [ -n "$NETBIRD_MGMT_URL" ] && _NB_UP_ARGS+=(--management-url="$NETBIRD_MGMT_URL")
                    sudo netbird "${_NB_UP_ARGS[@]}" \
                        || err "NetBird connection failed — check your setup key${NETBIRD_MGMT_URL:+ and management URL} and re-run."
                    sleep 3
                    _NB_IP=$(ip addr show wt0 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -1) || true
                    [ -n "$_NB_IP" ] && ok "NetBird connected ($_NB_IP)" \
                        || warn "Could not read wt0's IP after connecting — check 'netbird status'."
                    break
                    ;;
                [Tt]*)
                    ask_secret TAILSCALE_AUTH_KEY "Tailscale auth key (login.tailscale.com → Settings → Keys)"
                    info "Installing Tailscale…"
                    # Official vendor installer — same accepted trust model as above.
                    curl -fsSL https://tailscale.com/install.sh | sh
                    info "Connecting to Tailscale…"
                    sudo tailscale up --authkey="$TAILSCALE_AUTH_KEY" \
                        || err "Tailscale connection failed — check your auth key and re-run."
                    sleep 3
                    _TS_IP=$(ip addr show tailscale0 2>/dev/null | awk '/inet / {print $2}' | cut -d/ -f1 | head -1) || true
                    [ -n "$_TS_IP" ] && ok "Tailscale connected ($_TS_IP)" \
                        || warn "Could not read tailscale0's IP after connecting — check 'tailscale status'."
                    break
                    ;;
                [Ss]*)
                    warn "Skipping — this pod will only reach a local Zenoh router until connected manually."
                    break
                    ;;
                *) echo "    Enter N, T, or S" ;;
            esac
        done
    fi
fi

# ── EFDI certs / namespace ──────────────────────────────────────────────────────
# The pod always boots in plaintext/no-mTLS mode first (see the config.json5
# block below) so the Zenoh Admin WebUI comes up immediately with zero prior
# cert material. Real mTLS certs, PARTNER_NAMESPACE, and NAMESPACE_PREFIX are
# then set from the WebUI's Certificates page (uploads CA root/cert/key, which
# triggers a switch to mTLS and a router restart) — not typed at install time.
# A throwaway self-signed identity is generated below purely so the pod has
# *something* valid to boot with; it gets replaced entirely on first upload.
if [ "$INSTALL_MODE" = "production" ]; then
    section "Pod state directory"
    echo "  Zenoh router config and TLS material live here."
    echo "  Must be on the LUKS-encrypted volume in production."
    echo ""
    EXISTING_POD_STATE=""
    if [ -f "$ENV_FILE" ]; then
        EXISTING_POD_STATE=$(grep '^POD_STATE_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')
    fi
    ask POD_STATE_DIR "POD_STATE_DIR" "${EXISTING_POD_STATE:-$HOME/efdi-pod}"

    PARTNER_NAMESPACE=$(gen_uuid)
    NAMESPACE_PREFIX="EFDI"
    BUNDLE_DIR="${POD_STATE_DIR}/certs"
    ZENOH_LOCAL_ENDPOINT="tcp/127.0.0.1:7448"
    info "Generated placeholder PARTNER_NAMESPACE: $PARTNER_NAMESPACE"
    info "Replace certs and namespace from the WebUI's Certificates page after first boot."
else
    # Testing mode — generate everything automatically
    section "Test namespace and directories"
    PARTNER_NAMESPACE=$(gen_uuid)
    NAMESPACE_PREFIX="EFDI"
    BUNDLE_DIR="$SCRIPT_DIR/compose/test-certs"
    POD_STATE_DIR="$SCRIPT_DIR/.test-pod-state"
    ZENOH_LOCAL_ENDPOINT="tcp/127.0.0.1:7448"
    info "Generated PARTNER_NAMESPACE: $PARTNER_NAMESPACE"
    info "Test certs directory       : $BUNDLE_DIR"
    info "Pod state directory        : $POD_STATE_DIR"
fi

# ── Sensor bridges ─────────────────────────────────────────────────────────────
# Which sensors/C2 systems to wire up (ASTERIX radar, SitaWare, TAK Server —
# hostnames, ports, credentials) is deployment-specific detail most people
# installing for the first time don't have on hand yet, and re-typing every
# field at every re-run doesn't scale. Configure these after first boot from
# the Zenoh Admin GUI's Integration Settings page instead — same UI-first
# convention the rest of post-setup config already follows.
section "Sensor and C2 integrations"
info "Configure ASTERIX radar, SitaWare, and TAK Server connections after"
info "first boot from the Zenoh Admin GUI (Integration Settings)."

# ── Admin credentials and generated secrets ──────────────────────────────────
section "Zenoh WebUI administrator"
ZENOH_ADMIN_FIRST_USER="$(env_value ZENOH_ADMIN_FIRST_USER)"
ZENOH_ADMIN_FIRST_USER="${ZENOH_ADMIN_FIRST_USER:-admin}"
# Always ask, testing mode included — an auto-generated password here used to
# get silently scrubbed from .env the moment the first login created the
# account (scrub_admin_secret.sh), with no other record of it anywhere. A
# password you chose yourself is one you can still type after that happens.
ask ZENOH_ADMIN_FIRST_USER "Admin username" "$ZENOH_ADMIN_FIRST_USER"
while true; do
    ask_secret ZENOH_ADMIN_FIRST_PASS "Admin password (minimum 12 characters)"
    [ "${#ZENOH_ADMIN_FIRST_PASS}" -ge 12 ] && break
    warn "Minimum 12 characters required."
done
ZENOH_ADMIN_DB_USER="$(env_value ZENOH_ADMIN_DB_USER)"
ZENOH_ADMIN_DB_USER="${ZENOH_ADMIN_DB_USER:-zenoh_admin}"
ZENOH_ADMIN_DB_PASSWORD="$(env_value ZENOH_ADMIN_DB_PASSWORD)"
ZENOH_ADMIN_DB_PASSWORD="${ZENOH_ADMIN_DB_PASSWORD:-$(openssl rand -hex 24)}"
ZENOH_ADMIN_DB_ROOT_PASSWORD="$(env_value ZENOH_ADMIN_DB_ROOT_PASSWORD)"
ZENOH_ADMIN_DB_ROOT_PASSWORD="${ZENOH_ADMIN_DB_ROOT_PASSWORD:-$(openssl rand -hex 24)}"
ZENOH_ADMIN_SECRET_KEY="$(env_value ZENOH_ADMIN_SECRET_KEY)"
ZENOH_ADMIN_SECRET_KEY="${ZENOH_ADMIN_SECRET_KEY:-$(openssl rand -hex 32)}"
EFDI_CONTROL_TOKEN="$(env_value EFDI_CONTROL_TOKEN)"
EFDI_CONTROL_TOKEN="${EFDI_CONTROL_TOKEN:-$(openssl rand -hex 32)}"

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}── Summary ──────────────────────────────────────────────────────────${NC}"
echo "  Mode              : $INSTALL_MODE"
echo "  PARTNER_NAMESPACE : $PARTNER_NAMESPACE"
echo "  BUNDLE_DIR        : $BUNDLE_DIR"
echo "  POD_STATE_DIR     : $POD_STATE_DIR"
echo "  ZENOH_ENDPOINT    : $ZENOH_LOCAL_ENDPOINT"
[ -n "${CAT48_PORT:-}"    ] && echo "  CAT48_PORT        : $CAT48_PORT"
[ -n "${SITAWARE_URL:-}"  ] && echo "  SITAWARE_URL      : $SITAWARE_URL"
[ -n "${TAK_HOST:-}"      ] && echo "  TAK_HOST:PORT     : $TAK_HOST:$TAK_PORT"
echo ""
read -rp "$(echo -e "  ${BOLD}Proceed?${NC} [Y/n]: ")" _CONFIRM
[[ "${_CONFIRM:-Y}" =~ ^[Yy] ]] || { echo "Aborted."; exit 0; }

# ── Generate a throwaway self-signed identity ──────────────────────────────────
# Needed on every install (not just testing) so the pod has *some* valid cert
# material to boot with under the bootstrap config below, and so zenoh-admin's
# own BUNDLE_DIR/efdi/* bind mounts (docker-compose.yml) resolve to real files
# instead of Docker silently substituting empty directories. Production
# installs replace this entirely via the WebUI's Certificates page.
section "Generating self-signed bootstrap certificates"
mkdir -p "$BUNDLE_DIR/efdi"
# zenoh-admin bind-mounts this directory :rw (docker-compose.yml) so the
# Certificates page can write an uploaded/rotated identity itself
# (api/certs_bootstrap.py) — but the container always runs as the fixed
# non-root uid/gid 10001 (see compose/zenoh-admin/Dockerfile), while this
# directory is owned by the installing host user. Without group-write for
# that gid, every such write fails with EACCES ("Operation failed" in the UI,
# no detail, since Caddy has nothing useful to relay). Best-effort: a
# non-root installer may not be able to chgrp to an arbitrary gid.
chgrp 10001 "$BUNDLE_DIR/efdi" 2>/dev/null || true
chmod 775 "$BUNDLE_DIR/efdi" 2>/dev/null || true

# Same non-root-uid bind-mount issue as above, for the TAK Server client
# credential upload (api/tak_package.py, docker-compose.yml's
# integrations/tak:rw mount) — without this, uploading a CA root/cert/key
# from the Integration Settings page fails with an unhandled PermissionError
# (bare HTTP 500, no detail).
mkdir -p "$POD_STATE_DIR/integrations/tak"
chgrp 10001 "$POD_STATE_DIR/integrations/tak" 2>/dev/null || true
chmod 775 "$POD_STATE_DIR/integrations/tak" 2>/dev/null || true

CA_KEY="$BUNDLE_DIR/efdi/efdi-ca-root-key.pem"
CA_CERT="$BUNDLE_DIR/efdi/efdi-ca-root.pem"
NODE_KEY="$BUNDLE_DIR/efdi/${PARTNER_NAMESPACE}-key.pem"
NODE_CSR="$BUNDLE_DIR/.node.csr"
NODE_CERT="$BUNDLE_DIR/efdi/${PARTNER_NAMESPACE}-cert.pem"

info "Generating CA key and self-signed certificate…"
openssl genrsa -out "$CA_KEY" 2048 2>/dev/null
openssl req -new -x509 -key "$CA_KEY" -out "$CA_CERT" -days 3650 -nodes \
    -subj "/CN=efdi-bootstrap-ca/O=EFDI-Bootstrap/C=LT" 2>/dev/null
chmod 600 "$CA_KEY"
ok "CA certificate: $CA_CERT"

info "Generating node key and certificate…"
openssl genrsa -out "$NODE_KEY" 2048 2>/dev/null
openssl req -new -key "$NODE_KEY" -out "$NODE_CSR" -nodes \
    -subj "/CN=${PARTNER_NAMESPACE}/O=EFDI-Bootstrap/C=LT" 2>/dev/null
openssl x509 -req -in "$NODE_CSR" -CA "$CA_CERT" -CAkey "$CA_KEY" \
    -CAcreateserial -out "$NODE_CERT" -days 3650 2>/dev/null
rm -f "$NODE_CSR" "$BUNDLE_DIR/efdi/efdi-ca-root.srl"
chmod 600 "$NODE_KEY"
ok "Node certificate: $NODE_CERT"

if [ "$INSTALL_MODE" = "production" ]; then
    warn "Bootstrap certificates are a throwaway self-signed identity — upload real"
    warn "certs from the WebUI's Certificates page before trusting this pod."
else
    warn "Test certificates are NOT suitable for production — valid 10 years, self-signed."
fi

# ── Pod state dirs + Zenoh config (bootstrap — TCP, no mTLS) ───────────────────
# Every install boots plaintext first, regardless of mode. Real mTLS is turned
# on from the WebUI once real certs are uploaded (see api/certs_bootstrap.py).
section "Zenoh router config (bootstrap — TCP, no mTLS)"
mkdir -p "${POD_STATE_DIR}/zenoh/tls" "${POD_STATE_DIR}/zenoh/rocksdb"

# A prior interrupted attempt (pre-degraded-boot install.sh, which never wrote
# this file in production mode) can leave Docker's own stray artifact here: a
# missing bind-mount source silently becomes an empty directory. `cat >` can't
# redirect onto a directory ("Is a directory") — clear it first, same guard
# reinstall.sh/update.sh already carry for this exact failure mode.
if [ -d "${POD_STATE_DIR}/zenoh/config.json5" ]; then
    rmdir "${POD_STATE_DIR}/zenoh/config.json5" 2>/dev/null || true
fi

cat > "${POD_STATE_DIR}/zenoh/config.json5" << ZCFG
{
  mode: "router",
  listen: {
    endpoints: ["tcp/0.0.0.0:7448"],
  },
  plugins_loading: {
    enabled: true,
    search_dirs: ["/", "/opt/zenoh/plugins"],
  },
  plugins: {
    storage_manager: {
      storages: {
        efdi_live: {
          key_expr: "${NAMESPACE_PREFIX}/${PARTNER_NAMESPACE}/**",
          volume: { id: "memory" },
        },
      },
    },
  },
}
ZCFG
ok "Zenoh config written: ${POD_STATE_DIR}/zenoh/config.json5"
printf '%s\n' "${NAMESPACE_PREFIX}" > "${POD_STATE_DIR}/namespace-prefix"
# zenoh-admin runs as fixed uid/gid 10001 and rewrites this file on every
# config save; installing as root (or any other host user) leaves it
# owner-only, so the very next Save & Restart fails with
# "[Errno 13] Permission denied" — silently, since the container never
# owned this file to begin with.
chgrp 10001 "${POD_STATE_DIR}/namespace-prefix" 2>/dev/null || true
chmod 664 "${POD_STATE_DIR}/namespace-prefix" 2>/dev/null || true
ok "Namespace prefix written: ${POD_STATE_DIR}/namespace-prefix (${NAMESPACE_PREFIX})"
# zenoh-admin bind-mounts this file too (docker-compose.yml); a missing
# source here means Docker silently creates a directory instead of a file,
# and every later config apply crashes with "[Errno 21] Is a directory"
# trying to write to it. Defaults to NAMESPACE_PREFIX, matching first-boot.sh.
printf '%s\n' "${NAMESPACE_PREFIX}" > "${POD_STATE_DIR}/data-topic-prefix"
chgrp 10001 "${POD_STATE_DIR}/data-topic-prefix" 2>/dev/null || true
chmod 664 "${POD_STATE_DIR}/data-topic-prefix" 2>/dev/null || true
ok "Data topic prefix written: ${POD_STATE_DIR}/data-topic-prefix (${NAMESPACE_PREFIX})"

# ── Write compose/.env ─────────────────────────────────────────────────────────
section "Writing compose/.env"

# Preserve any keys from an existing .env that we don't manage here (e.g. INBOUND_NAMESPACE).
EXTRA_LINES=""
if [ -f "$ENV_FILE" ]; then
    MANAGED_KEYS="POD_STATE_DIR|PARTNER_NAMESPACE|NAMESPACE_PREFIX|POD_ID|ZENOH_LOCAL_ENDPOINT|ZENOH_LOG|INSTALL_MODE"
    MANAGED_KEYS+="|BUNDLE_DIR|EFDI_CERT_DIR"
    MANAGED_KEYS+="|CAT48_PORT|CAT48_TCP|CAT48_RADAR_SAC|CAT48_RADAR_SIC|CAT48_RADAR_NAME|CAT48_RADAR_LAT|CAT48_RADAR_LON"
    MANAGED_KEYS+="|CAT21_PORT|CAT21_TCP|CAT20_PORT|CAT20_TCP"
    MANAGED_KEYS+="|SITAWARE_URL|SITAWARE_API_PATH|SITAWARE_USER|SITAWARE_PASS|SITAWARE_POLL_S|SITAWARE_DISCOVER"
    MANAGED_KEYS+="|TAK_HOST|TAK_PORT"
    MANAGED_KEYS+="|ZENOH_ADMIN_DB_USER|ZENOH_ADMIN_DB_PASSWORD|ZENOH_ADMIN_DB_ROOT_PASSWORD"
    MANAGED_KEYS+="|ZENOH_ADMIN_SECRET_KEY|ZENOH_ADMIN_FIRST_USER|ZENOH_ADMIN_FIRST_PASS|EFDI_CONTROL_TOKEN"
    EXTRA_LINES=$(grep -Ev "^(#|[[:space:]]*$)" "$ENV_FILE" 2>/dev/null \
                  | grep -Ev "^(${MANAGED_KEYS})=" || true)
fi

{
    echo "# EFDI compose/.env — written by install.sh on $(date -u '+%Y-%m-%d %H:%M UTC')"
    echo "# Mode: ${INSTALL_MODE}  — DO NOT commit to version control."
    echo ""
    echo "# ── Pod identity ──────────────────────────────────────────────────────"
    echo "INSTALL_MODE=${INSTALL_MODE}"
    echo "POD_STATE_DIR=${POD_STATE_DIR}"
    echo "PARTNER_NAMESPACE=${PARTNER_NAMESPACE}"
    echo "NAMESPACE_PREFIX=${NAMESPACE_PREFIX}"
    echo "POD_ID=efdi-pod"
    echo ""
    echo "# ── Zenoh ─────────────────────────────────────────────────────────────"
    echo "ZENOH_LOCAL_ENDPOINT=${ZENOH_LOCAL_ENDPOINT}"
    echo "ZENOH_LOG=info"
    echo ""
    echo "# ── Certificates ──────────────────────────────────────────────────────"
    echo "BUNDLE_DIR=${BUNDLE_DIR}"
    echo "EFDI_CERT_DIR=${BUNDLE_DIR}/efdi"
    echo ""
    echo "# ── ASTERIX radar ─────────────────────────────────────────────────────"
    [ -n "${CAT48_PORT:-}"       ] && echo "CAT48_PORT=${CAT48_PORT}"
    [ -n "${CAT48_RADAR_SAC:-}"  ] && echo "CAT48_RADAR_SAC=${CAT48_RADAR_SAC}"
    [ -n "${CAT48_RADAR_SIC:-}"  ] && echo "CAT48_RADAR_SIC=${CAT48_RADAR_SIC}"
    [ -n "${CAT48_RADAR_NAME:-}" ] && echo "CAT48_RADAR_NAME=${CAT48_RADAR_NAME}"
    [ -n "${CAT48_RADAR_LAT:-}"  ] && echo "CAT48_RADAR_LAT=${CAT48_RADAR_LAT}"
    [ -n "${CAT48_RADAR_LON:-}"  ] && echo "CAT48_RADAR_LON=${CAT48_RADAR_LON}"
    [ -n "${CAT21_PORT:-}"       ] && echo "CAT21_PORT=${CAT21_PORT}"
    [ -n "${CAT20_PORT:-}"       ] && echo "CAT20_PORT=${CAT20_PORT}"
    echo ""
    echo "# ── Sensor ports ──────────────────────────────────────────────────────"
    echo ""
    echo "# ── Integrations ──────────────────────────────────────────────────────"
    [ -n "${SITAWARE_URL:-}"    ] && echo "SITAWARE_URL=${SITAWARE_URL}"
    [ -n "${SITAWARE_API_PATH:-}" ] && echo "SITAWARE_API_PATH=${SITAWARE_API_PATH}"
    [ -n "${SITAWARE_USER:-}"   ] && echo "SITAWARE_USER=${SITAWARE_USER}"
    [ -n "${SITAWARE_PASS:-}"   ] && echo "SITAWARE_PASS=${SITAWARE_PASS}"
    [ -n "${SITAWARE_POLL_S:-}" ] && echo "SITAWARE_POLL_S=${SITAWARE_POLL_S}"
    [ -n "${TAK_HOST:-}"        ] && echo "TAK_HOST=${TAK_HOST}"
    [ -n "${TAK_PORT:-}"        ] && echo "TAK_PORT=${TAK_PORT}"
    echo ""
    echo "# ── API keys ──────────────────────────────────────────────────────────"
    echo ""
    echo "# ── Zenoh WebUI and MariaDB ───────────────────────────────────────────"
    echo "ZENOH_ADMIN_DB_USER=${ZENOH_ADMIN_DB_USER}"
    echo "ZENOH_ADMIN_DB_PASSWORD=${ZENOH_ADMIN_DB_PASSWORD}"
    echo "ZENOH_ADMIN_DB_ROOT_PASSWORD=${ZENOH_ADMIN_DB_ROOT_PASSWORD}"
    echo "ZENOH_ADMIN_SECRET_KEY=${ZENOH_ADMIN_SECRET_KEY}"
    echo "ZENOH_ADMIN_FIRST_USER=${ZENOH_ADMIN_FIRST_USER}"
    echo "ZENOH_ADMIN_FIRST_PASS=${ZENOH_ADMIN_FIRST_PASS}"
    echo "EFDI_CONTROL_TOKEN=${EFDI_CONTROL_TOKEN}"
    if [ -n "$EXTRA_LINES" ]; then
        echo ""
        echo "# ── Preserved from prior .env ────────────────────────────────────────"
        echo "$EXTRA_LINES"
    fi
} > "$ENV_FILE"
chmod 600 "$ENV_FILE"
ok "compose/.env written (mode 600)"

# ── Python venv ────────────────────────────────────────────────────────────────
section "Python virtual environment"
if [ ! -x "$VENV/bin/pip" ]; then
    # Also catches a venv left over from before the ensurepip fix above existed
    # (python3 present, pip missing) — re-running venv creation on an existing
    # directory doesn't wipe it, it just fills in what's missing, and ensurepip
    # is now guaranteed available system-wide.
    info "Creating venv at $VENV…"
    "$PYTHON" -m venv "$VENV"
fi
info "Synchronizing Python runtime dependencies…"
"$VENV/bin/pip" install --quiet --disable-pip-version-check \
    -r "$SCRIPT_DIR/compose/requirements.txt" \
    -r "$SCRIPT_DIR/compose/zenoh-admin/requirements.txt"
ok "Venv ready from compose/requirements.txt"

# ── Infrastructure ────────────────────────────────────────────────────────────
section "EFDI infrastructure"
GIT_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || printf unknown)"
export GIT_COMMIT
info "Building locally maintained infrastructure…"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build
info "Starting router, MariaDB, WebUI, and proxy…"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d || {
    dump_service_logs
    err "Infrastructure startup failed — see logs above."
}

db_container="$(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps -q zenoh-admin-db)"
for _ in $(seq 1 60); do
    if [ -n "$db_container" ] && docker exec "$db_container" \
        healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
if [ -z "$db_container" ] || ! docker exec "$db_container" \
    healthcheck.sh --connect --innodb_initialized >/dev/null 2>&1; then
    err "MariaDB did not become ready."
fi
ok "MariaDB ready"

# shellcheck source=scripts/scrub_admin_secret.sh
. "$SCRIPT_DIR/scripts/scrub_admin_secret.sh"
scrub_admin_bootstrap_secret "$ENV_FILE" \
    || err "Admin bootstrap credential could not be removed safely."

EFDI_NONINTERACTIVE=1 "$SCRIPT_DIR/start.sh" --restore
ok "Infrastructure and saved native services started"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════════════╗${NC}"
if [ "$INSTALL_MODE" = "testing" ]; then
echo -e "${YELLOW}${BOLD}║           EFDI bridge stack ready  [TESTING MODE]            ║${NC}"
else
echo -e "${GREEN}${BOLD}║                  EFDI bridge stack ready                     ║${NC}"
fi
echo -e "${BOLD}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "  Start bridges  : ./start.sh"
echo "  Stop bridges   : ./stop.sh"
echo "  Logs           : tail -f ${POD_STATE_DIR}/logs/<service>.log"
echo "  Config         : $ENV_FILE"
if [ "$INSTALL_MODE" = "testing" ]; then
    echo ""
    echo -e "  ${YELLOW}Test certs${NC} : $BUNDLE_DIR"
    echo -e "  ${YELLOW}Namespace${NC}  : $PARTNER_NAMESPACE"
    echo ""
    echo "  Full data flow is active in testing mode:"
    echo "    Sensor bridges → Zenoh (tcp/127.0.0.1:7448) → CoT layer → ATAK/WinTAK/iTAK"
    echo ""
    echo "  To deliver tracks to ATAK devices on your network, start.sh will:"
    echo "    • tak_layer  → TAK Server at TAK_HOST:TAK_PORT (if configured)"
    echo ""
    echo -e "  ${YELLOW}[!]${NC} No EFDI fabric connectivity — local pub/sub only."
    echo "      All bridges, Zenoh, and CoT delivery are fully functional."
fi
echo ""
if [ "$INSTALL_MODE" = "production" ]; then
    echo -e "  ${YELLOW}[!]${NC} Running on a throwaway self-signed identity — the pod is NOT yet secured."
    echo "      Open the Zenoh Admin WebUI, log in, and go to Certificates to upload"
    echo "      your real CA root/cert/key and set PARTNER_NAMESPACE. Saving switches"
    echo "      the router to mTLS automatically."
    echo ""
fi
