# NFFI sources

`../../../compose/protocols/random/nffi.py` decodes NATO Friendly Force
Information (NFFI) XML — the Friendly Force Tracking exchange defined by
ADatP-36 / STANAG 5527 — into normalized friendly-force tracks. It does not
feed SitaWare directly: SitaWare's own C2 integration uses NVG, not NFFI
(see `../sitaware/SITAWARE.md`). NFFI is a separate inbound partner-format
translator, output onto the generic
`<PREFIX>/<ORG>/land/nato/c2/friendly/unit` track topic that any C2 layer
can consume — SitaWare included, via `sitaware_layer.py`'s generic
friendly-unit path, but nothing SitaWare-specific.

## Corrected 2026-09-06: a real primary source exists, and the old code didn't match it

An earlier pass through this file concluded "no public NFFI reference exists
anywhere" after a GitHub-only search. That conclusion was wrong, and the
implementation it describes was wrong as a direct result — pushed to keep
digging with non-GitHub sources, a general web search for
`STANAG 5527 schema` surfaced a real, primary document:

**`community.rti.com/sites/default/files/STANAG5527_NFFI14_original.xsd`** —
the actual STANAG 5527 / NFFI **1.4** XSD, authored by **NC3A** (the NATO
Consultation, Command and Control Agency — the actual body responsible for
this standard), hosted publicly on RTI's (a real DDS/data-distribution
vendor) community support site, apparently uploaded there to help a user
debug code generation against it. Fetched and read in full. Cross-checked
against a second, independent source — an RTI community forum thread
discussing the same file, which corroborates the namespace and also
mentions an NFFI 1.3 edition using the equivalent `NFFI_13.xsd` naming
pattern. Searched further (including the actual IETF `RFC 7467`, which
registers NATO's URN namespace but documents no sub-namespace examples) for
any evidence of a "NFFI 2.0" edition — found none, anywhere, in any source.
Only 1.3 and 1.4 have any confirmed public trace.

**What the real schema actually looks like** (namespace
`urn:nato:fft:protocols:nffi14`), vs. what the old code assumed:

| Real NFFI 1.4 (the XSD) | Old code assumed | 
|---|---|
| Namespace `urn:nato:fft:protocols:nffi14` | `urn:nato:nffi:2.0` + three sibling namespaces (`position`/`unit`/`identification`) — fabricated, no such edition found anywhere |
| Root `NFFIMessage`, repeated `track` (`trackType`) | Root/record tags `UnitInfo`/`Track`/`FriendlyForce`/`FriendlyForceUnit`/`UnitTrack` — none exist in the real schema |
| Track identity: `positionalData/trackSource` (`sourceSystem/system`, optional `subsystem`, `transponderId`) | `UnitID`/`TrackID`/`ID` — don't exist |
| Name: `identificationData/unitShortName` | `Name`/`UnitName`/`Callsign` — don't exist |
| Position: `positionalData/coordinates/latitude`+`longitude` (lowercase) | `Latitude`/`LAT`/`Longitude`/`LON`/`Long` (capitalized) — case mismatch means these would never have matched |
| Heading: `positionalData/bearing`, degrees | `Heading`/`Direction` — don't exist |
| Speed: `positionalData/speed`, **km/h** | `Speed`, assumed already m/s with no conversion — **would have been off by 3.6x** had it ever matched anything |
| Altitude: `coordinates/altitude`, metres MSL | `Altitude`/`Elevation` — matched by luck (`altitude` lowercase is real; `Altitude` capitalized is not) |
| Affiliation: **no such field** — NFFI is a friendly-force-only exchange by definition | `Affiliation`/`AffiliationCode` tag lookup, defaulting to `"FRIEND"` when (inevitably) not found — right answer, wrong reasoning, fragile if anything ever "coincidentally" matched |
| Unit type: `identificationData/unitSymbol`, a 15-character APP-6(A) SIDC | not read at all |
| Emergency: `operStatusData/alert`, boolean | not read at all |
| Operational data: `operStatusData/strength`, `statusCode`, `remarks` | not read at all |

None of the old namespace/tag names appear in the real 1.4 (or, per the RTI
thread, 1.3) schema. Given zero evidence any "2.0"-shaped edition exists,
this was very likely written from a plausible-sounding but unverified
guess, not from a real spec — exactly the failure mode this project's
reference-tracking convention exists to catch.

## Fixed 2026-09-06

Rewrote `parse_nffi()` against the real NFFI 1.4 element structure:
`NFFIMessage/track/positionalData` (`trackSource`, `dateTime`,
`coordinates/latitude|longitude|altitude`, `bearing`, `speed`),
`identificationData` (`unitSymbol`, `unitShortName`), `operStatusData`
(`alert`, `strength`, `statusCode`). Fixed the km/h→m/s speed conversion
(previously absent). Track identity now comes from `trackSource`
(`system`/`subsystem`/`transponderId`) rather than a nonexistent ID tag —
deliberately *not* combined with `dateTime`, even though the schema's own
text pairs them for message deduplication, because doing so would mint a
new track identity on every single update from the same physical unit.
Also fixed two independent bugs in how the decoded dict reaches the wire:
the affiliation key was `nffi_affil`, which matches no field on the
`NffiTrack` protobuf message (the real field is `nffi_affiliation`) and so
was silently dropped by `track_views.py`'s generic field-assignment loop;
and no `affiliation` key was set at all, so every NFFI track's
`NormalizedTrack.affiliation` read `"unknown"` instead of `"friendly"` in
the protobuf view (the JSON view was unaffected — it serializes the raw
dict regardless of which keys protobuf recognizes). Test fixture in
`tests/test_nffi.py` rewritten to the same real structure; all 3 tests
pass against it.

`unitSymbol`'s APP-6(A) SIDC is currently carried through as the raw
15-character code (`unit_type`) rather than decoded into affiliation/domain/
echelon — a real opportunity for a richer C2 mapping, deliberately left for
a separate pass rather than folded into this correctness fix.

## Added 2026-09-06: classification metadata (`secPolicyName`/`secClassification`/`secCategory`)

The real NFFI 1.4 XSD declares these three attributes independently on
`positionalData`, `identificationData`, `operStatusData`, and `detailData` —
all present in the XSD but not decoded by the initial rewrite above.
Cross-checked their meaning against STANAG 4778's Metadata Binding schema
— see `../mip/MIP.md` for that source and the full three-field
correspondence. NFFI's three flat attributes are exactly a flattening of
that confidentiality-label model, not an NFFI-specific invention. Now decoded into
`nffi_sec_policy`/`nffi_sec_classification`/`nffi_sec_category`, checking
`positionalData` first (always present), then `identificationData`, then
`operStatusData`, per attribute independently, since each section may set
its own value. `secClassification`/`secCategory` are also copied onto
generic `classification`/`classification_caveat` keys, since classification
is a cross-cutting concern any future protocol decoder might carry, not
an NFFI-only one.

**Wired to both C2 egress paths (2026-09-06):** `tak_layer.py`'s
`track_to_cot()` sets CoT's real `access`/`caveat` event attributes from
those two generic keys (confirmed against `snstac/pytak`'s own
`cot_event()` — these are standard CoT attributes, not invented here), and
`sitaware_layer.py`'s `track_to_nvg_item()` sets NVG's real root
`classification` attribute (confirmed against the vendored 1.5 XSD — see
`../sitaware/SITAWARE.md`). Any future decoder that sets the same two
generic keys reaches both C2 systems automatically, with no changes needed
in `tak_layer.py`/`sitaware_layer.py`. Mirrored to EFDI-Allies (egress side
only — that repo has no NFFI decoder). Test fixture updated with a
`secClassification`/`secPolicyName` example; new tests added in
`tests/test_sitaware_hq_nvg_feed.py` covering the CoT and NVG egress paths
directly. All tests pass.

## What still hasn't been verified

No real NFFI traffic from a partner system has been received by EFDI to
date. The above is a spec-conformance fix verified against the actual
primary XSD and a synthetic message built from it, not a live-traffic test.
