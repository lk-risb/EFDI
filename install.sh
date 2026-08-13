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

ask() {
    local _var="$1" _q="$2" _default="${3:-}" _ans
    if [ -n "$_default" ]; then
        read -rp "$(echo -e "  ${BOLD}${_q}${NC} [${_default}]: ")" _ans
        printf -v "$_var" '%s' "${_ans:-$_default}"
    else
        while true; do
            read -rp "$(echo -e "  ${BOLD}${_q}${NC}: ")" _ans
            [ -n "$_ans" ] && break
            echo "    (required)"
        done
        printf -v "$_var" '%s' "$_ans"
    fi
}

ask_opt() {  # ask_opt <var> <question> [default]  — empty answer allowed
    local _var="$1" _q="$2" _default="${3:-}" _ans
    if [ -n "$_default" ]; then
        read -rp "$(echo -e "  ${BOLD}${_q}${NC} [${_default}]: ")" _ans
        printf -v "$_var" '%s' "${_ans:-$_default}"
    else
        read -rp "$(echo -e "  ${BOLD}${_q}${NC} (leave blank to skip): ")" _ans
        printf -v "$_var" '%s' "${_ans:-}"
    fi
}

ask_secret() {
    local _var="$1" _q="$2" _ans
    read -rsp "$(echo -e "  ${BOLD}${_q}${NC}: ")" _ans
    echo
    printf -v "$_var" '%s' "$_ans"
}

env_value() {
    local key="$1"
    [ -f "$ENV_FILE" ] || return 0
    sed -n "s/^${key}=//p" "$ENV_FILE" | head -1
}

