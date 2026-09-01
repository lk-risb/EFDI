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

# Build into a fresh sibling directory and swap it in with two atomic
# renames, rather than deleting $OUTPUT's contents in place and regenerating
# on top of it. start.sh runs this on every single invocation, unconditional
# of which service was requested — deleting first left a real window where
# $OUTPUT existed but was empty, and any process starting (or being
# auto-restarted by supervisor.py after an unrelated crash) during that
# window failed with "No module named 'protocols.proto.X_pb2'" for no
# reason connected to its own code. A `mv` within the same filesystem is an
# atomic directory-entry swap: $OUTPUT is always either the complete old
# tree or the complete new one, never a partially-emptied one, and any
# process that already opened a file from the old tree keeps reading it
# fine even after the swap (POSIX unlink-on-rename semantics).
TMP_OUTPUT="$(mktemp -d "$ROOT/compose/generated.XXXXXX")"
trap 'rm -rf "$TMP_OUTPUT"' EXIT
OUTPUT_OLD="$ROOT/compose/generated.old.$$"
VENDOR_ROOT="$ROOT/compose/protocols/vendors/sapient"

mapfile -t contracts < <(find "$ROOT/compose/protocols" -type f -name '*.proto' -not -path "$VENDOR_ROOT/sapient_msg/*" -print | sort)
(( ${#contracts[@]} > 0 )) || { echo "No protobuf contracts found" >&2; exit 1; }
contract_names=()
for contract in "${contracts[@]}"; do
    contract_names+=("${contract#"$ROOT/compose/"}")
done

# The vendored BSI Flex 335 schema (compose/protocols/vendors/sapient/sapient_msg)
# is its own include root: its files import each other as
# "sapient_msg/bsi_flex_335_v2_0/<f>", which only resolves relative to
# VENDOR_ROOT, not compose/. They are compiled with paths relative to that
# root; EFDI's own contracts relative to compose/. Both roots are passed to
# one protoc invocation — each file argument is unambiguous under exactly one
# of the two roots, so there is no double-resolution.
vendor_names=()
if [[ -d "$VENDOR_ROOT/sapient_msg" ]]; then
    while IFS= read -r vendored; do
        vendor_names+=("${vendored#"$VENDOR_ROOT/"}")
    done < <(find "$VENDOR_ROOT/sapient_msg" -type f -name '*.proto' -print | sort)
fi

"$PYTHON" -m grpc_tools.protoc \
    -I "$ROOT/compose" \
    -I "$VENDOR_ROOT" \
    --python_out="$TMP_OUTPUT" \
    "${contract_names[@]}" "${vendor_names[@]}"

# Swap the freshly-built tree in. $OUTPUT may not exist yet (first run ever)
# — only rename it aside if it does, so that case isn't an error.
[[ -e "$OUTPUT" ]] && mv "$OUTPUT" "$OUTPUT_OLD"
mv "$TMP_OUTPUT" "$OUTPUT"
trap - EXIT
rm -rf "$OUTPUT_OLD"

echo "Generated $(( ${#contracts[@]} + ${#vendor_names[@]} )) Python protobuf bindings in $OUTPUT"
