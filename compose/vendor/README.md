# compose/vendor/

Third-party schemas vendored verbatim. **Do not edit these files** — they are
upstream contracts, and local edits would silently diverge EFDI's wire format
from the standard it claims to speak. Re-vendor from upstream instead.

## `sapient_msg/` — BSI Flex 335 v2.0 (SAPIENT)

- **Upstream:** https://github.com/dstl/SAPIENT-Proto-Files (`bsi_flex_335_v2_0/`)
- **Licence:** Apache License 2.0 — see `sapient_msg/LICENCE.txt`. Permits use,
  modification and redistribution, including commercial and defence use, as
  long as the licence and copyright notices are retained (which is why
  `LICENCE.txt` sits beside the schemas rather than only in this README).
- **Copyright:** The British Standards Institution retains ownership and
  copyright of BSI Flex 335; publication rights are held by BSI Standards Ltd.

### Why it lives here and not under `compose/protocols/`

`compose/protocols/` holds EFDI's *own* contracts. These are someone else's, and
they carry their own package (`sapient_msg.bsi_flex_335_v2_0`) and internal
import paths of the form `sapient_msg/bsi_flex_335_v2_0/<file>.proto`. Those
imports only resolve if this directory is its own protoc include root, so
`scripts/generate-protobuf.sh` passes `-I compose/vendor` alongside
`-I compose`.

### What uses it

EFDI both reads and writes SAPIENT:

- **Decoding** — `compose/protocols/vendors/sapient/flex335.py` parses incoming
  SAPIENT with a hand-written protobuf reader. Its field numbers were verified
  against these files and match exactly.
- **Encoding** — outbound tracks are converted into a real
  `SapientMessage`/`DetectionReport` so consumers need to understand only
  SAPIENT rather than every source protocol.
