#!/usr/bin/env bash
# moon-pod — first-boot bring-up (run once on the partner host, as root).
#
# Turns a signed enrollment bundle + an environment profile into a running pod:
#   goat profile init (verify + extract certs)  ->  render Zenoh config  ->
#   goat-clientd --import-bundle + enable mesh service  ->  docker compose up.
#
# goat-cli does the heavy lifting for the bundle: `goat profile init <bundle>` verifies the
# ECDSA-over-canonical-CBOR signature, checks expiry/activation, and writes the mTLS material
# (mtls.cert.pem / mtls.key.pem / ca-roots.pem) + portal token into the profile dir. We reuse
# those extracted certs for the Zenoh router rather than parsing CBOR ourselves.
#
# Idempotent where practical (re-running re-renders config + re-applies compose). goat-client
# v0.2.0 flags are confirmed against the release tag (docs/goat-client-v02-contract.md). On the sandbox
# the mesh is stock netbird, so the goat-clientd step is skipped gracefully when it's absent.
set -euo pipefail

POD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE="${1:-}"
BUNDLE="${2:-}"

usage() {
  echo "usage: sudo $0 <profile-name> <bundle.cbor>   e.g. $0 efdi /path/to/pod.cbor"
  exit 2
}
[ -n "$PROFILE" ] && [ -n "$BUNDLE" ] || usage
[ "$(id -u)" -eq 0 ] || { echo "must run as root (tunnel + cert privileges)"; exit 2; }

PROFILE_ENV="${POD_DIR}/profiles/${PROFILE}/profile.env"
[ -f "$PROFILE_ENV" ] || { echo "no such profile: ${PROFILE_ENV}"; exit 2; }
[ -f "$BUNDLE" ]      || { echo "no such bundle: ${BUNDLE}"; exit 2; }

# Tool presence — fail loud + early rather than midway.
for t in goat envsubst docker; do
  command -v "$t" >/dev/null 2>&1 || { echo "required tool not found on PATH: $t"; exit 2; }
done
command -v goat-clientd >/dev/null 2>&1 || echo "warning: goat-clientd not on PATH — mesh step (5) will be skipped"

# shellcheck disable=SC1090
source "$PROFILE_ENV"
: "${POD_STATE_DIR:=/var/lib/goat-moon}"
: "${GOAT_CLIENT_MODE:=combined}"
: "${ZENOH_LISTEN_PORT:=7447}"
: "${ZENOH_LOCAL_TCP_PORT:=7448}"
: "${ZENOH_VERIFY_NAME_ON_CONNECT:=false}"
: "${ZENOH_PLUGINS_LOADING_ENABLED:=true}"
: "${PARTNER_NAMESPACE:?PARTNER_NAMESPACE must be set in the profile}"
# INBOUND_NAMESPACE: bilateral prefix the fabric publishes TO the pod (profile-driven, not
# hardcoded to any stack). Default to the pod's own namespace so the rendered ACL is always valid
# even when no bilateral inbound is granted yet.
: "${INBOUND_NAMESPACE:=${PARTNER_NAMESPACE}}"
# ZENOH_FABRIC_ENDPOINT: profile fallback ONLY; the bundle's router_endpoint takes precedence
# (resolved from the bundle-derived profile.toml in step [3/5]).
: "${ZENOH_FABRIC_ENDPOINT:=}"

echo "==> goat-moon-pod first-boot (profile=${PROFILE}, state=${POD_STATE_DIR})"
echo "    WARNING: ${POD_STATE_DIR} MUST be on the partner's LUKS-encrypted volume."

GOAT_PROFILE_DIR="${POD_STATE_DIR}/goat-cli"
ZENOH_TLS_DIR="${POD_STATE_DIR}/zenoh/tls"
mkdir -p "${POD_STATE_DIR}/zenoh/rocksdb" "${GOAT_PROFILE_DIR}" "${ZENOH_TLS_DIR}"
chmod 700 "${POD_STATE_DIR}" "${GOAT_PROFILE_DIR}"

# --- [1/5] Verify bundle + extract certs + init operator profile (one goat-cli command) ----------
# `goat profile init` verifies the signature against the bundle's OWN CA roots (no compiled-in
# pin), checks expiry/activation-deadline, and writes mtls.cert.pem / mtls.key.pem / ca-roots.pem
# + portal token into the profile context dir. This is steps "verify", "extract certs", and
# "init goat-cli" folded into one.
echo "==> [1/5] goat profile init (verify bundle + extract mTLS material)"
# Idempotent: `goat profile init` refuses if a profile already exists (E_BUNDLE_INVALID with a
# "use rotate" hint). On re-run, rotate instead (preserves the prior context at .bak/).
if find "${GOAT_PROFILE_DIR}" -name mtls.cert.pem -print -quit 2>/dev/null | grep -q .; then
  echo "    profile already present — rotating (preserves prior at .bak/)"
  GOAT_PROFILE_DIR="${GOAT_PROFILE_DIR}" goat profile rotate "${BUNDLE}"
