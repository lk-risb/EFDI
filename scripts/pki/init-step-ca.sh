#!/usr/bin/env bash
# Initialize an online leaf-issuing CA beneath this router's bounded CA.
set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "usage: $0 <router-ca-cert> <router-ca-key> <step-ca-state-directory> [dns-name-or-ip ...]" >&2
    exit 2
fi

CA_CERT=$1
CA_KEY=$2
STATE_DIR=$3
shift 3
DNS_NAMES=(localhost "$@")
IMAGE=${EFDI_STEP_CA_IMAGE:-smallstep/step-ca:0.30.2@sha256:a2b17872915c193259b75a5474c398326f41bd199f0842093e52cf4182bc8270}
[[ -f "$CA_CERT" && -f "$CA_KEY" ]] || { echo "router CA certificate/key not found" >&2; exit 2; }
for dns_name in "${DNS_NAMES[@]}"; do
    [[ "$dns_name" =~ ^[A-Za-z0-9.-]{1,253}$ ]] || { echo "invalid CA DNS name or IP" >&2; exit 2; }
done
[[ ! -e "$STATE_DIR/config/ca.json" ]] || { echo "step-ca is already initialized" >&2; exit 1; }

mkdir -p "$STATE_DIR/secrets"
chmod 700 "$STATE_DIR" "$STATE_DIR/secrets"
CA_CERT=$(readlink -f "$CA_CERT")
CA_KEY=$(readlink -f "$CA_KEY")
STATE_DIR=$(readlink -f "$STATE_DIR")
umask 077
openssl rand -base64 48 >"$STATE_DIR/secrets/password"
openssl rand -base64 48 >"$STATE_DIR/secrets/provisioner-password"

dns_args=()
for dns_name in "${DNS_NAMES[@]}"; do dns_args+=(--dns "$dns_name"); done
docker run --rm --user "$(id -u):$(id -g)" --entrypoint step \
    -e STEPPATH=/home/step \
    -v "$STATE_DIR:/home/step" \
    -v "$CA_CERT:/input/router-ca.pem:ro" \
    -v "$CA_KEY:/input/router-ca-key.pem:ro" \
    "$IMAGE" ca init \
    --deployment-type standalone \
    --name "EFDI managed router CA" \
    "${dns_args[@]}" \
    --address ":${EFDI_STEP_CA_PORT:-9000}" \
    --provisioner efdi-router \
    --root /input/router-ca.pem \
    --key /input/router-ca-key.pem \
    --password-file /home/step/secrets/password \
    --provisioner-password-file /home/step/secrets/provisioner-password

# step-ca needs only its generated intermediate key. Remove any copied router
# CA key immediately; the bounded router CA remains behind host control.
if [[ -f "$STATE_DIR/secrets/root_ca_key" ]]; then
    shred -u "$STATE_DIR/secrets/root_ca_key"
fi
chmod 600 "$STATE_DIR/secrets/"*
echo "step-ca initialized at $STATE_DIR; start the managed-ca Compose profile"
