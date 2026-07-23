#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHON=${EFDI_PROTOC_PYTHON:-"$ROOT/compose/venv/bin/python3"}
OUTPUT=${EFDI_PROTOBUF_OUTPUT:-"$ROOT/compose/generated"}

if [[ "$PYTHON" == */* ]]; then
    [[ -x "$PYTHON" ]] || {
        echo "Python environment not found: $PYTHON" >&2
        exit 1
    }
else
    PYTHON=$(command -v "$PYTHON") || {
        echo "Python environment not found on PATH: $PYTHON" >&2
        exit 1
    }
fi
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

# Vendored third-party schemas (compose/vendor) are their own include root: the
# BSI Flex 335 files import each other as "sapient_msg/bsi_flex_335_v2_0/<f>",
# which only resolves relative to compose/vendor. They are compiled with
# paths relative to that root, EFDI's own contracts relative to compose/.
vendor_names=()
if [[ -d "$ROOT/compose/vendor" ]]; then
    while IFS= read -r vendored; do
        vendor_names+=("${vendored#"$ROOT/compose/vendor/"}")
    done < <(find "$ROOT/compose/vendor" -type f -name '*.proto' -print | sort)
fi

"$PYTHON" -m grpc_tools.protoc \
    -I "$ROOT/compose" \
    -I "$ROOT/compose/vendor" \
    --python_out="$OUTPUT" \
    "${contract_names[@]}" "${vendor_names[@]}"

echo "Generated $(( ${#contracts[@]} + ${#vendor_names[@]} )) Python protobuf bindings in $OUTPUT"
