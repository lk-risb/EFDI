#!/usr/bin/env bash
# Create the router's non-CA policy-signing identity from its local router CA.
set -euo pipefail

if [[ $# -ne 4 ]]; then
    echo "usage: $0 <full-managed-namespace> <router-ca-cert> <router-ca-key> <private-pki-state-directory>" >&2
    exit 2
fi

NAMESPACE_PATH="${1#/}"
CA_CERT="$2"
CA_KEY="$3"
STATE_DIR="$4"
TRUST_DOMAIN="${EFDI_TRUST_DOMAIN:-efdi.global}"
[[ "$NAMESPACE_PATH" =~ ^[A-Za-z0-9._/-]{1,512}$ && "$NAMESPACE_PATH" != *//* ]] || { echo "invalid namespace" >&2; exit 2; }
[[ "$TRUST_DOMAIN" =~ ^[a-z0-9.-]{1,253}$ ]] || { echo "invalid trust domain" >&2; exit 2; }
[[ -f "$CA_CERT" && -f "$CA_KEY" ]] || { echo "router CA certificate/key not found" >&2; exit 2; }
for command_name in openssl docker readlink; do
    command -v "$command_name" >/dev/null 2>&1 || { echo "$command_name is required" >&2; exit 2; }
done

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
KEY="$STATE_DIR/policy-signer-key.pem"
CERT="$STATE_DIR/policy-signer-cert.pem"
if [[ -e "$KEY" || -e "$CERT" ]]; then
    echo "policy signer already exists; refusing to overwrite $STATE_DIR" >&2
    exit 1
fi

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT
umask 077
CSR="$TEMP_DIR/policy.csr"
EXT="$TEMP_DIR/policy.cnf"
IDENTITY_URI="spiffe://${TRUST_DOMAIN}/router/${NAMESPACE_PATH}"
COMMON_NAME="efdi-policy-$(printf '%s' "$IDENTITY_URI" | sha256sum | cut -c1-32)"

openssl ecparam -name prime256v1 -genkey -noout -out "$KEY"
openssl req -new -key "$KEY" -subj "/CN=${COMMON_NAME}" -out "$CSR"
cat >"$EXT" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=1.3.6.1.4.1.55555.1.1
subjectAltName=URI:${IDENTITY_URI}
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
openssl x509 -req -in "$CSR" -CA "$CA_CERT" -CAkey "$CA_KEY" \
    -set_serial "0x$(openssl rand -hex 19)" -days 30 -sha256 \
    -extfile "$EXT" -out "$CERT"
if ! chgrp 10001 "$KEY" 2>/dev/null; then
    STEP_IMAGE=${EFDI_STEP_CA_IMAGE:-smallstep/step-ca:0.30.2@sha256:a2b17872915c193259b75a5474c398326f41bd199f0842093e52cf4182bc8270}
    docker run --rm --user root --entrypoint chgrp \
        -v "$(readlink -f "$STATE_DIR"):/state" "$STEP_IMAGE" \
        10001 /state/policy-signer-key.pem
fi
chmod 640 "$KEY"
chmod 644 "$CERT"
openssl verify -partial_chain -CAfile "$CA_CERT" "$CERT" >/dev/null
openssl x509 -in "$CERT" -noout -subject -enddate
echo "Policy signer initialized at $STATE_DIR"
