#!/usr/bin/env bash
# One-shot: connect the pod's fabric to the LTU sandbox (zenoh1/2/3.efdi.ltu)
# with the correct mTLS identity. Supersedes scripts/switch-fabric-ltu.sh (which
# used the wrong 'efdi' profile + only zenoh1).
#
#   identity : compose/certs/efdi-ltu/client.pem + client.key
#   trust    : compose/certs/efdi-ltu/ca.crt
#   endpoints: zenoh1 (reachable-first) -> zenoh2 -> zenoh3, by NetBird domain name
#
# Step 1 validates the LTU chain, prompts for the encrypted source key's
# passphrase, and stages a runtime-only unencrypted key + full client chain.
# Zenoh has no private-key passphrase setting and cannot use the source key
# directly. The prepared files live only in ignored runtime state.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TLS_STATE="$ROOT/compose/state/zenoh/tls"
SRC="$ROOT/compose/certs/efdi-ltu"
CHAIN_HOST="${EFDI_LTU_CHAIN_HOST:-zenoh1.efdi.ltu}"
VENDOR_PREFIX="${EFDI_LTU_VENDOR_PREFIX:-}"
HOSTS_OVERRIDE="$ROOT/compose/state/ltu-router-hosts.yml"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
umask 077

for tool in awk cmp docker getent openssl timeout; do
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

# The participant bundle contains the leaf and trusted root but not the
# intermediate. Fetch the public server chain, extract its intermediate, then
# verify that intermediate and the client leaf against our pinned LTU root.
timeout 10 openssl s_client \
  -connect "$CHAIN_HOST:7447" -servername "$CHAIN_HOST" -showcerts \
  -cert "$SRC/client.pem" -key "$TMP/client.key" \
  </dev/null >"$TMP/server-chain.txt" 2>/dev/null || true
awk '
  /-----BEGIN CERTIFICATE-----/ { count++ }
  count == 2 { print }
  /-----END CERTIFICATE-----/ && count == 2 { exit }
' "$TMP/server-chain.txt" >"$TMP/efdi-ltu-intermediate.pem"
[[ -s "$TMP/efdi-ltu-intermediate.pem" ]] || {
  echo "Could not obtain the LTU intermediate CA from $CHAIN_HOST:7447." >&2
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

echo "== step 2: stage prepared LTU material into $TLS_STATE/ltu =="
docker run --rm -v "$TLS_STATE":/tls -v "$TMP":/prepared:ro alpine:3 sh -c '
  mkdir -p /tls/ltu &&
  cp /prepared/client-chain.pem /prepared/client.key /prepared/ca.crt /tls/ltu/ &&
  chmod 600 /tls/ltu/client.key &&
  chmod 644 /tls/ltu/client-chain.pem /tls/ltu/ca.crt &&
  chown -R 10001:10001 /tls/ltu'

echo "== step 3: give the router container NetBird split-DNS mappings =="
zenoh1_ip="$(getent ahostsv4 zenoh1.efdi.ltu | awk 'NR == 1 { print $1 }')"
zenoh2_ip="$(getent ahostsv4 zenoh2.efdi.ltu | awk 'NR == 1 { print $1 }')"
zenoh3_ip="$(getent ahostsv4 zenoh3.efdi.ltu | awk 'NR == 1 { print $1 }')"
[[ "$zenoh1_ip" && "$zenoh2_ip" && "$zenoh3_ip" ]] || {
  echo "NetBird split DNS did not resolve all three LTU routers." >&2
  exit 1
}
{
  printf 'services:\n'
  printf '  zenoh-router:\n'
  printf '    extra_hosts:\n'
  printf '      - "zenoh1.efdi.ltu:%s"\n' "$zenoh1_ip"
  printf '      - "zenoh2.efdi.ltu:%s"\n' "$zenoh2_ip"
  printf '      - "zenoh3.efdi.ltu:%s"\n' "$zenoh3_ip"
} >"$HOSTS_OVERRIDE"
(
  cd "$ROOT/compose"
  docker compose -f docker-compose.yml -f state/ltu-router-hosts.yml \
    up -d --no-deps --force-recreate zenoh-router
)

echo "== step 4: apply ltu-local profile, zenoh1 reachable-first =="
docker exec -i -e EFDI_LTU_VENDOR_PREFIX="$VENDOR_PREFIX" \
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
f.fabric_endpoints = ["tls/zenoh1.efdi.ltu:7447", "tls/zenoh2.efdi.ltu:7447", "tls/zenoh3.efdi.ltu:7447"]
f.fabric_endpoint = "tls/zenoh1.efdi.ltu:7447"
f.verify_name_on_connect = True
vendor_prefix = os.environ.get("EFDI_LTU_VENDOR_PREFIX", "").strip().strip("/")
if vendor_prefix:
    f.publish_prefix = vendor_prefix
rendered = c._render_config(f)
result = c.apply_rendered_config(
    rendered,
    f,
    restart_native=bool(vendor_prefix),
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
print("APPLIED: ltu-local, zenoh1 reachable-first; remote link established" + suffix)
PY

if [[ -z "$VENDOR_PREFIX" ]]; then
  echo "NOTE: EFDI_LTU_VENDOR_PREFIX is unset; the existing data publish prefix was preserved."
  echo "Set it to the portal-assigned vendor prefix to publish into that Panoscope slot."
fi

echo "== step 5: fabric link =="
sleep 2
ss -tn 2>/dev/null | grep ':7447' || echo "(no link yet — give the router a few seconds, then: ss -tn | grep :7447)"
