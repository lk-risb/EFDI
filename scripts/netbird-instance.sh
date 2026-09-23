#!/usr/bin/env bash
# Provisions (or reconfigures) a SECOND, independent NetBird client instance
# on this host, running alongside the host's existing default `netbird`
# service without disrupting it.
#
# Why this exists: this host's default netbird.service joins exactly one
# NetBird account (management server) — confirmed live, its own unit has
# `--disable-profiles`, and `netbird profile list` shows a single "default"
# profile. `netbird.ltu` (this org's own network) and the EFDI Backbone trial
# fabric are two SEPARATE NetBird accounts, so reaching both at once needs two
# separate daemons: two systemd units, two config dirs, two control sockets,
# two log dirs. Each netbird service instance still only holds one account —
# this script does not add multi-profile switching to a single daemon, it
# adds a second daemon.
#
# The zenoh-router-backbone container (see docker-compose.yml,
# api/backbone_bootstrap.py) needs this second instance ("backbone") up and
# joined before its outbound mTLS link to
# tls/zenoh.efdi.netbird.efdi-backbone.net:7447 can resolve/route at all —
# NetBird DNS + overlay routing for that domain only exists inside the
# backbone account.
#
# Usage:
#   sudo scripts/netbird-instance.sh <instance-name> <management-url> <setup-key>
#
# Re-running with a new management-url/setup-key on an EXISTING instance name
# reconfigures that instance in place (idempotent) — this is the mechanism for
# rotating a setup key or pointing an instance at a different account later,
# without hand-editing systemd units over SSH each time.
#
# instance-name becomes: netbird-<name>.service, /etc/netbird-<name>/,
# /var/log/netbird-<name>/, unix:///var/run/netbird-<name>.sock — never
# touches the host's own default netbird.service/socket/config.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (systemd unit install + /etc, /var/log writes)" >&2
  exit 1
fi

# Pure bash trim (no xargs/sed re-parsing — a setup key or URL could contain
# characters those would treat specially). Strips leading/trailing
# whitespace only. A trailing newline from a copy-paste (portal pages,
# terminals, and clipboard managers all commonly add one) turns an otherwise
# correct key into a literally different string — NetBird's server rejects
# that as "setup key is invalid" with no hint that whitespace is the actual
# cause, so it silently looks like a bad/burned key instead.
trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

NAME="${1:?usage: netbird-instance.sh <instance-name> <management-url> <setup-key>}"
MANAGEMENT_URL="$(trim "${2:?usage: netbird-instance.sh <instance-name> <management-url> <setup-key>}")"
SETUP_KEY="$(trim "${3:?usage: netbird-instance.sh <instance-name> <management-url> <setup-key>}")"

if ! [[ "$NAME" =~ ^[a-z0-9-]+$ ]]; then
  echo "instance-name must match [a-z0-9-]+ (used verbatim in a systemd unit name and file paths)" >&2
  exit 1
fi
if [ "$NAME" = "netbird" ] || [ "$NAME" = "default" ]; then
  echo "refusing to use '$NAME' — reserved for the host's own default netbird instance" >&2
  exit 1
fi
# "wt-${NAME}" must fit Linux's IFNAMSIZ (16 bytes incl. NUL, so 15 usable
# chars) — "wt-" takes 3, leaving 12 for NAME.
if [ "${#NAME}" -gt 12 ]; then
  echo "instance-name must be 12 characters or fewer (used in a 15-char WireGuard interface name)" >&2
  exit 1
fi

