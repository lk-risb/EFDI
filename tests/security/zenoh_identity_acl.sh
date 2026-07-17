#!/usr/bin/env bash
set -euo pipefail

# Prove the identity primitive required by the delegated-ACL design against the
# exact Zenoh version used by Compose. All keys, certificates, credentials and
# configs are disposable and live only below mktemp(1)'s directory.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/../.." && pwd)
ZENOH_IMAGE=${ZENOH_IMAGE:-eclipse/zenoh:1.9.0}
TEST_DIR=$(mktemp -d)
CONTAINER_NAME="efdi-zenoh-identity-acl-$$"
TLS_PORT=
TCP_PORT=

cleanup() {
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    rm -rf "$TEST_DIR"
}
trap cleanup EXIT INT TERM

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

for command in docker openssl timeout; do
    command -v "$command" >/dev/null 2>&1 || fail "$command is required"
done

docker image inspect "$ZENOH_IMAGE" >/dev/null 2>&1 ||
    fail "$ZENOH_IMAGE is not available locally (pull the Compose image first)"

echo "Preparing disposable Zenoh 1.9 identity material..."

if [[ -n ${EFDI_ZENOH_PYTHON:-} ]]; then
    ZENOH_PYTHON=$EFDI_ZENOH_PYTHON
elif [[ -x "$REPO_ROOT/compose/venv/bin/python3" ]]; then
    ZENOH_PYTHON="$REPO_ROOT/compose/venv/bin/python3"
else
    ZENOH_PYTHON=python3
fi

"$ZENOH_PYTHON" -c \
    'import importlib.metadata; assert importlib.metadata.version("eclipse-zenoh") == "1.9.0"' \
    2>/dev/null || fail "eclipse-zenoh==1.9.0 is required (set EFDI_ZENOH_PYTHON)"

free_port() {
    "$ZENOH_PYTHON" -c \
        'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()'
}

TLS_PORT=$(free_port)
TCP_PORT=$(free_port)
[[ $TLS_PORT != "$TCP_PORT" ]] || TCP_PORT=$(free_port)

openssl ecparam -name prime256v1 -genkey -noout -out "$TEST_DIR/ca-key.pem"
openssl req -x509 -new -sha256 -days 1 \
    -key "$TEST_DIR/ca-key.pem" -subj "/CN=EFDI disposable ACL test CA" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$TEST_DIR/ca.pem"

issue_cert() {
    local name=$1
    local common_name=$2
    local eku=$3
    local san=${4:-}
    local ext_file="$TEST_DIR/$name.ext"

    openssl ecparam -name prime256v1 -genkey -noout -out "$TEST_DIR/$name-key.pem"
    openssl req -new -sha256 -key "$TEST_DIR/$name-key.pem" \
        -subj "/CN=$common_name" -out "$TEST_DIR/$name.csr"
    {
        echo "basicConstraints=critical,CA:FALSE"
        echo "keyUsage=critical,digitalSignature"
        echo "extendedKeyUsage=$eku"
        if [[ -n $san ]]; then
            echo "subjectAltName=$san"
        fi
    } >"$ext_file"
    local serial_args=(-CAserial "$TEST_DIR/ca.srl")
    if [[ ! -f $TEST_DIR/ca.srl ]]; then
        serial_args=(-CAcreateserial -CAserial "$TEST_DIR/ca.srl")
    fi
    openssl x509 -req -sha256 -days 1 \
        -in "$TEST_DIR/$name.csr" -CA "$TEST_DIR/ca.pem" \
        -CAkey "$TEST_DIR/ca-key.pem" "${serial_args[@]}" \
        -extfile "$ext_file" -out "$TEST_DIR/$name.pem"
}

issue_cert router zenoh-acl-router serverAuth "IP:127.0.0.1,DNS:localhost"
issue_cert client-a client-a clientAuth
issue_cert client-b client-b clientAuth
issue_cert client-observer client-observer clientAuth
issue_cert client-unregistered client-unregistered clientAuth
issue_cert client-quarantined client-quarantined clientAuth

