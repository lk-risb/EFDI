# Sparkplug B sources

`../../../compose/protocols/vendors/sparkplug/sparkplug.py` decodes Eclipse
Sparkplug B protobuf payloads received via `bridges/mqtt_bridge.py`, using
the schema vendored at
`compose/protocols/vendors/sparkplug/sparkplug_b.proto`.

## Sources (two, independent, cross-checked 2026-09-06)

1. **`github.com/eclipse-sparkplug/sparkplug`** — the Eclipse Foundation
   working group's own specification repository (spec text + TCK), the
   primary/official source for Sparkplug B's normative behavior.
2. **`github.com/eclipse-tahu/tahu`** — the older Eclipse reference
   implementation, kept as an independent structural cross-check for the
   `.proto` wire schema itself.

## Trust: high

- Byte-diffed the vendored `sparkplug_b.proto` against
  `eclipse-tahu/tahu`'s `sparkplug_b/sparkplug_b.proto`. Every message,
  field number, and `oneof` shape matches exactly (our copy is reformatted
  and reproduces the schema without the `enum DataType` block, which our
  decoder never needs — it disambiguates metric values via
  `metric.WhichOneof("value")`, not the numeric `datatype` field).
- STANAG 4609-style tag/byte-layout risk doesn't apply here: Sparkplug is
  protobuf, so wire-format correctness comes from the protobuf library
  itself once the `.proto` is confirmed correct (same reasoning
  `../sapient/SAPIENT.md` gives for SAPIENT).

## Bug found and fixed: alias table not reset on rebirth

`AliasTable.learn()` merged each BIRTH's alias→name mappings into the
existing table for that node/device scope instead of replacing it. Found
during this cross-check because the class comment ("learned from each
node's BIRTH certificate") didn't match the merge behavior, then confirmed
against the specification text itself
(`specification/src/main/asciidoc/chapters/Sparkplug_5_Operational_Behavior.adoc`,
`tck-id-operational-behavior-data-publish-nbirth`): *"NBIRTH messages MUST
include all metrics for the specified Edge Node that will ever be
published for that Edge Node within the established Sparkplug session."*
A BIRTH is a **new session's** complete alias contract — a reconnecting
edge node may legally reuse an old alias number for a different metric, so
the old table must be discarded, not merged into. Fixed by replacing
`self._tables.setdefault(scope, {})` with `self._tables[scope] = {}`.

## What hasn't been verified

No real Sparkplug B broker traffic has been received by EFDI to date.
Treat `sparkplug.py` as structurally correct against the specification and
schema, not yet proven against a live edge-node feed.
