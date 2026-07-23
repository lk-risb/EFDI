#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${EFDI_PROTOC_PYTHON:-"$ROOT/compose/venv/bin/python3"}
OUTPUT=${EFDI_PROTOBUF_OUTPUT:-"$ROOT/compose/generated"}

[[ -x "$PYTHON" ]] || { echo "Python environment not found: $PYTHON" >&2; exit 1; }
"$PYTHON" -c 'import grpc_tools.protoc, google.protobuf' 2>/dev/null || {
    echo "grpcio-tools and protobuf are required; install compose/requirements.txt" >&2
    exit 1
}

mkdir -p "$OUTPUT"
mapfile -t contracts < <(find "$ROOT/compose/protocols" -type f -name '*.proto' -print | sort)
(( ${#contracts[@]} > 0 )) || { echo "No protobuf contracts found" >&2; exit 1; }
contract_names=()
for contract in "${contracts[@]}"; do
    contract_names+=("${contract#"$ROOT/compose/"}")
done

"$PYTHON" -m grpc_tools.protoc \
    -I "$ROOT/compose" \
    --python_out="$OUTPUT" \
    "${contract_names[@]}"

echo "Generated ${#contracts[@]} Python protobuf bindings in $OUTPUT"
