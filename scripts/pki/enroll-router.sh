#!/usr/bin/env bash
# Enroll this router beneath a managed parent without transferring private keys.
set -euo pipefail

if [[ $# -lt 4 || $# -gt 5 ]]; then
    echo "usage: $0 <parent-admin-url> <namespace> <efdi-cert-directory> <private-pki-state-directory> [zenoh-runtime-state-directory]" >&2
    echo "token: set EFDI_ENROLLMENT_TOKEN or enter it at the hidden prompt" >&2
    exit 2
fi

PARENT_URL="${1%/}"
ROUTER_NAMESPACE="$2"
OUTPUT_DIR="$3"
PKI_STATE_DIR="$4"
ZENOH_STATE_DIR="${5:-$(dirname "$PKI_STATE_DIR")/zenoh}"
NAMESPACE_PREFIX="${NAMESPACE_PREFIX:-EFDI}"
EFDI_TRUST_DOMAIN="${EFDI_TRUST_DOMAIN:-efdi.global}"
[[ "$ROUTER_NAMESPACE" =~ ^[A-Za-z0-9._/-]{1,512}$ ]] || { echo "invalid namespace (maximum 512 characters)" >&2; exit 2; }
[[ "$NAMESPACE_PREFIX" =~ ^[A-Za-z0-9._/-]{1,512}$ ]] || { echo "invalid NAMESPACE_PREFIX" >&2; exit 2; }
[[ "$EFDI_TRUST_DOMAIN" =~ ^[a-z0-9.-]{1,253}$ ]] || { echo "invalid EFDI_TRUST_DOMAIN" >&2; exit 2; }
for command_name in openssl curl python3; do
    command -v "$command_name" >/dev/null 2>&1 || { echo "$command_name is required" >&2; exit 2; }
done

ENROLLMENT_TOKEN="${EFDI_ENROLLMENT_TOKEN:-}"
if [[ -z "$ENROLLMENT_TOKEN" ]]; then
    read -rsp "Enrollment token: " ENROLLMENT_TOKEN
    echo
fi
[[ -n "$ENROLLMENT_TOKEN" ]] || { echo "empty enrollment token" >&2; exit 2; }

TEMP_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT
umask 077

CA_KEY="$TEMP_DIR/router-ca-key.pem"
CA_CSR="$TEMP_DIR/router-ca.csr"
TRANSPORT_KEY="$TEMP_DIR/transport-key.pem"
TRANSPORT_CSR="$TEMP_DIR/transport.csr"
POLICY_KEY="$TEMP_DIR/policy-signer-key.pem"
POLICY_CSR="$TEMP_DIR/policy-signer.csr"
REQUEST_JSON="$TEMP_DIR/request.json"
RESPONSE_JSON="$TEMP_DIR/response.json"

MANAGED_IDENTITY="spiffe://${EFDI_TRUST_DOMAIN}/router/${NAMESPACE_PREFIX%/}/${ROUTER_NAMESPACE#/}"
CA_COMMON_NAME="efdi-ca-$(printf '%s' "$MANAGED_IDENTITY" | sha256sum | cut -c1-32)"
TRANSPORT_COMMON_NAME="efdi-router-$(printf '%s' "$MANAGED_IDENTITY" | sha256sum | cut -c1-32)"
POLICY_COMMON_NAME="efdi-policy-$(printf '%s' "$MANAGED_IDENTITY" | sha256sum | cut -c1-32)"
openssl ecparam -name prime256v1 -genkey -noout -out "$CA_KEY"
openssl req -new -key "$CA_KEY" -subj "/CN=${CA_COMMON_NAME}" -out "$CA_CSR"
openssl ecparam -name prime256v1 -genkey -noout -out "$TRANSPORT_KEY"
openssl req -new -key "$TRANSPORT_KEY" -subj "/CN=${TRANSPORT_COMMON_NAME}" \
    -addext "subjectAltName=URI:${MANAGED_IDENTITY}" -out "$TRANSPORT_CSR"
openssl ecparam -name prime256v1 -genkey -noout -out "$POLICY_KEY"
openssl req -new -key "$POLICY_KEY" -subj "/CN=${POLICY_COMMON_NAME}" -out "$POLICY_CSR"

export ENROLLMENT_TOKEN CA_CSR TRANSPORT_CSR POLICY_CSR REQUEST_JSON
python3 - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "token": os.environ["ENROLLMENT_TOKEN"],
    "router_ca_csr": Path(os.environ["CA_CSR"]).read_text(),
    "transport_csr": Path(os.environ["TRANSPORT_CSR"]).read_text(),
    "policy_signer_csr": Path(os.environ["POLICY_CSR"]).read_text(),
}
Path(os.environ["REQUEST_JSON"]).write_text(json.dumps(payload, separators=(",", ":")))
PY
unset ENROLLMENT_TOKEN

HTTP_CODE="$(curl --silent --show-error --output "$RESPONSE_JSON" --write-out '%{http_code}' \
    --header 'Content-Type: application/json' --data-binary "@$REQUEST_JSON" \
    "$PARENT_URL/api/pki/enroll")"
if [[ "$HTTP_CODE" != "200" ]]; then
    python3 - "$RESPONSE_JSON" <<'PY' >&2
import json
import sys
try:
    print(json.load(open(sys.argv[1])).get("detail", "enrollment failed"))
except Exception:
    print("enrollment failed")
PY
    exit 1
fi

mkdir -p "$OUTPUT_DIR/router-ca" "$OUTPUT_DIR/trust" "$PKI_STATE_DIR" "$ZENOH_STATE_DIR" "$(dirname "$OUTPUT_DIR/${ROUTER_NAMESPACE}-cert.pem")"
chmod 700 "$PKI_STATE_DIR" "$ZENOH_STATE_DIR"
export RESPONSE_JSON OUTPUT_DIR PKI_STATE_DIR ZENOH_STATE_DIR ROUTER_NAMESPACE
python3 - <<'PY'
import json
import os
from pathlib import Path

response = json.loads(Path(os.environ["RESPONSE_JSON"]).read_text())
output = Path(os.environ["OUTPUT_DIR"])
namespace = os.environ["ROUTER_NAMESPACE"]
leaf = output / f"{namespace}-cert.pem"
leaf.parent.mkdir(parents=True, exist_ok=True)
issuer_chain = response["issuer_chain"].rstrip() + "\n"
transport_chain = response.get("transport_issuer_chain", response["issuer_chain"]).rstrip() + "\n"
leaf.write_text(response["transport_certificate"].rstrip() + "\n" + transport_chain)
(output / "efdi-ca-root.pem").write_text(transport_chain)
(output / "router-ca" / "router-ca-cert.pem").write_text(response["router_ca_certificate"].rstrip() + "\n" + issuer_chain)
(output / "router-ca" / "router-ca-chain.pem").write_text(response["router_ca_certificate"].rstrip() + "\n" + issuer_chain)
(output / "router-ca" / "policy-signer-cert.pem").write_text(response["policy_signer_certificate"].rstrip() + "\n")
parent = response.get("parent_transport_certificate")
if parent:
    (output / "trust" / "trusted-parent-cert.pem").write_text(parent.rstrip() + "\n")
parent_policy = response.get("parent_policy_signer_certificate")
if parent_policy:
    (output / "trust" / "trusted-parent-policy.pem").write_text(parent_policy.rstrip() + "\n")
(output / "trust" / "managed-bootstrap.json").write_text(json.dumps({
    "schema": "efdi.managed-bootstrap/v2",
    "parent_namespace": response["parent_namespace"],
    "parent_authority": response["parent_authority"],
    "delegation_envelope": response["delegation_envelope"],
    "trust_chain": response["trust_chain"],
}, sort_keys=True, indent=2) + "\n")
state = Path(os.environ["PKI_STATE_DIR"])
(state / "delegation.json").write_text(json.dumps(response["delegation_envelope"], sort_keys=True, indent=2) + "\n")
(state / "enrollment.json").write_text(json.dumps({
    key: response.get(key) for key in (
        "namespace", "parent_namespace", "max_delegation_depth", "issued_serials"
    )
}, indent=2) + "\n")
link = response["link_credential"]
zenoh_state = Path(os.environ["ZENOH_STATE_DIR"])
(zenoh_state / "link-credentials.json").write_text(json.dumps({
    "parent": {"username": link["username"], "password": link["password"]},
    "children": {},
}, sort_keys=True, separators=(",", ":")) + "\n")
PY

install -m 600 "$CA_KEY" "$PKI_STATE_DIR/router-ca-key.pem"
install -m 600 "$POLICY_KEY" "$PKI_STATE_DIR/policy-signer-key.pem"
install -m 644 "$OUTPUT_DIR/router-ca/policy-signer-cert.pem" "$PKI_STATE_DIR/policy-signer-cert.pem"
install -m 600 "$TRANSPORT_KEY" "$OUTPUT_DIR/${ROUTER_NAMESPACE}-key.pem"
chgrp 10001 "$OUTPUT_DIR/${ROUTER_NAMESPACE}-key.pem" 2>/dev/null || true
chmod 640 "$OUTPUT_DIR/${ROUTER_NAMESPACE}-key.pem"
chmod 644 "$OUTPUT_DIR/${ROUTER_NAMESPACE}-cert.pem" "$OUTPUT_DIR/efdi-ca-root.pem"
find "$OUTPUT_DIR/router-ca" "$OUTPUT_DIR/trust" -type f -name '*.pem' -exec chmod 644 {} +
chmod 600 "$PKI_STATE_DIR/enrollment.json" "$PKI_STATE_DIR/delegation.json" "$ZENOH_STATE_DIR/link-credentials.json"
chmod 644 "$OUTPUT_DIR/trust/managed-bootstrap.json"

echo "Router enrollment completed."
echo "  identity: $OUTPUT_DIR/${ROUTER_NAMESPACE}-cert.pem"
echo "  router CA: $OUTPUT_DIR/router-ca/router-ca-cert.pem"
echo "  router CA private key: $PKI_STATE_DIR/router-ca-key.pem"
echo "  policy signer: $PKI_STATE_DIR/policy-signer-cert.pem"
echo "  policy signer private key: $PKI_STATE_DIR/policy-signer-key.pem"
echo "  parent link credential: $ZENOH_STATE_DIR/link-credentials.json"
echo "  trusted parent: $OUTPUT_DIR/trust/trusted-parent-cert.pem"
echo "Configure this child admin with:"
echo "  ZENOH_ADMIN_TRUSTED_PARENT_HOST_CERT_PATH=$OUTPUT_DIR/trust/trusted-parent-cert.pem"
echo "  ZENOH_ADMIN_TRUSTED_PARENT_CN=<parent router certificate CN>"
echo "Configure this host control agent with the absolute host paths to router-ca/router-ca-cert.pem, $PKI_STATE_DIR/router-ca-key.pem, and router-ca/router-ca-chain.pem."
