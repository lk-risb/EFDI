# SAPIENT / BSI Flex 335 sources

`../../../compose/protocols/vendors/sapient/flex335.py` implements both decode and
encode for all 9 SAPIENT message types (registration, status report,
detection report, task, task ack, alert, alert ack, error, associated
file), against the wire schema vendored at
`compose/protocols/vendors/sapient/sapient_msg/bsi_flex_335_v2_0/*.proto`.

## Source

The vendored `.proto` files carry `option java_package =
"uk.gov.dstl.sapientmsg.bsiflex335v2"` throughout, identifying them as the
UK Ministry of Defence's Defence Science and Technology Laboratory (Dstl)
official protobuf schema for BSI Flex 335 (the SAPIENT interface standard).
This is the authoritative machine-readable schema — not a third-party
transcription — vendored directly into this repository at
`../../../compose/protocols/vendors/sapient/sapient_msg`.

## Trust: high

`flex335.py`'s encode/decode logic is driven directly by protobuf's own
generated bindings against this vendored schema — wire-format correctness
(field tags, types, required/optional handling) is enforced by the
protobuf library itself, not hand-parsed bit arithmetic. This removes the
main risk category that applies to the ASTERIX work (see `../asterix-specs/ASTERIX.md`):
there is no "did I read the bit layout off a spec correctly" step, because
the schema **is** the wire format.

The residual risk is narrower: does our internal track/detection JSON map
onto the *correct* proto field for a given concept (e.g. is a bearing value
written into the field SAPIENT expects it in)? That mapping was implemented
and reviewed field-by-field against the vendored `.proto` comments during
this project (tasks completed: full decode of all 9 message types, full
encode of all 9 message types), but has not been checked against a real
SAPIENT-conformant third-party sensor or ICD test vectors.

## What hasn't been verified

No real SAPIENT-conformant sensor traffic has been received by EFDI to
date. As with the unverified ASTERIX categories, treat `flex335.py` as
structurally correct against the vendored schema, not yet proven against a
live SAPIENT feed.