CONFIG_DIR="/etc/netbird-${NAME}"
LOG_DIR="/var/log/netbird-${NAME}"
SOCK="unix:///var/run/netbird-${NAME}.sock"
UNIT_PATH="/etc/systemd/system/netbird-${NAME}.service"
SERVICE_NAME="netbird-${NAME}"
# Confirmed live: `netbird up`'s --interface-name and --wireguard-port both
# default to the SAME value ("wt0" / 51820) on every instance, including the
# host's own default netbird.service. Two instances that never override
# these silently collide on the interface name — the second one never gets
# its own real WireGuard device, so its traffic falls through to whichever
# instance actually holds "wt0" (confirmed: routes to backbone peer IPs
# resolved via the PRIMARY instance's table and went nowhere, even though
# `netbird status` on the backbone daemon reported "Connected" with a real
# IP the whole time — that status reflects successful control-plane login,
# not a working local data-plane interface).
#
# Interface name is self-descriptive ("wt-backbone", not "wt1") — reading
# `ip link show` shouldn't require memorizing which number means what.
# WireGuard port is an explicit table, same manual-curation style as
# admin_control.py's _NETBIRD_INSTANCES allowlist: add one line per new
# instance name, rather than deriving it — a derived value doesn't need to
# exist yet for the one instance this actually has today, and an explicit
# table is easier to read and to grep for a collision than a hash.
IFACE_NAME="wt-${NAME}"
case "$NAME" in
  backbone) WIREGUARD_PORT=51821 ;;
  *)
    echo "no WireGuard port assigned for instance '$NAME' — add one to the case in netbird-instance.sh (pick anything unused, e.g. 51822)" >&2
    exit 1
    ;;
esac

mkdir -p "$CONFIG_DIR" "$LOG_DIR"
chmod 700 "$CONFIG_DIR"

cat > "$UNIT_PATH" <<EOF
[Unit]
Description=NetBird mesh network client — ${NAME}
ConditionFileIsExecutable=/usr/bin/netbird

After=network.target syslog.target

[Service]
StartLimitInterval=5
StartLimitBurst=10
ExecStart=/usr/bin/netbird "service" "run" "--config" "${CONFIG_DIR}/config.json" "--log-level" "info" "--daemon-addr" "${SOCK}" "--log-file" "${LOG_DIR}/client.log" "--disable-profiles"

StandardOutput=file:${LOG_DIR}/netbird.out
StandardError=file:${LOG_DIR}/netbird.err

Restart=always
RestartSec=120

Environment=SYSTEMD_UNIT=${SERVICE_NAME}
[Install]
WantedBy=multi-user.target
EOF
# Mirrors this host's own /etc/systemd/system/netbird.service verbatim,
# renamed and pointed at isolated paths — same daemon binary, same flags,
# no behavioral guesswork.

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
# Always a full restart, never just "start if not already running":
# confirmed live, a running daemon does NOT recreate its WireGuard interface
# just because a later `netbird up` call below passes a different
# --interface-name/--wireguard-port — those only take effect from a clean
# process start. Without this, a re-run of this script (rotating a setup
# key, or picking up an interface/port fix) silently keeps the daemon on
# whatever interface/port it happened to create the FIRST time it ever
# started, no matter what flags `up` is given afterward.
systemctl restart "$SERVICE_NAME"

# Give the freshly (re)started daemon a moment to open its control socket
# before the client CLI tries to dial it.
for _ in $(seq 1 20); do
  [ -S "/var/run/netbird-${NAME}.sock" ] && break
  sleep 0.5
done

# `netbird up` is idempotent against an already-joined instance — re-running
# it with a new management-url/setup-key re-registers against the new
# account. The setup key never touches disk here; it only ever exists as a
# short-lived argv on this one command.
#
# Confirmed live: on a bad/burned setup key, `netbird up` does NOT fail fast
# — it retries the login with growing backoff indefinitely, printing the
# real error each time but never returning control. Left unwrapped, this
# just blocks until admin_control.py's own 90s subprocess timeout kills it,
# which then reports a bare "timed out" and throws away everything printed
# here. 25s is enough for the real error (or success) to show up at least
# once; `timeout`'s own 124 exit still trips `set -e` below, same as any
# other failure.
timeout 25s netbird up --daemon-addr "$SOCK" --management-url "$MANAGEMENT_URL" --setup-key "$SETUP_KEY" \
  --interface-name "$IFACE_NAME" --wireguard-port "$WIREGUARD_PORT"

netbird status --daemon-addr "$SOCK"
