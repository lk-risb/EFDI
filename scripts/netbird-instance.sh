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

NAME="${1:?usage: netbird-instance.sh <instance-name> <management-url> <setup-key>}"
MANAGEMENT_URL="${2:?usage: netbird-instance.sh <instance-name> <management-url> <setup-key>}"
SETUP_KEY="${3:?usage: netbird-instance.sh <instance-name> <management-url> <setup-key>}"

if ! [[ "$NAME" =~ ^[a-z0-9-]+$ ]]; then
  echo "instance-name must match [a-z0-9-]+ (used verbatim in a systemd unit name and file paths)" >&2
  exit 1
fi
if [ "$NAME" = "netbird" ] || [ "$NAME" = "default" ]; then
  echo "refusing to use '$NAME' — reserved for the host's own default netbird instance" >&2
  exit 1
fi

CONFIG_DIR="/etc/netbird-${NAME}"
LOG_DIR="/var/log/netbird-${NAME}"
SOCK="unix:///var/run/netbird-${NAME}.sock"
UNIT_PATH="/etc/systemd/system/netbird-${NAME}.service"
SERVICE_NAME="netbird-${NAME}"

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
systemctl enable --now "$SERVICE_NAME"

# Give the freshly started daemon a moment to open its control socket before
# the client CLI tries to dial it.
for _ in $(seq 1 20); do
  [ -S "/var/run/netbird-${NAME}.sock" ] && break
  sleep 0.5
done

# `netbird up` is idempotent against an already-joined instance — re-running
# it with a new management-url/setup-key re-registers against the new
# account. The setup key never touches disk here; it only ever exists as a
# short-lived argv on this one command.
netbird up --daemon-addr "$SOCK" --management-url "$MANAGEMENT_URL" --setup-key "$SETUP_KEY"

netbird status --daemon-addr "$SOCK"
