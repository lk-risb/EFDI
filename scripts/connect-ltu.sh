#!/usr/bin/env bash
# One-shot: connect the pod to an LTU fabric using its issued mTLS identity.
#
#   identity : compose/certs/efdi-ltu/client.pem + client.key
#   trust    : compose/certs/efdi-ltu/ca.crt
#   endpoints: supplied through EFDI_LTU_FABRIC_ENDPOINTS as a whitespace-
#              separated list of tls/host:port values
#   aliases  : optional EFDI_LTU_FABRIC_HOST_ALIASES entries map certificate
#              DNS names to resolvable mesh DNS names (cert-host=mesh-host)
#   slot     : optional EFDI_LTU_PARTNER_NAMESPACE selects the exact LTU data
#              slot and local management namespace; no legacy prefix is added
#
# Step 1 validates the LTU chain, prompts for the encrypted source key's
# passphrase, and stages a runtime-only unencrypted key + full client chain.
# Zenoh has no private-key passphrase setting and cannot use the source key
# directly. The prepared files live only in ignored runtime state.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TLS_STATE="$ROOT/compose/state/zenoh/tls"
SRC="$ROOT/compose/certs/efdi-ltu"
CHAIN_HOST="${EFDI_LTU_CHAIN_HOST:-}"
VENDOR_PREFIX="${EFDI_LTU_VENDOR_PREFIX:-}"
PARTNER_NAMESPACE="${EFDI_LTU_PARTNER_NAMESPACE:-}"
ENDPOINTS_RAW="${EFDI_LTU_FABRIC_ENDPOINTS:-}"
ALIASES_RAW="${EFDI_LTU_FABRIC_HOST_ALIASES:-}"
HOSTS_OVERRIDE="$ROOT/compose/state/ltu-router-hosts.yml"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
umask 077