# Issue one client with a seconds-scale lifetime using openssl ca(1), so the
# same run can prove that close_link_on_expiration disconnects an active mTLS
# link. The other test leaves use x509(1)'s simpler day-granularity path.
openssl ecparam -name prime256v1 -genkey -noout -out "$TEST_DIR/client-expiring-key.pem"
mkdir -p "$TEST_DIR/ca-db/newcerts"
: >"$TEST_DIR/ca-db/index.txt"
echo 1000 >"$TEST_DIR/ca-db/serial"
cat >"$TEST_DIR/ca.cnf" <<EOF
[ ca ]
default_ca = local_ca
[ local_ca ]
database = $TEST_DIR/ca-db/index.txt
new_certs_dir = $TEST_DIR/ca-db/newcerts
serial = $TEST_DIR/ca-db/serial
certificate = $TEST_DIR/ca.pem
private_key = $TEST_DIR/ca-key.pem
default_md = sha256
default_days = 1
policy = identity_policy
copy_extensions = copy
unique_subject = no
[ identity_policy ]
commonName = supplied
EOF
EXPIRY_TEXT=$(date -u -d '+25 seconds' '+%y%m%d%H%M%SZ')
START_TEXT=$(date -u -d '-1 minute' '+%y%m%d%H%M%SZ')
# Add the client extensions to the CSR because openssl ca copies requested
# extensions but does not accept x509(1)'s -extfile form for issuance.
openssl req -new -sha256 -key "$TEST_DIR/client-expiring-key.pem" \
    -subj "/CN=client-expiring" \
    -addext "basicConstraints=critical,CA:FALSE" \
    -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=clientAuth" \
    -out "$TEST_DIR/client-expiring.csr"
openssl ca -batch -notext -config "$TEST_DIR/ca.cnf" \
    -startdate "$START_TEXT" -enddate "$EXPIRY_TEXT" \
    -in "$TEST_DIR/client-expiring.csr" -out "$TEST_DIR/client-expiring.pem"
EXPIRY_EPOCH=$(date -u -d "$(openssl x509 -in "$TEST_DIR/client-expiring.pem" -noout -enddate | cut -d= -f2)" '+%s')

PASS_A=$(openssl rand -hex 16)
PASS_B=$(openssl rand -hex 16)
PASS_OBSERVER=$(openssl rand -hex 16)
PASS_QUARANTINED=$(openssl rand -hex 16)
PASS_EXPIRING=$(openssl rand -hex 16)
cat >"$TEST_DIR/users.txt" <<EOF
user-a:$PASS_A
user-b:$PASS_B
observer:$PASS_OBSERVER
quarantined:$PASS_QUARANTINED
expiring:$PASS_EXPIRING
EOF

cat >"$TEST_DIR/router.json5" <<'EOF'
{
  mode: "router",
  listen: { endpoints: ["tls/0.0.0.0:17447", "tcp/0.0.0.0:17448"] },
  scouting: {
    multicast: { enabled: false },
    gossip: { enabled: false },
  },
  transport: {
    link: {
      tls: {
        root_ca_certificate: "/test/ca.pem",
        listen_private_key: "/test/router-key.pem",
        listen_certificate: "/test/router.pem",
        enable_mtls: true,
        close_link_on_expiration: true,
      },
    },
    auth: { usrpwd: { dictionary_file: "/test/users.txt" } },
  },
  access_control: {
    enabled: true,
    default_permission: "deny",
    rules: [
      { id: "allow-a", messages: ["put"], flows: ["ingress"], permission: "allow", key_exprs: ["test/a/**"] },
      { id: "allow-b", messages: ["put"], flows: ["ingress"], permission: "allow", key_exprs: ["test/b/**"] },
      { id: "allow-expiring", messages: ["put"], flows: ["ingress"], permission: "allow", key_exprs: ["test/expiring/**"] },
      { id: "allow-quarantined", messages: ["put"], flows: ["ingress"], permission: "allow", key_exprs: ["test/quarantined/**"] },
      { id: "deny-quarantined", messages: ["put"], flows: ["ingress"], permission: "deny", key_exprs: ["**"] },
      { id: "observe", messages: ["declare_subscriber", "put"], flows: ["ingress", "egress"], permission: "allow", key_exprs: ["test/**"] },
    ],
    subjects: [
      { id: "identity-a", cert_common_names: ["client-a"], usernames: ["user-a"] },
      { id: "identity-b", cert_common_names: ["client-b"], usernames: ["user-b"] },
      { id: "identity-observer", cert_common_names: ["client-observer"], usernames: ["observer"] },
      { id: "identity-expiring", cert_common_names: ["client-expiring"], usernames: ["expiring"] },
      { id: "identity-quarantined", cert_common_names: ["client-quarantined"], usernames: ["quarantined"] },
    ],
    policies: [
      { rules: ["allow-a"], subjects: ["identity-a"] },
      { rules: ["allow-b"], subjects: ["identity-b"] },
      { rules: ["observe"], subjects: ["identity-observer"] },
      { rules: ["allow-expiring"], subjects: ["identity-expiring"] },
      { rules: ["allow-quarantined", "deny-quarantined"], subjects: ["identity-quarantined"] },
    ],
  },
}
EOF