else
  GOAT_PROFILE_DIR="${GOAT_PROFILE_DIR}" goat profile init "${BUNDLE}"
fi

# Locate the extracted certs. `goat profile init` writes them under <root>/contexts/<name>/
# (NOT the root that `goat profile path` returns). Resolve the context dir directly as the
# directory CONTAINING mtls.cert.pem — robust to a renamed default context + to `goat profile
# path` returning the profile root rather than the active context.
CERT_PATH="$(find "${GOAT_PROFILE_DIR}" -name mtls.cert.pem -print -quit 2>/dev/null || true)"
[ -n "${CERT_PATH}" ] && [ -f "${CERT_PATH}" ] \
  || { echo "goat profile init did not produce mtls.cert.pem under ${GOAT_PROFILE_DIR}"; exit 1; }
CTX_DIR="$(dirname "${CERT_PATH}")"

# --- [2/5] Lay the pod's Zenoh mTLS material from the goat-cli-extracted certs -------------------
echo "==> [2/5] laying Zenoh mTLS certs from the goat-cli profile"
install -m 600 "${CTX_DIR}/mtls.cert.pem" "${ZENOH_TLS_DIR}/pod-cert.pem"
install -m 600 "${CTX_DIR}/mtls.key.pem"  "${ZENOH_TLS_DIR}/pod-key.pem"
install -m 600 "${CTX_DIR}/ca-roots.pem"  "${ZENOH_TLS_DIR}/ca-roots.pem"

# --- [3/5] Render the pod Zenoh router config ---------------------------------------------------
# The SIGNED BUNDLE is the single source of truth for the fabric router endpoint: `goat profile
# init` wrote the bundle's goat_cli_extras.router_endpoint into profile.toml. We read it from
# there so the pod follows the bundle, never a stale profile constant. The ZENOH_FABRIC_ENDPOINT
# profile var is a fallback only (hand-driven validation, or a bundle that omitted the field).
echo "==> [3/5] rendering Zenoh router config"
PROFILE_TOML="$(find "${GOAT_PROFILE_DIR}" -name profile.toml -print -quit 2>/dev/null || true)"
BUNDLE_ROUTER=""
if [ -n "${PROFILE_TOML}" ] && [ -f "${PROFILE_TOML}" ]; then
  BUNDLE_ROUTER="$(sed -n 's/^[[:space:]]*router_endpoint[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${PROFILE_TOML}" | head -1)"
fi
if [ -n "${BUNDLE_ROUTER}" ]; then
  ZENOH_FABRIC_ENDPOINT="${BUNDLE_ROUTER}"
  echo "    router endpoint from bundle: ${ZENOH_FABRIC_ENDPOINT}"
elif [ -n "${ZENOH_FABRIC_ENDPOINT}" ]; then
  echo "    router endpoint from profile fallback (bundle carried none): ${ZENOH_FABRIC_ENDPOINT}"
else
  echo "ZENOH_FABRIC_ENDPOINT unset and the bundle carried no router_endpoint — cannot render"; exit 1
fi
export ZENOH_LISTEN_PORT ZENOH_LOCAL_TCP_PORT ZENOH_FABRIC_ENDPOINT PARTNER_NAMESPACE INBOUND_NAMESPACE
export ZENOH_VERIFY_NAME_ON_CONNECT ZENOH_PLUGINS_LOADING_ENABLED
export POD_CERT_PEM="/etc/zenoh/tls/pod-cert.pem"   # in-container paths (compose mounts ZENOH_TLS_DIR ro)
export POD_KEY_PEM="/etc/zenoh/tls/pod-key.pem"
export CA_ROOTS_PEM="/etc/zenoh/tls/ca-roots.pem"
envsubst \
  '${ZENOH_LISTEN_PORT} ${ZENOH_LOCAL_TCP_PORT} ${ZENOH_FABRIC_ENDPOINT} ${PARTNER_NAMESPACE} ${INBOUND_NAMESPACE} ${ZENOH_VERIFY_NAME_ON_CONNECT} ${ZENOH_PLUGINS_LOADING_ENABLED} ${POD_CERT_PEM} ${POD_KEY_PEM} ${CA_ROOTS_PEM}' \
  < "${POD_DIR}/host/zenoh-router.json5.tmpl" \
  > "${POD_STATE_DIR}/zenoh/config.json5"
