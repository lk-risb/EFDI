#!/usr/bin/env bash
# Atomically add VPN/DNS names to an initialized step-ca server certificate.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "usage: $0 <step-ca-state-directory> <dns-name-or-ip> [...]" >&2
    exit 2
fi
STATE_DIR=$1
shift
CONFIG=$STATE_DIR/config/ca.json
[[ -f "$CONFIG" ]] || { echo "step-ca configuration not found" >&2; exit 2; }
for name in "$@"; do
    [[ "$name" =~ ^[A-Za-z0-9.-]{1,253}$ ]] || { echo "invalid DNS name or IP" >&2; exit 2; }
done

export EFDI_STEP_CA_CONFIG=$CONFIG
export EFDI_STEP_CA_NAMES
EFDI_STEP_CA_NAMES=$(printf '%s\n' "$@")
python3 - <<'PY'
import json
import os
import tempfile
from pathlib import Path

path = Path(os.environ["EFDI_STEP_CA_CONFIG"])
document = json.loads(path.read_text(encoding="utf-8"))
names = [item for item in os.environ["EFDI_STEP_CA_NAMES"].splitlines() if item]
document["dnsNames"] = sorted(set(["localhost", *document.get("dnsNames", []), *names]))
fd, temporary = tempfile.mkstemp(prefix=".ca.json.", dir=path.parent, text=True)
try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
finally:
    try:
        os.unlink(temporary)
    except FileNotFoundError:
        pass
PY
echo "step-ca serving names updated; restart the step-ca service"