chmod 0600 "$TEST_DIR"/*-key.pem "$TEST_DIR/users.txt"

docker run --detach --rm --name "$CONTAINER_NAME" \
    --publish "127.0.0.1:$TLS_PORT:17447" \
    --publish "127.0.0.1:$TCP_PORT:17448" \
    --volume "$TEST_DIR:/test:ro" \
    "$ZENOH_IMAGE" -c /test/router.json5 >/dev/null

echo "Waiting for the disposable Zenoh router..."

router_ready=0
for _ in $(seq 1 60); do
    if "$ZENOH_PYTHON" -c \
        "import socket; s=socket.create_connection(('127.0.0.1',$TLS_PORT),.2); s.close()" \
        >/dev/null 2>&1; then
        router_ready=1
        break
    fi
    sleep 0.1
done
if (( ! router_ready )); then
    docker logs "$CONTAINER_NAME" >&2 || true
    fail "Zenoh test router did not start"
fi

export ACL_TEST_DIR="$TEST_DIR"
export ACL_TLS_ENDPOINT="tls/127.0.0.1:$TLS_PORT"
export ACL_TCP_ENDPOINT="tcp/127.0.0.1:$TCP_PORT"
export ACL_PASS_A="$PASS_A"
export ACL_PASS_B="$PASS_B"
export ACL_PASS_OBSERVER="$PASS_OBSERVER"
export ACL_PASS_QUARANTINED="$PASS_QUARANTINED"
export ACL_PASS_EXPIRING="$PASS_EXPIRING"
export ACL_EXPIRY_EPOCH="$EXPIRY_EPOCH"

cat >"$TEST_DIR/run_cases.py" <<'PY'
import importlib.metadata
import json
import os
import threading
import time

import zenoh

assert importlib.metadata.version("eclipse-zenoh") == "1.9.0"

ROOT = os.environ["ACL_TEST_DIR"]
TLS_ENDPOINT = os.environ["ACL_TLS_ENDPOINT"]
TCP_ENDPOINT = os.environ["ACL_TCP_ENDPOINT"]
received: set[str] = set()
received_lock = threading.Lock()


def client_config(endpoint, cert=None, username=None, password=None):
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([endpoint]))
    conf.insert_json5("connect/timeout_ms", "2000")
    conf.insert_json5("connect/exit_on_failure", "true")
    conf.insert_json5("scouting/multicast/enabled", "false")
    if cert is not None:
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": f"{ROOT}/ca.pem",
            "connect_certificate": f"{ROOT}/{cert}.pem",
            "connect_private_key": f"{ROOT}/{cert}-key.pem",
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    if username is not None:
        conf.insert_json5("transport/auth/usrpwd", json.dumps({
            "user": username,
            "password": password,
        }))
    return conf


def open_client(cert=None, username=None, password=None, endpoint=TLS_ENDPOINT):
    return zenoh.open(client_config(endpoint, cert, username, password))


def on_sample(sample):
    with received_lock:
        received.add(str(sample.key_expr))


def wait_for(key, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with received_lock:
            if key in received:
                return True
        time.sleep(0.05)
    return False


def publish_case(label, key, expected, *, cert=None, username=None, password=None, endpoint=TLS_ENDPOINT):
    session = None
    try:
        session = open_client(cert, username, password, endpoint)
        for _ in range(3 if expected else 1):
            session.put(key, label.encode())
            time.sleep(0.1)
    except Exception:
        if expected:
            raise
    finally:
        if session is not None:
            session.close()
    observed = wait_for(key, 2.0 if expected else 0.7)
    if observed != expected:
        raise AssertionError(f"{label}: expected observed={expected}, got {observed}")
    print(f"PASS: {label}")


observer = open_client(
    "client-observer", "observer", os.environ["ACL_PASS_OBSERVER"]
)
subscriber = observer.declare_subscriber("test/**", on_sample)
time.sleep(0.5)

publish_case(
    "A certificate + A password can publish on A scope",
    "test/a/allowed", True,
    cert="client-a", username="user-a", password=os.environ["ACL_PASS_A"],
)
publish_case(
    "A certificate + A password cannot publish on B scope",
    "test/b/a-scope-escape", False,
    cert="client-a", username="user-a", password=os.environ["ACL_PASS_A"],
)
publish_case(
    "A certificate + B password is denied",
    "test/a/cert-a-password-b", False,
    cert="client-a", username="user-b", password=os.environ["ACL_PASS_B"],
)
publish_case(
    "B certificate + A password is denied",
    "test/b/cert-b-password-a", False,
    cert="client-b", username="user-a", password=os.environ["ACL_PASS_A"],
)
publish_case(
    "unregistered CA-valid certificate is denied",
    "test/a/unregistered-cert", False,
    cert="client-unregistered", username="user-a", password=os.environ["ACL_PASS_A"],
)
publish_case(
    "TLS certificate without username is denied",
    "test/a/tls-without-username", False,
    cert="client-a",
)
publish_case(
    "username without TLS certificate is denied",
    "test/a/username-without-tls", False,
    username="user-a", password=os.environ["ACL_PASS_A"], endpoint=TCP_ENDPOINT,
)
publish_case(
    "explicit quarantine deny overrides matching allow",
    "test/quarantined/blocked", False,
    cert="client-quarantined", username="quarantined",
    password=os.environ["ACL_PASS_QUARANTINED"],
)

expiring = open_client(
    "client-expiring", "expiring", os.environ["ACL_PASS_EXPIRING"]
)
before_key = "test/expiring/before-expiry"
expiring.put(before_key, b"before")
if not wait_for(before_key):
    raise AssertionError("expiring certificate was not usable before expiry")
print("PASS: seconds-scale certificate works before expiry")

sleep_for = max(0.0, float(os.environ["ACL_EXPIRY_EPOCH"]) - time.time() + 2.0)
time.sleep(sleep_for)
deadline = time.monotonic() + 8.0
while time.monotonic() < deadline and list(expiring.info.routers_zid()):
    time.sleep(0.2)
if list(expiring.info.routers_zid()):
    raise AssertionError("close_link_on_expiration did not disconnect the expired mTLS client")
after_key = "test/expiring/after-expiry"
try:
    expiring.put(after_key, b"after")
except Exception:
    pass
if wait_for(after_key, 0.7):
    raise AssertionError("expired mTLS client still delivered data")
print("PASS: close_link_on_expiration disconnects the active mTLS client")

expiring.close()
subscriber.undeclare()
observer.close()
print("PASS: Zenoh 1.9 identity ACL compatibility matrix")
PY

echo "Running certificate-CN + username ACL matrix..."
timeout --foreground 75s "$ZENOH_PYTHON" "$TEST_DIR/run_cases.py" || {
    status=$?
    docker logs "$CONTAINER_NAME" >&2 || true
    if (( status == 124 )); then
        fail "identity ACL matrix exceeded 75 seconds"
    fi
    exit "$status"
}
