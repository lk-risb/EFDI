#!/usr/bin/env bash
# Renew step-ca transport identities without requiring step-cli on the host.
set -euo pipefail

DAEMON=0
if [[ "${1:-}" == "--daemon" ]]; then
    DAEMON=1
    shift
fi
if [[ $# -lt 3 ]]; then
    echo "usage: $0 [--daemon] <ca-url> <root-cert> <cert:key> [cert:key ...]" >&2
    exit 2
fi

CA_URL=$1
ROOT_CERT=$2
shift 2
IMAGE=${EFDI_STEP_CA_IMAGE:-smallstep/step-ca:0.30.2@sha256:a2b17872915c193259b75a5474c398326f41bd199f0842093e52cf4182bc8270}
INTERVAL=${EFDI_STEP_RENEW_CHECK_SECONDS:-900}
RENEW_BEFORE=${EFDI_STEP_RENEW_BEFORE_SECONDS:-28800}
[[ "$CA_URL" == https://* ]] || { echo "CA URL must use HTTPS" >&2; exit 2; }
[[ -f "$ROOT_CERT" ]] || { echo "root certificate not found" >&2; exit 2; }
[[ "$INTERVAL" =~ ^[0-9]+$ && "$INTERVAL" -ge 60 ]] || { echo "renew check interval must be at least 60 seconds" >&2; exit 2; }
[[ "$RENEW_BEFORE" =~ ^[0-9]+$ && "$RENEW_BEFORE" -ge 300 ]] || { echo "renew-before interval must be at least 300 seconds" >&2; exit 2; }

renew_once() {
    local changed=0 pair cert key cert_dir key_dir before after renewed_cert=""
    for pair in "$@"; do
        cert=${pair%%:*}
        key=${pair#*:}
        [[ "$cert" != "$key" && -f "$cert" && -f "$key" ]] || { echo "invalid cert:key pair" >&2; return 2; }
        cert=$(readlink -f "$cert")
        key=$(readlink -f "$key")
        if openssl x509 -checkend "$RENEW_BEFORE" -noout -in "$cert" >/dev/null; then
            continue
        fi
        cert_dir=$(dirname "$cert")
        key_dir=$(dirname "$key")
        before=$(sha256sum "$cert" | cut -d' ' -f1)
        docker run --rm --network host --user "$(id -u):$(id -g)" --entrypoint step \
            -v "$(readlink -f "$ROOT_CERT"):/work/root.pem:ro" \
            -v "$cert_dir:/work/certs" \
            -v "$key_dir:/work/keys:ro" \
            "$IMAGE" ca renew --force \
            --ca-url "$CA_URL" --root /work/root.pem \
            "/work/certs/$(basename "$cert")" "/work/keys/$(basename "$key")"
        after=$(sha256sum "$cert" | cut -d' ' -f1)
        if [[ "$before" != "$after" ]]; then
            changed=1
            renewed_cert=$cert
        fi
    done
    if [[ "$changed" == "1" ]]; then
        if [[ -n "${EFDI_STEP_RENEW_RUNTIME_CERT_PATH:-}" ]]; then
            install -m 600 "$renewed_cert" "$EFDI_STEP_RENEW_RUNTIME_CERT_PATH"
        fi
        docker restart "${ZENOH_ROUTER_CONTAINER:-efdi-pod-zenoh-router}" >/dev/null
        docker restart "${ZENOH_ADMIN_CONTAINER:-efdi-pod-zenoh-admin}" >/dev/null 2>&1 || true
        echo "renewed transport identity and restarted certificate consumers"
    fi
}

if [[ "$DAEMON" == "0" ]]; then
    renew_once "$@"
    exit
fi

while true; do
    renew_once "$@" || echo "certificate renewal check failed; retrying" >&2
    sleep "$INTERVAL"
done
