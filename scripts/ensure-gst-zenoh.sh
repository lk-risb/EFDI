#!/usr/bin/env bash
# ensure-gst-zenoh.sh — idempotent installer for gst-plugin-zenoh
# (zenohsrc/zenohsink/zenohdemux), the GStreamer/Zenoh dependency
# video_zenoh_bridge.py needs (github.com/p13marc/gst-plugin-zenoh).
#
# Called from install.sh (fresh installs), update.sh and health.sh
# (self-heal a host that's missing it — an older checkout, or a prior
# attempt here that failed and needs a retry). Safe to run repeatedly:
# exits immediately once zenohsrc/zenohsink actually load.
#
# Best-effort, never fatal to the caller: this is a prototype-stage,
# optional feature (see compose/bridges/video_zenoh_bridge.py). On failure
# this script exits 1 and start.sh's video-zenoh-bridge svc_ready() simply
# leaves that one service unavailable until it's fixed — nothing else in
# the stack depends on it.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/compose/.env"

# shellcheck source=scripts/_spinner.sh
. "$ROOT/scripts/_spinner.sh"

env_value() {
    [ -f "$ENV_FILE" ] || return 0
    grep "^$1=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '[:space:]'
}

POD_STATE_DIR="$(env_value POD_STATE_DIR)"
POD_STATE_DIR="${POD_STATE_DIR:-$HOME/efdi-pod}"
BUILD_DIR="${POD_STATE_DIR}/gst-plugin-zenoh"
PLUGIN_DIR="$BUILD_DIR/target/release"
ENV_PLUGIN_PATH="$(env_value VIDEO_ZENOH_PLUGIN_PATH)"
_check_paths="${ENV_PLUGIN_PATH:+$ENV_PLUGIN_PATH:}${PLUGIN_DIR}${GST_PLUGIN_PATH:+:$GST_PLUGIN_PATH}"

is_installed() {
    command -v gst-inspect-1.0 >/dev/null 2>&1 || return 1
    GST_PLUGIN_PATH="$_check_paths" gst-inspect-1.0 zenohsrc >/dev/null 2>&1
}

if is_installed; then
    ok "gst-plugin-zenoh already installed (zenohsrc/zenohsink available)"
    exit 0
fi

info "gst-plugin-zenoh (zenohsrc/zenohsink) not found — installing for video_zenoh_bridge.py…"

PKG_MGR=""
command -v apt-get >/dev/null 2>&1 && PKG_MGR="apt"
[ -z "$PKG_MGR" ] && command -v dnf >/dev/null 2>&1 && PKG_MGR="dnf"

case "$PKG_MGR" in
    apt)
        _apt=(apt-get); [ "$(id -u)" -eq 0 ] || _apt=(sudo apt-get)
        run_spin "Installing GStreamer development packages (apt)" "GStreamer development packages installed" \
            "${_apt[@]}" install -y -qq \
            gstreamer1.0-tools libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev \
            gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
            libunwind-dev pkg-config build-essential curl ca-certificates \
            || { warn "GStreamer package install failed — video-zenoh-bridge will stay unavailable."; exit 1; }
        ;;
    dnf)
        _dnf=(dnf); [ "$(id -u)" -eq 0 ] || _dnf=(sudo dnf)
        run_spin "Installing GStreamer development packages (dnf)" "GStreamer development packages installed" \
            "${_dnf[@]}" install -y -q \
            gstreamer1-devel gstreamer1-plugins-base-devel \
            gstreamer1-plugins-good gstreamer1-plugins-bad-free gstreamer1-plugins-ugly-free \
            libunwind-devel pkgconf-pkg-config gcc gcc-c++ make curl ca-certificates \
            || { warn "GStreamer package install failed — video-zenoh-bridge will stay unavailable."; exit 1; }
        ;;
    *)
        warn "No apt or dnf found — skipping gst-plugin-zenoh (install GStreamer + Rust manually per docs)."
        exit 1
        ;;
esac

# apt/dnf's own rustc is usually years behind gst-plugin-zenoh's MSRV
# (edition 2024, needs 1.97+) — rustup, isolated under POD_STATE_DIR so it
# never touches or conflicts with a system Rust toolchain other tooling
# might depend on.
export RUSTUP_HOME="${POD_STATE_DIR}/rustup"
export CARGO_HOME="${POD_STATE_DIR}/cargo"
if ! "${CARGO_HOME}/bin/rustc" --version 2>/dev/null | grep -qE ' 1\.(9[7-9]|[1-9][0-9]{2})\.'; then
    run_spin "Installing Rust toolchain (rustup, isolated under $POD_STATE_DIR)" "Rust toolchain installed" \
        bash -c 'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain 1.97 --profile minimal' \
        || { warn "rustup install failed — video-zenoh-bridge will stay unavailable."; exit 1; }
fi
# shellcheck disable=SC1091
. "${CARGO_HOME}/env"

if [ -d "$BUILD_DIR/.git" ]; then
    run_spin "Updating gst-plugin-zenoh source" "gst-plugin-zenoh source updated" \
        git -C "$BUILD_DIR" fetch --depth 1 origin master \
        || warn "Could not update gst-plugin-zenoh source — building existing checkout as-is."
    git -C "$BUILD_DIR" reset --hard origin/master >/dev/null 2>&1 || true
else
    run_spin "Cloning gst-plugin-zenoh" "gst-plugin-zenoh cloned" \
        git clone --depth 1 https://github.com/p13marc/gst-plugin-zenoh.git "$BUILD_DIR" \
        || { warn "Clone failed — video-zenoh-bridge will stay unavailable."; exit 1; }
fi

# -j2 caps peak memory on small VMs (a full zenoh + gstreamer-bindings build
# is memory-heavy at default all-cores parallelism) — this only ever runs
# once per host, not on every deploy, so the extra build time is a one-off.
run_spin "Building gst-plugin-zenoh (can take several minutes)" "gst-plugin-zenoh built" \
    bash -c "cd '$BUILD_DIR' && CARGO_BUILD_JOBS=2 '$CARGO_HOME/bin/cargo' build --release" \
    || { warn "Build failed — video-zenoh-bridge will stay unavailable. Source kept at $BUILD_DIR."; exit 1; }

if ! is_installed; then
    warn "Build succeeded but zenohsrc still doesn't load from $PLUGIN_DIR — check for errors above."
    exit 1
fi

ok "gst-plugin-zenoh built: $PLUGIN_DIR/libgstzenoh.so"
if [ -f "$ENV_FILE" ] && ! grep -q '^VIDEO_ZENOH_PLUGIN_PATH=' "$ENV_FILE"; then
    printf 'VIDEO_ZENOH_PLUGIN_PATH=%s\n' "$PLUGIN_DIR" >> "$ENV_FILE"
    ok "Recorded VIDEO_ZENOH_PLUGIN_PATH=$PLUGIN_DIR in compose/.env"
fi