[[ -n "$ENDPOINTS_RAW" ]] || {
  echo "Set EFDI_LTU_FABRIC_ENDPOINTS to the issued router endpoints." >&2
  echo "Example: EFDI_LTU_FABRIC_ENDPOINTS='tls/router-a.example:7447 tls/router-b.example:7447'" >&2
  exit 1
}
read -r -a FABRIC_ENDPOINTS <<<"$ENDPOINTS_RAW"
(( ${#FABRIC_ENDPOINTS[@]} > 0 )) || {
  echo "EFDI_LTU_FABRIC_ENDPOINTS does not contain an endpoint." >&2
  exit 1
}
declare -A HOST_ALIASES=()
for mapping in $ALIASES_RAW; do
  [[ "$mapping" == *=* && "$mapping" != =* && "$mapping" != *= ]] || {
    echo "Invalid EFDI_LTU_FABRIC_HOST_ALIASES entry: $mapping" >&2
    echo "Expected whitespace-separated cert-host=mesh-host mappings." >&2
    exit 1
  }
  HOST_ALIASES["${mapping%%=*}"]="${mapping#*=}"
done
if [[ -z "${EFDI_LTU_CHAIN_HOST:-}" ]]; then
  CHAIN_HOST="${FABRIC_ENDPOINTS[0]#tls/}"
  CHAIN_HOST="${CHAIN_HOST%:*}"
fi

for tool in awk cmp date docker getent openssl timeout; do
  command -v "$tool" >/dev/null || {
    echo "Required command is not installed: $tool" >&2
    exit 1
  }
done

for required in client.pem client.key ca.crt; do
  [[ -f "$SRC/$required" ]] || {
    echo "Missing LTU credential: $SRC/$required" >&2
    exit 1
  }
done

echo "== step 1: prepare LTU client chain and runtime key =="
not_before="$(openssl x509 -in "$SRC/client.pem" -noout -startdate)"
not_before="${not_before#notBefore=}"
not_before_epoch="$(date -u -d "$not_before" +%s 2>/dev/null || true)"
now_epoch="$(date -u +%s)"
if [[ -n "$not_before_epoch" ]] && (( now_epoch < not_before_epoch )); then
  echo "The LTU client certificate is not valid yet." >&2
  echo "Certificate valid from: $not_before" >&2
  echo "Current UTC time:       $(date -u '+%b %e %H:%M:%S %Y GMT')" >&2
  echo "Wait until the validity time or ask the issuer to correct its signing clock." >&2
  exit 1
fi
if ! openssl x509 -in "$SRC/client.pem" -noout -checkend 0 >/dev/null; then
  echo "The LTU client certificate has expired." >&2
  exit 1
fi
if openssl pkey -in "$SRC/client.key" -passin pass: -noout >/dev/null 2>&1; then
  openssl pkey -in "$SRC/client.key" -passin pass: \
    -out "$TMP/client.key" >/dev/null
else
  [[ -t 0 ]] || {
    echo "The LTU private key is encrypted; rerun this script in a terminal." >&2
    exit 1
  }
  read -r -s -p "LTU client-key passphrase: " key_passphrase
  echo
  if ! openssl pkey -in "$SRC/client.key" -passin fd:3 \
      -out "$TMP/client.key" 3<<<"$key_passphrase" >/dev/null; then
    unset key_passphrase
    echo "Could not decrypt the LTU private key (wrong passphrase?)." >&2
    exit 1
  fi
  unset key_passphrase
fi

# Prefer a previously verified runtime intermediate during certificate
# rollover. A remote server may reject the expired client before it sends its
# full chain, which otherwise makes replacing that client certificate
# impossible. Fresh installations still obtain the intermediate from the
# public server chain.
if [[ -f "$TLS_STATE/ltu/client-chain.pem" ]]; then
  awk '
    /-----BEGIN CERTIFICATE-----/ { count++ }
    count == 2 { print }
    /-----END CERTIFICATE-----/ && count == 2 { exit }
  ' "$TLS_STATE/ltu/client-chain.pem" >"$TMP/efdi-ltu-intermediate.pem"
fi
if ! openssl verify -CAfile "$SRC/ca.crt" \
    "$TMP/efdi-ltu-intermediate.pem" >/dev/null 2>&1 ||
    ! openssl verify -CAfile "$SRC/ca.crt" \
      -untrusted "$TMP/efdi-ltu-intermediate.pem" \
      "$SRC/client.pem" >/dev/null 2>&1; then
  chain_connect_host="${HOST_ALIASES[$CHAIN_HOST]:-$CHAIN_HOST}"
  timeout 10 openssl s_client \
    -connect "$chain_connect_host:7447" -servername "$CHAIN_HOST" -showcerts \
    -cert "$SRC/client.pem" -key "$TMP/client.key" \
    </dev/null >"$TMP/server-chain.txt" 2>/dev/null || true
  awk '
    /-----BEGIN CERTIFICATE-----/ { count++ }
    count == 2 { print }
    /-----END CERTIFICATE-----/ && count == 2 { exit }
  ' "$TMP/server-chain.txt" >"$TMP/efdi-ltu-intermediate.pem"
fi
[[ -s "$TMP/efdi-ltu-intermediate.pem" ]] || {
  echo "Could not obtain the LTU intermediate CA locally or from $CHAIN_HOST:7447." >&2
  exit 1
}
openssl verify -CAfile "$SRC/ca.crt" \
  "$TMP/efdi-ltu-intermediate.pem" >/dev/null
openssl verify -CAfile "$SRC/ca.crt" \
  -untrusted "$TMP/efdi-ltu-intermediate.pem" "$SRC/client.pem" >/dev/null
cmp -s \
  <(openssl pkey -in "$TMP/client.key" -pubout 2>/dev/null) \
  <(openssl x509 -in "$SRC/client.pem" -pubkey -noout 2>/dev/null) || {
    echo "The decrypted key does not match the LTU client certificate." >&2
    exit 1
  }
cp "$SRC/client.pem" "$TMP/client-chain.pem"
awk '
  /-----BEGIN CERTIFICATE-----/ { emit = 1 }
  emit { print }
  /-----END CERTIFICATE-----/ { exit }
' "$TMP/efdi-ltu-intermediate.pem" >>"$TMP/client-chain.pem"
cp "$SRC/ca.crt" "$TMP/ca.crt"
cat "$TMP/efdi-ltu-intermediate.pem" >>"$TMP/ca.crt"

echo "== step 2: stage prepared LTU material into $TLS_STATE/ltu =="
docker run --rm -v "$TLS_STATE":/tls -v "$TMP":/prepared:ro alpine:3 sh -c '
  mkdir -p /tls/ltu &&
  cp /prepared/client-chain.pem /prepared/client.key /prepared/ca.crt /tls/ltu/ &&
  chmod 600 /tls/ltu/client.key &&
  chmod 644 /tls/ltu/client-chain.pem /tls/ltu/ca.crt &&
  chown -R 10001:10001 /tls/ltu'

echo "== step 3: resolve mesh DNS names for the router container =="
resolved_hosts=()
for endpoint in "${FABRIC_ENDPOINTS[@]}"; do
  host="${endpoint#tls/}"
  host="${host%:*}"
  resolve_host="${HOST_ALIASES[$host]:-$host}"
  if [[ "$resolve_host" =~ ^[0-9.]+$ ]]; then
    ip="$resolve_host"
  else
    ip="$(getent ahostsv4 "$resolve_host" | awk 'NR == 1 { print $1 }')"
  fi
  [[ -n "$ip" ]] || {
    echo "Could not resolve fabric endpoint host: $resolve_host" >&2
    exit 1
  }
  resolved_hosts+=("$host:$ip")
done
{
  printf 'services:\n'
  for service in zenoh-router zenoh-admin; do
    printf '  %s:\n' "$service"
    printf '    extra_hosts:\n'
    for host_mapping in "${resolved_hosts[@]}"; do
      printf '      - "%s"\n' "$host_mapping"
    done
  done
} >"$HOSTS_OVERRIDE"
(
  cd "$ROOT/compose"
  docker compose -f docker-compose.yml -f state/ltu-router-hosts.yml \
    up -d --no-deps --force-recreate zenoh-router zenoh-admin
)

echo "== step 4: apply the LTU profile and configured endpoints =="
docker exec -i \
  -e EFDI_LTU_VENDOR_PREFIX="$VENDOR_PREFIX" \
  -e EFDI_LTU_PARTNER_NAMESPACE="$PARTNER_NAMESPACE" \
  -e EFDI_LTU_FABRIC_ENDPOINTS="$ENDPOINTS_RAW" \
  efdi-pod-zenoh-admin python3 - <<'PY'
import os
import api.config as c
c._TLS_PROFILES.setdefault("ltu-local", {
    "label": "LTU sandbox (EFDI LTU CA)",
    "publish_cert_dir": "efdi-ltu",
    "publish_root_ca": "ca.crt",
    "publish_client_cert": "client.pem",
    "publish_client_key": "client.key",
    "listen_certificate": "/etc/zenoh/tls/ltu/client-chain.pem",
    "listen_private_key": "/etc/zenoh/tls/ltu/client.key",
    "connect_certificate": "/etc/zenoh/tls/ltu/client-chain.pem",
    "connect_private_key": "/etc/zenoh/tls/ltu/client.key",
    "root_ca": "/etc/zenoh/tls/ltu/ca.crt",
})
raw = open(c.CONFIG_PATH).read()
try:
    f = c._extract_fields(raw)
except ValueError as exc:
    if "unknown fabric TLS profile" not in str(exc):
        raise
    # A previous release may have rendered machine-specific runtime filenames.
    # Normalize only the server-owned TLS path fields, then parse the remaining
    # user configuration normally.
    migrated = c.json5.loads(raw)
    tls = migrated["transport"]["link"]["tls"]
    profile = c._TLS_PROFILES["ltu-local"]
    tls["listen_certificate"] = profile["listen_certificate"]
    tls["listen_private_key"] = profile["listen_private_key"]
    tls["connect_certificate"] = profile["connect_certificate"]
    tls["connect_private_key"] = profile["connect_private_key"]
    tls["root_ca_certificate"] = profile["root_ca"]
    raw = c.json5.dumps(migrated)
    f = c._extract_fields(raw)
f.fabric_tls_profile = "ltu-local"
endpoints = os.environ["EFDI_LTU_FABRIC_ENDPOINTS"].split()
f.fabric_endpoints = endpoints
f.fabric_endpoint = endpoints[0]
f.verify_name_on_connect = True
vendor_prefix = os.environ.get("EFDI_LTU_VENDOR_PREFIX", "").strip().strip("/")
partner_namespace = os.environ.get(
    "EFDI_LTU_PARTNER_NAMESPACE", ""
).strip().strip("/")
if partner_namespace:
    f.partner_namespace = partner_namespace
    f.publish_prefix = ""
if vendor_prefix:
    f.publish_prefix = vendor_prefix
rendered = c._render_config(f)
result = c.apply_rendered_config(
    rendered,
    f,
    restart_native=bool(vendor_prefix or partner_namespace),
    preserve_management=True,
)
if result["status"] != "applied":
    raise SystemExit(
        f"{result['status'].upper()}: "
        f"{result.get('error') or 'LTU configuration did not activate'}"
    )
if result["native_process_restart_failures"]:
    raise SystemExit(
        "APPLIED, but native process restarts failed: "
        + "; ".join(result["native_process_restart_failures"])
    )
suffix = f"; publish slot {vendor_prefix}/**" if vendor_prefix else ""
if partner_namespace:
    suffix = f"; publish slot {partner_namespace}/**"
print("APPLIED: ltu-local; remote link established" + suffix)
PY

if [[ -z "$VENDOR_PREFIX" && -z "$PARTNER_NAMESPACE" ]]; then
  echo "NOTE: EFDI_LTU_VENDOR_PREFIX is unset; the existing data publish prefix was preserved."
  echo "Set EFDI_LTU_PARTNER_NAMESPACE to the LTU-issued slot ID for an exact <slot-id>/** root."
fi

echo "== step 5: fabric link =="
sleep 2
ss -tn 2>/dev/null | grep ':7447' || echo "(no link yet — give the router a few seconds, then: ss -tn | grep :7447)"
