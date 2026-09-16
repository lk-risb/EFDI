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

# Build into a fresh sibling directory and swap it in via a single symlink
# rename, rather than deleting $OUTPUT's contents in place and regenerating
# on top of it. start.sh runs this on every single invocation, unconditional
# of which service was requested — deleting first left a real window where
# $OUTPUT existed but was empty, and any process starting (or being
# auto-restarted by supervisor.py after an unrelated crash, concurrently
# with a second start.sh invocation regenerating protobufs for an unrelated
# --service request) during that window failed with "No module named
# 'protocols.proto.X_pb2'" for no reason connected to its own code — this
# is exactly what happened on the EFDI box on 2026-09-16 (aartos_json.py hit
# it after a WebUI-triggered restart raced supervisor.py's own restart of a
# different bridge). An in-place two-step "mv old aside, mv new into place"
# (the previous approach here) is NOT actually atomic as a whole — there are
# two separate rename() syscalls with a real window of total absence between
# them, and two concurrent runs of this script can also interleave their own
# renames unpredictably since neither locks the other out. A single `mv -T`
# of a symlink onto $OUTPUT is exactly one rename() syscall: $OUTPUT is
# always either the complete old tree (via the old symlink) or the complete
# new one, with no window where it resolves to nothing, and no way for two
# concurrent regenerations to leave it half-swapped — whichever one's final
# rename() lands last simply wins outright, same as any other lock-free
# last-writer-wins rename.
TMP_OUTPUT="$(mktemp -d "$ROOT/compose/generated.XXXXXX")"
trap 'rm -rf "$TMP_OUTPUT"' EXIT
OUTPUT_LINK="$ROOT/compose/generated.link.$$"
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

# Swap the freshly-built tree in with one atomic rename. rename(2) cannot
# replace a non-empty real directory with a symlink directly, so a plain
# directory left over from before this script used symlinks (or a first-ever
# run where $OUTPUT doesn't exist) needs a one-time plain removal first —
# only real directories take this path; on every run after the first,
# $OUTPUT is already a symlink and this branch never triggers again, so the
# steady-state swap below is always a single rename() with no window.
if [[ -e "$OUTPUT" && ! -L "$OUTPUT" ]]; then
    rm -rf "$OUTPUT"
fi
OLD_TARGET="$(readlink "$OUTPUT" 2>/dev/null || true)"
ln -s "$TMP_OUTPUT" "$OUTPUT_LINK"
mv -T "$OUTPUT_LINK" "$OUTPUT"
trap - EXIT
[[ -n "$OLD_TARGET" && -e "$OLD_TARGET" ]] && rm -rf "$OLD_TARGET"

echo "Generated $(( ${#contracts[@]} + ${#vendor_names[@]} )) Python protobuf bindings in $OUTPUT"
