#!/usr/bin/env bash
# Create this router's bounded subordinate CA. The parent/global key is read
# only for this ceremony and is never copied into router runtime state.
set -euo pipefail

if [[ $# -ne 4 ]]; then
    echo "usage: $0 <namespace> <parent-ca-cert> <parent-ca-key> <private-pki-state-directory>" >&2
    exit 2
fi

ROUTER_NAMESPACE="$1"
PARENT_CERT="$2"
PARENT_KEY="$3"
PKI_STATE_DIR="$4"
[[ "$ROUTER_NAMESPACE" =~ ^[A-Za-z0-9._/-]{1,64}$ ]] || { echo "invalid namespace" >&2; exit 2; }
[[ -f "$PARENT_CERT" && -f "$PARENT_KEY" ]] || { echo "parent CA certificate/key not found" >&2; exit 2; }
command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; exit 2; }

mkdir -p "$PKI_STATE_DIR"
chmod 700 "$PKI_STATE_DIR"
CA_KEY="$PKI_STATE_DIR/router-ca-key.pem"
CA_CERT="$PKI_STATE_DIR/router-ca-cert.pem"
CA_CHAIN="$PKI_STATE_DIR/router-ca-chain.pem"
if [[ -e "$CA_KEY" || -e "$CA_CERT" ]]; then
    echo "router CA already exists; refusing to overwrite $PKI_STATE_DIR" >&2
    exit 1
fi

TEMP_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$TEMP_DIR"
}
trap cleanup EXIT
umask 077

CA_CSR="$TEMP_DIR/router-ca.csr"
EXTENSIONS="$TEMP_DIR/extensions.cnf"
CA_COMMON_NAME="efdi-ca-$(printf '%s' "$ROUTER_NAMESPACE" | sha256sum | cut -c1-32)"
openssl ecparam -name prime256v1 -genkey -noout -out "$CA_KEY"
openssl req -new -key "$CA_KEY" -subj "/CN=${CA_COMMON_NAME}" -out "$CA_CSR"
cat > "$EXTENSIONS" <<'EOF'
basicConstraints=critical,CA:TRUE,pathlen:8
keyUsage=critical,digitalSignature,keyCertSign,cRLSign
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
EOF
openssl x509 -req -in "$CA_CSR" -CA "$PARENT_CERT" -CAkey "$PARENT_KEY" \
    -set_serial "0x$(openssl rand -hex 19)" -days 365 -sha256 \
    -extfile "$EXTENSIONS" -out "$CA_CERT"
cat "$CA_CERT" "$PARENT_CERT" > "$CA_CHAIN"
chmod 600 "$CA_KEY"
chmod 644 "$CA_CERT" "$CA_CHAIN"

openssl verify -CAfile "$PARENT_CERT" "$CA_CERT" >/dev/null
openssl x509 -in "$CA_CERT" -noout -subject -enddate
echo "Router subordinate CA initialized at $PKI_STATE_DIR"
echo "The parent/global CA key was not copied. Move it back to offline storage after this ceremony."
