#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
IMAGE=${ZENOH_IMAGE:-eclipse/zenoh:1.9.0}
PYTHON=${EFDI_ZENOH_PYTHON:-$ROOT/compose/venv/bin/python3}
TEST_DIR=$(mktemp -d)
NETWORK="efdi-managed-three-$PPID"
ROOT_CONTAINER="efdi-managed-root-$PPID"
CHILD_CONTAINER="efdi-managed-child-$PPID"
GRAND_CONTAINER="efdi-managed-grand-$PPID"

cleanup() {
    docker rm -f "$ROOT_CONTAINER" "$CHILD_CONTAINER" "$GRAND_CONTAINER" >/dev/null 2>&1 || true
    docker network rm "$NETWORK" >/dev/null 2>&1 || true
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT INT TERM

fail() { echo "FAIL: $*" >&2; exit 1; }
for command in docker openssl; do command -v "$command" >/dev/null || fail "$command is required"; done
[[ -x "$PYTHON" ]] || PYTHON=python3
"$PYTHON" -c 'import zenoh' 2>/dev/null || fail "eclipse-zenoh Python package is required"
docker image inspect "$IMAGE" >/dev/null 2>&1 || fail "$IMAGE is not available locally"

free_port() {
    "$PYTHON" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'
}
ROOT_PORT=$(free_port)
CHILD_PORT=$(free_port)
GRAND_PORT=$(free_port)

openssl ecparam -name prime256v1 -genkey -noout -out "$TEST_DIR/ca-key.pem"
openssl req -x509 -new -sha256 -days 1 -key "$TEST_DIR/ca-key.pem" \
    -subj /CN=EFDI-managed-matrix-CA \
    -addext basicConstraints=critical,CA:TRUE,pathlen:1 \
    -addext keyUsage=critical,keyCertSign,cRLSign,digitalSignature \
    -out "$TEST_DIR/ca.pem"

issue_router() {
    local name=$1 common_name=$2
    local serial_args=(-CAserial "$TEST_DIR/ca.srl")
    [[ -f "$TEST_DIR/ca.srl" ]] || serial_args=(-CAcreateserial -CAserial "$TEST_DIR/ca.srl")
    openssl ecparam -name prime256v1 -genkey -noout -out "$TEST_DIR/$name-key.pem"
    openssl req -new -sha256 -key "$TEST_DIR/$name-key.pem" -subj "/CN=$common_name" -out "$TEST_DIR/$name.csr"
    openssl x509 -req -sha256 -days 1 -in "$TEST_DIR/$name.csr" \
        -CA "$TEST_DIR/ca.pem" -CAkey "$TEST_DIR/ca-key.pem" "${serial_args[@]}" \
        -extfile <(printf '%s\n' \
            'basicConstraints=critical,CA:FALSE' \
            'keyUsage=critical,digitalSignature' \
            'extendedKeyUsage=serverAuth,clientAuth') \
        -out "$TEST_DIR/$name.pem"
}
issue_router root router-root
issue_router child router-child
issue_router grand router-grand
printf 'child-link:child-secret\nlocal:local-secret\n' >"$TEST_DIR/root-users.txt"
printf 'grand-link:grand-secret\nlocal:local-secret\n' >"$TEST_DIR/child-users.txt"
printf 'local:local-secret\n' >"$TEST_DIR/grand-users.txt"

cat >"$TEST_DIR/root.json5" <<'EOF'
{
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447", "tcp/0.0.0.0:7448"] },
  scouting: { multicast: { enabled: false }, gossip: { enabled: false } },
  transport: {
    link: { tls: {
      root_ca_certificate: "/test/ca.pem", listen_certificate: "/test/root.pem",
      listen_private_key: "/test/root-key.pem", enable_mtls: true,
      verify_name_on_connect: false, close_link_on_expiration: true,
    } },
    auth: { usrpwd: { dictionary_file: "/test/root-users.txt" } },
  },
  access_control: {
    enabled: true, default_permission: "deny",
    rules: [
      { id: "local", messages: ["put","delete","declare_subscriber","query","reply"], flows: ["ingress","egress"], permission: "allow", key_exprs: ["**"] },
      { id: "child-in", messages: ["put","delete","declare_subscriber","query","reply"], flows: ["ingress"], permission: "allow", key_exprs: ["EFDI/child/**"] },
      { id: "child-out", messages: ["put","reply","declare_subscriber","query"], flows: ["egress"], permission: "allow", key_exprs: ["EFDI/**"] },
    ],
    subjects: [
      { id: "local-tcp", link_protocols: ["tcp"] },
      { id: "child", cert_common_names: ["router-child"], usernames: ["child-link"] },
    ],
    policies: [
      { id: "local-policy", rules: ["local"], subjects: ["local-tcp"] },
      { id: "child-policy", rules: ["child-in","child-out"], subjects: ["child"] },
    ],
  },
}
EOF

cat >"$TEST_DIR/child.json5" <<'EOF'
{
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447", "tcp/0.0.0.0:7448"] },
  connect: { endpoints: ["tls/root:7447"] },
  scouting: { multicast: { enabled: false }, gossip: { enabled: false } },
  transport: {
    link: { tls: {
      root_ca_certificate: "/test/ca.pem", listen_certificate: "/test/child.pem",
      listen_private_key: "/test/child-key.pem", connect_certificate: "/test/child.pem",
      connect_private_key: "/test/child-key.pem", enable_mtls: true,
      verify_name_on_connect: false, close_link_on_expiration: true,
    } },
    auth: { usrpwd: { user: "child-link", password: "child-secret", dictionary_file: "/test/child-users.txt" } },
  },
  access_control: {
    enabled: true, default_permission: "deny",
    rules: [
      { id: "local", messages: ["put","delete","declare_subscriber","query","reply"], flows: ["ingress","egress"], permission: "allow", key_exprs: ["**"] },
      { id: "grand-in", messages: ["put","delete","declare_subscriber","query","reply"], flows: ["ingress"], permission: "allow", key_exprs: ["EFDI/child/grand/**"] },
      { id: "grand-out", messages: ["put","reply","declare_subscriber","query"], flows: ["egress"], permission: "allow", key_exprs: ["EFDI/child/**"] },
      { id: "parent-in", messages: ["put","reply","declare_subscriber","query"], flows: ["ingress"], permission: "allow", key_exprs: ["EFDI/**"] },
      { id: "parent-out", messages: ["put","delete","declare_subscriber","query","reply"], flows: ["egress"], permission: "allow", key_exprs: ["EFDI/child/**"] },
    ],
    subjects: [
      { id: "local-tcp", link_protocols: ["tcp"] },
      { id: "grand", cert_common_names: ["router-grand"], usernames: ["grand-link"] },
      { id: "parent", cert_common_names: ["router-root"] },
    ],
    policies: [
      { id: "local-policy", rules: ["local"], subjects: ["local-tcp"] },
      { id: "grand-policy", rules: ["grand-in","grand-out"], subjects: ["grand"] },
      { id: "parent-policy", rules: ["parent-in","parent-out"], subjects: ["parent"] },
    ],
  },
}
EOF

cat >"$TEST_DIR/grand.json5" <<'EOF'
{
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:7447", "tcp/0.0.0.0:7448"] },
  connect: { endpoints: ["tls/child:7447"] },
  scouting: { multicast: { enabled: false }, gossip: { enabled: false } },
  transport: {
    link: { tls: {
      root_ca_certificate: "/test/ca.pem", listen_certificate: "/test/grand.pem",
      listen_private_key: "/test/grand-key.pem", connect_certificate: "/test/grand.pem",
      connect_private_key: "/test/grand-key.pem", enable_mtls: true,
      verify_name_on_connect: false, close_link_on_expiration: true,
    } },
    auth: { usrpwd: { user: "grand-link", password: "grand-secret", dictionary_file: "/test/grand-users.txt" } },
  },
  access_control: {
    enabled: true, default_permission: "deny",
    rules: [
      { id: "local", messages: ["put","delete","declare_subscriber","query","reply"], flows: ["ingress","egress"], permission: "allow", key_exprs: ["**"] },
      { id: "parent-in", messages: ["put","reply","declare_subscriber","query"], flows: ["ingress"], permission: "allow", key_exprs: ["EFDI/child/**"] },
      { id: "parent-out", messages: ["put","delete","declare_subscriber","query","reply"], flows: ["egress"], permission: "allow", key_exprs: ["EFDI/child/grand/**"] },
    ],
    subjects: [
      { id: "local-tcp", link_protocols: ["tcp"] },
      { id: "parent", cert_common_names: ["router-child"] },
    ],
    policies: [
      { id: "local-policy", rules: ["local"], subjects: ["local-tcp"] },
      { id: "parent-policy", rules: ["parent-in","parent-out"], subjects: ["parent"] },
    ],
  },
}
EOF

docker network create "$NETWORK" >/dev/null
docker run -d --name "$ROOT_CONTAINER" --network "$NETWORK" --network-alias root \
    -p "127.0.0.1:$ROOT_PORT:7448" -v "$TEST_DIR:/test:ro" "$IMAGE" -c /test/root.json5 >/dev/null
docker run -d --name "$CHILD_CONTAINER" --network "$NETWORK" --network-alias child \
    -p "127.0.0.1:$CHILD_PORT:7448" -v "$TEST_DIR:/test:ro" "$IMAGE" -c /test/child.json5 >/dev/null
docker run -d --name "$GRAND_CONTAINER" --network "$NETWORK" --network-alias grand \
    -p "127.0.0.1:$GRAND_PORT:7448" -v "$TEST_DIR:/test:ro" "$IMAGE" -c /test/grand.json5 >/dev/null

for item in "$ROOT_CONTAINER:$ROOT_PORT" "$CHILD_CONTAINER:$CHILD_PORT" "$GRAND_CONTAINER:$GRAND_PORT"; do
    container=${item%%:*}
    port=${item#*:}
    ready=0
    for _ in $(seq 1 80); do
        if "$PYTHON" -c "import socket; s=socket.create_connection(('127.0.0.1',$port),.2); s.close()" >/dev/null 2>&1; then
            ready=1
            break
        fi
        sleep 0.1
    done
    if [[ "$ready" == "0" ]]; then
        docker logs "$container" >&2 || true
        fail "$container did not start"
    fi
done

export MATRIX_ROOT_ENDPOINT="tcp/127.0.0.1:$ROOT_PORT"
export MATRIX_CHILD_ENDPOINT="tcp/127.0.0.1:$CHILD_PORT"
export MATRIX_GRAND_ENDPOINT="tcp/127.0.0.1:$GRAND_PORT"
export MATRIX_ROOT_CONTAINER="$ROOT_CONTAINER"
if ! "$PYTHON" - <<'PY'
import json
import os
import subprocess
import threading
import time
import zenoh

def session(endpoint):
    config = zenoh.Config()
    config.insert_json5("mode", '"client"')
    config.insert_json5("connect/endpoints", json.dumps([endpoint]))
    config.insert_json5("connect/timeout_ms", "3000")
    config.insert_json5("connect/exit_on_failure", "true")
    config.insert_json5("scouting/multicast/enabled", "false")
    config.insert_json5("transport/auth/usrpwd", json.dumps({
        "user": "local", "password": "local-secret",
    }))
    return zenoh.open(config)

def prove_delivery(observer_endpoint, publisher_endpoint, key, expected=True, attempts=20):
    seen = threading.Event()
    observer = session(observer_endpoint)
    subscriber = observer.declare_subscriber(key, lambda _: seen.set())
    publisher = session(publisher_endpoint)
    try:
        for _ in range(attempts):
            publisher.put(key, b"managed-three-router")
            if seen.wait(0.25):
                break
        if seen.is_set() != expected:
            raise AssertionError(f"delivery for {key}: expected {expected}, got {seen.is_set()}")
    finally:
        publisher.close()
        subscriber.undeclare()
        observer.close()

time.sleep(2)
prove_delivery(os.environ["MATRIX_ROOT_ENDPOINT"], os.environ["MATRIX_GRAND_ENDPOINT"], "EFDI/child/grand/track")
prove_delivery(os.environ["MATRIX_ROOT_ENDPOINT"], os.environ["MATRIX_GRAND_ENDPOINT"], "EFDI/foreign/escape", False, 4)
print("PASS: grandchild data reaches root only inside the delegated scope")

subprocess.run(["docker", "stop", os.environ["MATRIX_ROOT_CONTAINER"]], check=True, stdout=subprocess.DEVNULL)
time.sleep(1)
prove_delivery(os.environ["MATRIX_CHILD_ENDPOINT"], os.environ["MATRIX_GRAND_ENDPOINT"], "EFDI/child/grand/offline")
print("PASS: child and grandchild continue locally while root is offline")

subprocess.run(["docker", "start", os.environ["MATRIX_ROOT_CONTAINER"]], check=True, stdout=subprocess.DEVNULL)
time.sleep(4)
prove_delivery(os.environ["MATRIX_ROOT_ENDPOINT"], os.environ["MATRIX_GRAND_ENDPOINT"], "EFDI/child/grand/recovered")
print("PASS: hierarchy recovers after the root returns")
PY
then
    docker logs "$ROOT_CONTAINER" >&2 || true
    docker logs "$CHILD_CONTAINER" >&2 || true
    docker logs "$GRAND_CONTAINER" >&2 || true
    fail "managed hierarchy data-plane assertions failed"
fi

echo "Managed three-router matrix passed."
