#!/usr/bin/env bash
# EFDI moon-pod — self-issued mTLS certs for the Zenoh fabric.
#
# No external CA, no vendor bundle/portal: this script generates (once) an EFDI root CA at
# compose/certs/efdi-ca-root.pem + compose/certs/efdi-ca-root-key.pem (gitignored, keep this
# key safe — it signs every pod's leaf cert), then signs a leaf cert+key for the given
# namespace. Re-running for a new namespace reuses the existing root CA; it is only generated
# once.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="${BUNDLE_DIR:-${SCRIPT_DIR}/compose/certs}"
NAMESPACE="${1:-}"

usage() {
  echo "usage: $0 <namespace>   e.g. $0 release/example-partner"
  exit 2
}
[ -n "$NAMESPACE" ] || usage
command -v openssl >/dev/null 2>&1 || { echo "openssl not found on PATH"; exit 2; }

mkdir -p "$CERT_DIR"

CA_CERT="${CERT_DIR}/efdi-ca-root.pem"
CA_KEY="${CERT_DIR}/efdi-ca-root-key.pem"

if [ -f "$CA_CERT" ] && [ -f "$CA_KEY" ]; then
  echo "==> reusing existing EFDI root CA at ${CA_CERT}"
else
  echo "==> [1/2] generating EFDI root CA (once — reused for every pod namespace after this)"
  openssl ecparam -name prime256v1 -genkey -noout -out "$CA_KEY"
  openssl req -x509 -new -key "$CA_KEY" -sha256 -days 3650 \
    -subj "/O=EFDI/CN=efdi-root-ca" -out "$CA_CERT"
  chmod 600 "$CA_KEY"
fi

# Namespace can contain slashes (e.g. release/<partner>) — the bridges build the cert path as
# os.path.join(EFDI_CERT_DIR, PARTNER_NAMESPACE + "-cert.pem"), so a slash in the namespace means
# a real subdirectory, not a flattened filename. Keep that exact layout here.
LEAF_CERT="${CERT_DIR}/${NAMESPACE}-cert.pem"
LEAF_KEY="${CERT_DIR}/${NAMESPACE}-key.pem"
mkdir -p "$(dirname "$LEAF_CERT")"
CSR="$(mktemp)"

echo "==> [2/2] issuing leaf cert for namespace '${NAMESPACE}'"
openssl ecparam -name prime256v1 -genkey -noout -out "$LEAF_KEY"
openssl req -new -key "$LEAF_KEY" -subj "/O=EFDI/CN=${NAMESPACE}" -out "$CSR"
openssl x509 -req -in "$CSR" -CA "$CA_CERT" -CAkey "$CA_KEY" -CAcreateserial \
  -days 825 -sha256 -out "$LEAF_CERT"
rm -f "$CSR"
chmod 600 "$LEAF_KEY"

echo "==> done:"
echo "    CA root:  ${CA_CERT}"
echo "    leaf cert: ${LEAF_CERT}"
echo "    leaf key:  ${LEAF_KEY}"
echo "    set PARTNER_NAMESPACE=${NAMESPACE} in compose/.env to match"