gen_uuid() { python3 -c 'import uuid; print(uuid.uuid4().hex)'; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  EFDI — Bridge Stack Installer${NC}"
echo "  Sensor bridges: ASTERIX · SitaWare · dronuradaras"
echo ""

# ── Existing installation ────────────────────────────────────────────────────
if [ -f "$ENV_FILE" ]; then
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
                    ask_secret NETBIRD_SETUP_KEY "NetBird setup key (app.netbird.io → Keys)"
                    info "Installing NetBird…"
                    # Official vendor installer, fetched fresh over HTTPS — not
                    # checksum-pinned since it's a live, auto-updating script;
                    # the trust boundary is the TLS connection to NetBird's own domain.
                    curl -fsSL https://pkgs.netbird.io/install.sh | sh
                    info "Connecting to NetBird…"
                    sudo netbird up --setup-key="$NETBIRD_SETUP_KEY" \
                        || err "NetBird connection failed — check your setup key and re-run."
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

# ── EFDI certs / test-certs ────────────────────────────────────────────────────
if [ "$INSTALL_MODE" = "production" ]; then
    section "EFDI certificates (mTLS)"
    echo "  Generated by scripts/gen-certs.sh <namespace> — a directory containing:"
    echo "    efdi/efdi-ca-root.pem   efdi/<NAMESPACE>-cert.pem   efdi/<NAMESPACE>-key.pem"
    echo ""
    ask BUNDLE_DIR "certs directory" "$HOME/efdi-certs"

    if [ -d "$BUNDLE_DIR" ]; then
        PEM_COUNT=$(find "$BUNDLE_DIR/efdi" -maxdepth 1 -type f -name '*.pem' 2>/dev/null | wc -l)
        if (( PEM_COUNT >= 3 )); then
            ok "Certs found ($PEM_COUNT .pem files)"
        else
            warn "Directory exists but has fewer than 3 .pem files — run scripts/gen-certs.sh <namespace>"
        fi
    else
        warn "Path does not exist yet — run scripts/gen-certs.sh <namespace> before starting bridges"
    fi

    section "EFDI Namespace"
    echo "  Your PARTNER_NAMESPACE is the namespace you passed to scripts/gen-certs.sh."
    echo "  Example: 0123456789abcdef0123456789abcdef"
    echo ""
    EXISTING_NS=""
    if [ -f "$ENV_FILE" ]; then
        EXISTING_NS=$(grep '^PARTNER_NAMESPACE=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')
    fi
    ask PARTNER_NAMESPACE "PARTNER_NAMESPACE" "${EXISTING_NS:-}"

    EXISTING_PREFIX=""
    if [ -f "$ENV_FILE" ]; then
        EXISTING_PREFIX=$(grep '^NAMESPACE_PREFIX=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')
    fi
    echo ""
    echo "  NAMESPACE_PREFIX is your deployment's topic prefix and may have any depth."
    echo "  Use a stable organization name; do not put an IP address or secret in it."
    ask NAMESPACE_PREFIX "NAMESPACE_PREFIX" "${EXISTING_PREFIX:-EFDI}"

    section "Pod state directory"
    echo "  Zenoh router config and TLS material live here (created by first-boot.sh)."
    echo "  Must be on the LUKS-encrypted volume in production."
    echo ""
    EXISTING_POD_STATE=""
    if [ -f "$ENV_FILE" ]; then
        EXISTING_POD_STATE=$(grep '^POD_STATE_DIR=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')
    fi
    ask POD_STATE_DIR "POD_STATE_DIR" "${EXISTING_POD_STATE:-$HOME/efdi-pod}"

    EXISTING_ZENOH_ENDPOINT=""
    if [ -f "$ENV_FILE" ]; then
        EXISTING_ZENOH_ENDPOINT=$(grep '^ZENOH_LOCAL_ENDPOINT=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[:space:]')
    fi
    ZENOH_LOCAL_ENDPOINT="${EXISTING_ZENOH_ENDPOINT:-tcp/127.0.0.1:7448}"

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
section "ASTERIX radar (CAT-48/34)"
echo "  Leave blank if no radar is connected."
echo ""
ask_opt CAT48_PORT      "Radar UDP port (CAT48_PORT)"     "50048"
if [ -n "${CAT48_PORT:-}" ]; then
    ask_opt CAT48_RADAR_SAC  "Radar SAC (Source Area Code)"    ""
    ask_opt CAT48_RADAR_SIC  "Radar SIC (Source Identification Code)" ""
    ask_opt CAT48_RADAR_NAME "Radar display name in C2 clients" "ASTERIX Radar"
    ask_opt CAT48_RADAR_LAT  "Radar fallback latitude  (or blank for auto from CAT-34)" ""
    ask_opt CAT48_RADAR_LON  "Radar fallback longitude (or blank for auto from CAT-34)" ""
    ask_opt CAT21_PORT       "ADS-B CAT-21 UDP port (optional)" ""
    ask_opt CAT20_PORT       "MLAT  CAT-20 UDP port (optional)" ""
fi

section "SitaWare friendly-force tracking"
ask_opt SITAWARE_URL  "SitaWare URL (e.g. https://sitaware.example.com)" ""
if [ -n "${SITAWARE_URL:-}" ]; then
    ask_opt SITAWARE_API_PATH "Documented SitaWare JSON resource path" ""
    ask_opt SITAWARE_USER "SitaWare username" ""
    ask_opt SITAWARE_PASS "SitaWare password" ""
    ask_opt SITAWARE_POLL_S "Poll interval in seconds" "10"
fi

section "TAK Server (optional — for cross-subnet CoT)"
ask_opt TAK_HOST "TAK Server hostname/IP" ""
ask_opt TAK_PORT "TAK Server port"        "8087"

section "API keys (optional)"

# ── Admin credentials and generated secrets ──────────────────────────────────
section "Zenoh WebUI administrator"
ZENOH_ADMIN_FIRST_USER="$(env_value ZENOH_ADMIN_FIRST_USER)"
ZENOH_ADMIN_FIRST_USER="${ZENOH_ADMIN_FIRST_USER:-admin}"
if [ "$INSTALL_MODE" = "production" ]; then
    ask ZENOH_ADMIN_FIRST_USER "Admin username" "$ZENOH_ADMIN_FIRST_USER"
    while true; do
        ask_secret ZENOH_ADMIN_FIRST_PASS "Admin password (minimum 12 characters)"
        [ "${#ZENOH_ADMIN_FIRST_PASS}" -ge 12 ] && break
        warn "Minimum 12 characters required."
    done
else
    ZENOH_ADMIN_FIRST_PASS="$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)"
fi
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

# ── Generate test certificates (testing mode only) ────────────────────────────
if [ "$INSTALL_MODE" = "testing" ]; then
    section "Generating self-signed test certificates"
    mkdir -p "$BUNDLE_DIR/efdi"

    CA_KEY="$BUNDLE_DIR/efdi/efdi-ca-root-key.pem"
    CA_CERT="$BUNDLE_DIR/efdi/efdi-ca-root.pem"
    NODE_KEY="$BUNDLE_DIR/efdi/${PARTNER_NAMESPACE}-key.pem"
    NODE_CSR="$BUNDLE_DIR/.node.csr"
    NODE_CERT="$BUNDLE_DIR/efdi/${PARTNER_NAMESPACE}-cert.pem"

    info "Generating CA key and self-signed certificate…"
    openssl genrsa -out "$CA_KEY" 2048 2>/dev/null
    openssl req -new -x509 -key "$CA_KEY" -out "$CA_CERT" -days 3650 -nodes \
        -subj "/CN=efdi-test-ca/O=EFDI-Test/C=LT" 2>/dev/null
    chmod 600 "$CA_KEY"
    ok "CA certificate: $CA_CERT"

    info "Generating node key and certificate…"
    openssl genrsa -out "$NODE_KEY" 2048 2>/dev/null
    openssl req -new -key "$NODE_KEY" -out "$NODE_CSR" -nodes \
        -subj "/CN=${PARTNER_NAMESPACE}/O=EFDI-Test/C=LT" 2>/dev/null
    openssl x509 -req -in "$NODE_CSR" -CA "$CA_CERT" -CAkey "$CA_KEY" \
        -CAcreateserial -out "$NODE_CERT" -days 3650 2>/dev/null
    rm -f "$NODE_CSR" "$BUNDLE_DIR/efdi/efdi-ca-root.srl"
    chmod 600 "$NODE_KEY"
    ok "Node certificate: $NODE_CERT"

    warn "Test certificates are NOT suitable for production — valid 10 years, self-signed."
fi

# ── Pod state dirs + Zenoh config (testing mode) ──────────────────────────────
if [ "$INSTALL_MODE" = "testing" ]; then
    section "Zenoh router config (test — TCP, no mTLS)"
    mkdir -p "${POD_STATE_DIR}/zenoh/tls" "${POD_STATE_DIR}/zenoh/rocksdb"

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
    ok "Namespace prefix written: ${POD_STATE_DIR}/namespace-prefix (${NAMESPACE_PREFIX})"
fi

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
if [ ! -x "$VENV/bin/python3" ]; then
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
ZENOH_CONFIG="${POD_STATE_DIR}/zenoh/config.json5"
if [ ! -f "$ZENOH_CONFIG" ]; then
    warn "Zenoh config not found at $ZENOH_CONFIG"
    warn "Run host/first-boot.sh with certs from scripts/gen-certs.sh to generate it, then:"
    warn "  docker compose -f compose/docker-compose.yml up -d zenoh-router"
else
    GIT_COMMIT="$(git -C "$SCRIPT_DIR" rev-parse HEAD 2>/dev/null || printf unknown)"
    export GIT_COMMIT
    info "Building locally maintained infrastructure…"
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build
    info "Starting router, MariaDB, WebUI, and proxy…"
    docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d

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
fi

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
if [ "$INSTALL_MODE" = "production" ] && [ ! -f "$ZENOH_CONFIG" ]; then
    echo -e "  ${YELLOW}[!]${NC} Run host/first-boot.sh with certs from scripts/gen-certs.sh before starting bridges."
    echo ""
fi