echo "    wrote ${POD_STATE_DIR}/zenoh/config.json5"

# --- [4/5] Bring up the host mesh daemon (goat-clientd) -----------------------------------------
echo "==> [4/5] importing bundle into goat-clientd (mode=${GOAT_CLIENT_MODE})"
if command -v goat-clientd >/dev/null 2>&1; then
  # goat-client v0.2.0: --import-bundle + --headless + explicit --bundle/--socket/--config
  # confirmed against the release tag (docs/goat-client-v02-contract.md). Keep goat-clientd
  # state on the pod's LUKS volume (POD_STATE_DIR), not its default /var/lib/goat-client.
  install -d -m 0700 "${POD_STATE_DIR}/goat-client"
  goat-clientd --import-bundle "${BUNDLE}" --bundle "${POD_STATE_DIR}/goat-client/bundle.cbor"
  # Install + enable the host service templated with the profile's mode + pod state dir.
  UNIT_SRC="${POD_DIR}/host/goat-clientd.service.example"
  UNIT_DST="/etc/systemd/system/goat-clientd.service"
  GOAT_CLIENT_MODE="${GOAT_CLIENT_MODE}" POD_STATE_DIR="${POD_STATE_DIR}" \
    envsubst '${GOAT_CLIENT_MODE} ${POD_STATE_DIR}' < "${UNIT_SRC}" > "${UNIT_DST}"
  systemctl daemon-reload
  systemctl enable --now goat-clientd
  echo "    goat-clientd enabled; verify mesh with: goat doctor"
else
  echo "    skipped (goat-clientd not installed; the sandbox uses stock netbird). Bring the mesh up via"
  echo "    netbird, or install goat-clientd then re-run for the production mesh path."
fi

# --- [5/5] Render compose .env and start the data plane -----------------------------------------
echo "==> [5/5] writing compose/.env and starting the data plane"
# F-174: the audit-sink (a mode:client Zenoh peer with multicast scouting) cannot open a session
# against tls/127.0.0.1 — on network_mode:host it attempts a conflicting TLS listener on :7447
# which zenohd already owns ("Permission denied"). Point it at the router's MESH IP instead. The
# mesh IP is assigned by netbird at enrollment; resolve it here (falls back to loopback if the
# mesh client isn't up, e.g. goat-clientd-deferred installs).
MESH_IP="$(netbird status 2>/dev/null | sed -n 's/.*NetBird IP:[[:space:]]*\([0-9.]*\).*/\1/p' | head -1)"
if [ -n "${MESH_IP}" ]; then
  ZENOH_LOCAL_HOST="${MESH_IP}"
  echo "    audit-sink endpoint = mesh IP ${MESH_IP} (F-174 fix)"
else
  ZENOH_LOCAL_HOST="127.0.0.1"
  echo "    warning: mesh IP not resolved; audit-sink endpoint falls back to loopback (see F-174)"
fi
ENV_OUT="${POD_DIR}/compose/.env"
{
  echo "# generated by first-boot.sh on $(hostname) — do not edit by hand"
  echo "POD_STATE_DIR=${POD_STATE_DIR}"
  echo "PARTNER_NAMESPACE=${PARTNER_NAMESPACE}"
  echo "POD_ID=${POD_ID:-moon-pod}"
  echo "ZENOH_LOCAL_ENDPOINT=tls/${ZENOH_LOCAL_HOST}:${ZENOH_LISTEN_PORT}"
  echo "ZENOH_LOG=${ZENOH_LOG:-info}"
} > "${ENV_OUT}"
chmod 600 "${ENV_OUT}"
echo "    wrote ${ENV_OUT}"

( cd "${POD_DIR}/compose" && docker compose up -d )

echo "==> first-boot complete."
echo "    health:  GOAT_PROFILE_DIR=${GOAT_PROFILE_DIR} goat doctor"
echo "    publish: GOAT_PROFILE_DIR=${GOAT_PROFILE_DIR} goat pub ${PARTNER_NAMESPACE}/test/ping --payload-file /tmp/ping"
echo "    inbound: GOAT_PROFILE_DIR=${GOAT_PROFILE_DIR} goat sub '${INBOUND_NAMESPACE}/**' --follow"
