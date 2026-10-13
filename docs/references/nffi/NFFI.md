# NFFI sources

`../../../compose/protocols/vendors/nffi/nffi.py` decodes NATO Friendly Force
Information (NFFI) XML — the Friendly Force Tracking exchange defined by
ADatP-36 / STANAG 5527 — into normalized friendly-force tracks. `nffi.py`
itself is protocol-agnostic about who publishes the raw XML: output goes
onto the generic `<PREFIX>/<ORG>/{domain}/nato/c2/friendly/unit` track
topic (see the domain-routing fix below) that any C2 layer can consume —
SitaWare included, via `sitaware_layer.py`'s generic friendly-unit path,
but nothing SitaWare-specific.

**Correction (2026-09-06):** this file previously claimed "SitaWare's own
C2 integration uses NVG, not NFFI" — true only for this project's own
egress path (EFDI → SitaWare, via `sitaware_layer.py`'s NVG feed). It's
false as a claim about SitaWare itself: a real SitaWare Headquarters
instance has a dedicated "NFFI and FFI Manager" (Coalition Gateway
section) that can run as either an NFFI/FFI Client or Server, across
several transport variants (IP1, IP1 Classic, IP2, SIP3) — confirmed by
direct screenshot of a live instance, not documentation. See "Added
2026-09-06: bridges/vendors/nffi/nffi_bridge.py" below.

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
15-character code (`unit_type`) rather than fully decoded into affiliation/
echelon — a real opportunity for a richer C2 mapping, deliberately left for
a separate pass rather than folded into this correctness fix. The battle
dimension character IS now decoded, for topic routing — see below.

## Fixed 2026-09-06: output topic hardcoded the `land` domain

`OUTPUT_TOPIC` was a single constant with `land` baked into the domain
segment (`{root}/land/nato/c2/friendly/unit`), for every track regardless of
what it actually was. Nothing in NFFI restricts it to land: STANAG 5527 is a
general Friendly Force Tracking exchange, and `identificationData/unitSymbol`
is documented as a plain APP-6(A) SIDC, not a land-only code. A real feed
carrying a naval or air friendly unit would have been forced onto the land
topic regardless.

Fixed by decoding the SIDC's Battle Dimension character — position 3
(0-based index 2) of the 15-character code, per APP-6(A) Annex B / 2525B —
and picking the topic's domain segment from it at publish time, per track:

| SIDC battle dimension | Domain topic |
|---|---|
| `P` (Space) | `space` |
| `A` (Air) | `air` |
| `G` (Ground) | `land` |
| `S` (Sea Surface) | `sea` |
| `U` (Subsurface) | `sea` (no distinct subsurface topic domain exists) |
| `F` (Special Operations Forces) | `land` (ground-based by convention) |
| anything else, or `unitSymbol` absent | `land` (previous behavior, kept as the default) |

`OUTPUT_TOPIC` (one constant) became `OUTPUT_TOPICS` (a dict keyed by domain)
plus `_output_topic(unit_symbol)` to pick the right one. `tests/test_nffi.py`
covers all six mapped dimensions plus the unrecognized/absent-symbol default.

## Added 2026-09-06: `unitSymbol` echelon (Symbol Modifier, SIDC positions 11-12)

Partial follow-up to the "left for a separate pass" note above — decodes one
more SIDC field, not the full affiliation/domain/echelon set. Positions 11-12
(0-based index 10:12) are the Echelon/Size Symbol Modifier; when the code is
one of the 13 recognized values it is set on `track["nffi_echelon"]` as a
readable label (`"Battalion/Squadron"`, `"Division"`, etc.) — the same
`nffi_`-prefixed, non-generic-key convention as `nffi_strength`/`nffi_status`.
Placeholder codes (`"--"`, or the `"**"` seen in wildcard SIDCs like the test
fixture's) and any other unrecognized code are left undecoded, no key set.

**Confidence caveat:** the code table (`-A`=Team/Crew … `-M`=Region) is
sourced from Carmenta Engine's own MIL-STD-2525B Appendix B documentation
(a GIS vendor implementing this exact standard), not independently
cross-checked against the DoD's own MIL-STD-2525B/APP-6A PDF text — that
document is too large to search directly and no second corroborating source
was found. Same single-vendor-source caution as the daveb1034/NVGTools note
in `../sitaware/SITAWARE.md`. The values are at least internally consistent:
alphabetical order matches ascending unit size with no gaps, which is weak
but real evidence against a hallucinated table. Revisit if a primary-source
copy of MIL-STD-2525B/APP-6A ever gets checked directly.

Affiliation (SIDC position 2) is deliberately NOT decoded from `unitSymbol`:
NFFI's own schema already forces `affiliation: "friendly"` for every track
(see the code comment above), so decoding a redundant field from the SIDC
would add no information and risks contradicting that schema-level guarantee
if a real feed ever sent something else in that position.

## Fixed 2026-09-06: TAK/SitaWare had no route for non-`land` NFFI units

Direct consequence of the domain-routing fix above: once a naval/air/space
NFFI unit could publish on its own domain topic, neither consumer had a
subscription entry for it. `tak_layer.py`'s `_TOPIC_COT` and
`sitaware_layer.py`'s `_TOPIC_SIDC` only had `land/**/friendly/unit/**`
wired — so the fix, alone, would have turned "wrongly shown as a ground
unit" into "invisible in both C2 systems" for that case.

Checked whether a real Air/Sea/Space equivalent of Ground's `a-f-G-U-C`
("-U-C" = Unit, Combat) exists before adding anything: it does not.
Confirmed against dB-SPL/cot-types' `CoTtypes.xml` (a comprehensive,
community-maintained CoT type catalog) — Ground has a real `a-.-G-U`
("Gnd/Unit") category that Air/Sea/Subsurface do not; grepping the full
file for any `A-U`/`S-U` "unit" entry returns nothing. MIL-STD-2525's
Unit/Equipment/Installation split is Ground-only; Air/Sea/Space function
IDs go straight to platform-type codes (aircraft type, vessel type), with
no organizational-unit concept to borrow. Inventing one would repeat the
exact mistake already made and fixed once this session (the fabricated
NFFI namespace).

Used the dimension-only "track, no further classification" CoT types the
catalog does define — `a-.-A` (Air Track), `a-.-S` (Sea Surface Track),
`a-.-P` — which this codebase already relies on elsewhere (`a-u-A` for
unclassified radar returns, `a-f-P` for satellites). On the SitaWare side,
the equivalent is an unspecified (dash-filled) SIDC function ID rather than
Ground's `U` — `SFAP------*****` / `SFSP------*****` / `SFPP------*****` —
which matches the existing `space/**/*/satellite/**` entries' own style
exactly (same "no function ID" pattern, already in the file).

Sea Surface (`S`) and Subsurface (`U`) SIDC dimensions both still collapse
onto the single `sea` topic domain (per the earlier fix) and now the single
`a-f-S` / `SFSP------*****` marker — a submarine reporting over NFFI would
render with the surface-track icon. Accepted: NFFI/BFT traffic from a
subsurface platform is not a realistic scenario, and the codebase's own
topic taxonomy has no separate subsurface domain to route it through even
if it were.

`tak_layer.py`'s `_TOPIC_COT` and `sitaware_layer.py`'s `_TOPIC_SIDC` each
gained three entries (`air`/`sea`/`space` × `friendly/unit`).
`tests/test_sitaware_hq_nvg_feed.py` covers all four domains (including the
pre-existing `land` one) for both dicts.

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

## Added 2026-09-06: `bridges/vendors/nffi/nffi_bridge.py` — the missing ingest side

`nffi.py` owns no source socket by design (see its own docstring) — it has
always expected something else to land raw NFFI XML onto
`.../raw/nffi/{source-id}`. Nothing in this repo ever did that. Added
`nffi_bridge.py`: dials out to a partner's NFFI/FFI server, extracts
complete `NFFIMessage`/`track` XML frames from the TCP stream, republishes
the raw bytes for `nffi.py` to decode unchanged.

**Scoped against, but not yet tested on, a real endpoint.** The intended
target is a SitaWare Headquarters "NFFI and FFI Manager" server instance
(IP1/IP1 Classic/IP2/SIP3 — see the correction above), but that instance
is being reinstalled and its real connection details (host, port, which
profile, TLS) aren't available yet. The bridge's TCP-dial-and-frame
pattern is copied from `bridges/vendors/tak/tak_bridge.py`'s own proven ingress path
(same codebase, already working for CoT), not from any confirmed NFFI wire
documentation — the file's own docstring lists exactly what's reused-and-
proven versus guessed-and-unverified (transport, framing, direction of
dial, no known default port). Update it once real details land, and treat
anything it does before then as untested.

Registered in `start.sh` (a `nffi-bridge` case, prompting for
`NFFI_HOST`/`NFFI_PORT` the same way `tak-bridge` prompts for `TAK_HOST`)
and `compose/.env.example` — this project has a documented prior instance
of a translator existing but never being wired into the launcher (`nffi`
itself, per `docs/14-continuous-integration.md`'s 2026-07-10 entry), so
registered this one immediately instead of letting the same gap recur.

Verified: `tests/test_nffi_bridge.py` (6 tests) covers frame extraction
(single document, two documents split across chunks, a bare `<track>` with
no `NFFIMessage` wrapper, leading noise before the first recognized tag —
the last two using a real TCP socket, not just the pure function) and the
required-host/required-port refusal. All pass. Full test suite unaffected.

## What still hasn't been verified

No real NFFI traffic from a partner system has been received by EFDI to
date. The `nffi.py` decode logic above is a spec-conformance fix verified
against the actual primary XSD and a synthetic message built from it, not
a live-traffic test. `nffi_bridge.py` (above) is closer to closing that
gap but isn't there yet — it has never connected to a real NFFI/FFI server.


## Added 2026-10-13: layers/vendors/tak/systematic/nffi_layer.py (EFDI -> SitaWare)

`nffi_layer.py` is the reverse direction of `nffi.py`: it writes the TAK users as NFFI XML to SitaWare's NFFI and FFI Manager
(IP1 Client, connected out to its port; the layer can also listen).

Sources used and how far each is trusted:

- **The NC3A NFFI 1.4 XSD** (`community.rti.com/sites/default/files/STANAG5527_NFFI14_original.xsd`, the same file as above),
  fetched again and read for the element order, the required elements and the field patterns the layer cuts its values to:
  `trackSource/sourceSystem` (country `[A-Z]{3}`, system `[A-Z0-9-]` up to 30, optional subsystem), `transponderId`
  (`[A-Za-z0-9.,:*-]` 1-60), `dateTime` (14 digits), latitude/longitude ranges, bearing `[0,360)`, speed `>= 0` (km/h as the schema
  states it), `unitSymbol` (`[A-Z0-9-]{15}`, so no `*` wildcards), `unitShortName` (21 characters), `remarks` (60 characters).
  Trust: high for the document structure. The XSD itself is not stored in the repo and no XSD validation is run; the tests check the patterns.
- **The existing project notes above**, for the fact that a real SitaWare HQ has an "NFFI and FFI Manager" (Coalition Gateway) that
  can run as client or server in the profiles IP1, IP1 Classic, IP2 and SIP3 (from a screenshot of a live instance, not documentation).
- **milsymbol SIDC documentation** and the **symbol.army 2525C list** for the letter SIDC in `unitSymbol` (friendly ground unit, the
  Team/Crew echelon `A`, the function ids), see `../symbology/SYMBOLOGY.md`.
- **A vendor blog on CoT and NFFI** (`corvusintell.com`, "Bridging CoT and NATO standards"), only for the idea of keeping a stable
  track id per CoT uid and using the stale time to drop tracks; no code or field was taken from it.
- **Systematic's SitaWare Headquarters datasheet**, only for the statement that NFFI is a supported standard.

## Verified 2026-10-13 against a live SitaWare Headquarters 6.21: what SitaWare itself writes

An NFFI IP1 Classic Server was created in the manager (payload NFFI, source layer with 9 objects, port 8095) with a contract whose
host was the gateway's address. A passive TCP connection from that host to the server port was then answered with a stream. What it
showed (21,300 bytes, 4 messages of 9 tracks, 10 s apart):

- Namespace `urn:nato:fft:protocols:nffi13`, not nffi14: SitaWare 6.21 writes NFFI 1.3. A newer SitaWare may write 1.4 (not checked),
  so `NFFI_LAYER_VERSION` selects the namespace and defaults to 1.3.
- Each document is behind a 16-byte header: `04 00 00 00`, the time in epoch seconds (4 bytes, big-endian), `00 00 00 00`, `01 00`
  (the contract's header priority is 1, so possibly that), and the document length in bytes (2 bytes, big-endian). The XML follows with
  a declaration (`<?xml version="1.0" encoding="UTF-8"?>` and a newline) and ends with a newline. The meaning of the first four bytes and
  of `01 00` is a guess; the layer writes the same values.
- `positionalData` and `identificationData` carry `secPolicyName="0" secClassification="UNCLASSIFIED" secCategory="0"`; element order is
  trackSource (country, system, subsystem, transponderId), dateTime, coordinates, then identificationData with unitSymbol and unitShortName.
- The server closes a connection from a host that has no contract (tried before the contract existed: closed at once, no data).
- The IP1 Server form (the non-Classic one) asks for no port and its contract asks for the client's host and port; the IP1 Classic Server
  asks for a port and its contract only for a host. Which direction the IP1 Client form (port only) expects is still not verified.

Not confirmed: whether the 16-byte header and nffi13 are also what SitaWare's IP1 *Client* expects (it is the receiving side of the
same protocol, so it is assumed); the layer's earlier attempts (no header, nffi14) left the client at Connected with 0 objects.

The CoT fields NFFI has no place for (team colour, platform) are carried in the remarks.

## Verified 2026-10-13: what a live SitaWare 6.21 writes as FFI (APP-11 Edition D), and `ffi_layer.py`

The same NFFI IP1 Classic Server, set to Payload Message "FFI (APP-11 Edition D Version 1)" with Joint Symbology Type APP-6(B), was read
through its port with a contract for the reading host (28,066 bytes, 2 messages of 9 tracks). Findings:

- Each message is behind the same 16-byte header as NFFI, but the first byte is `0x14` instead of `0x04`; the length at bytes 14-15 and the
  epoch seconds at bytes 4-7 are as for NFFI.
- The XML has the root `mtf:FriendlyForceInformation` (namespace `urn:nato:mtf:app-11(d):1:ffi`, `mtfid="FFI"`), then `MessageIdentifier`
  (`MessageTextFormatIdentifier` FFI, `Standard` APP-11(D), `Version` 1, `Originator` the country code, `ReferenceTimeOfPublication/DateTimeIso`
  with year, month, day, `T`, hour, minute, second, `Z`, the security policy, classification and category), `GeodeticDatum/GeodeticDatum` WGE, and
  one or more `TrackInformation`. SitaWare put all 9 tracks of the layer into one message; `ffi_layer.py` sends one track per message.
- A `TrackInformation` holds `TrackSource` (`TransponderId`, `System`, `Subsystem`), `TrackSecurityLabel` (policy, classification, category, and
  the same for BlueDot and Details), `TrackPositionalData` (`Time` as year to second, and `Location` in degrees and minutes: `LatitudeDegrees`,
  `LatitudeMinutes04DecimalPlaces` such as `46.004`, `LatitudinalHemisphere`, `Hyphen` `-`, `LongitudeDegrees` three digits, minutes,
  hemisphere) and `TrackIdentificationData` (`UnitSymbol`, `SymbolStandard` `APP-6(B)`, `UnitShortName`). No speed, bearing or altitude appeared
  in this capture.
- With APP-6(B) the `UnitSymbol` is the same 15-character letter code as in NFFI. The dropdown of other symbology types was not looked at yet,
  so which `SymbolStandard` values SitaWare accepts (APP-6(D) among them) and what the 20-digit symbol looks like in `UnitSymbol` is unverified.

`ffi_layer.py` writes this structure for the TAK users (a separate layer from `nffi_layer.py`; `FFI_LAYER_SYMBOL_STANDARD=APP-6(D)` puts the
20-digit APP-6D dismounted individual in `UnitSymbol`). 
Tested live the same day (the gateway's `ffi_layer` connected out to the IP1 Client `EFDI-TAK`, `nffi_layer` stopped):

- The IP1 Client, which has no payload setting, took the FFI messages (object count 1, the same user as from NFFI, details: Friendly, Team, system
  EFDI, subsystem TAK; since changed to the pod-scoped `EFDI-<namespace>`, see below).
- With `FFI_LAYER_SYMBOL_STANDARD=APP-6(B)` (`SFGPUCI----A---`) it draws the same infantry symbol as from NFFI.
- With `APP-6(D)` (the 20-digit dismounted individual in `UnitSymbol`, `SymbolStandard` `APP-6(D)`) the message was still accepted, but the user was
  drawn as a plain blue unit frame, Friendly, size Unknown, with no icon and no person: SitaWare 6.21 does not draw the numeric APP-6(D) symbol
  in FFI either, as it did not in NVG. Which `SymbolStandard` values it parses beyond APP-6(B) was not checked (the server form's dropdown was not read).
- So on 6.21 the person icon is not reachable through NFFI, FFI or NVG; the letter code (infantry, Team) is the best it draws. A newer SitaWare
  may draw it; the two layers and `SITAWARE_TAK_USERS_NVG` are there to try that without new code.

## The way back, 2026-10-13: nffi_bridge.py / ffi_bridge.py, ffi.py and the decoders against the real captures

The same Classic Server captures (NFFI and FFI) were fed to the new code, offline, as a check: `nffi_bridge.Ip1Reader` cut the NFFI capture into
4 documents and the FFI capture into 2, told by the first header byte; `nffi.parse_nffi` (now matching elements by local name, so it takes
NFFI 1.3 and 1.4) decoded 32 tracks from the NFFI documents and `ffi.parse_ffi` 16 from the FFI ones. The two formats gave the same
tracks at the same positions (to the sixth decimal; FFI carries minutes with three decimals), with `source_system` EFDI and `source_subsystem`
TAK, the system and subsystem of the tracks of `nffi_layer`, which that server layer held.

- The header has to be parsed, not the XML alone: type byte, 3 zeros, time, 4 zeros, 2 bytes (the contract's header priority, 1 here), length.
  A server can put several documents in one stream; the bridge also cuts a stream with no header by the closing tag of the root element.
- A numeric APP-6(D) `UnitSymbol` is routed by its symbol set (digits 5-6; the sets 01, 02, 05, 10, 11, 15, 20, 25, 27, 30, 35, 36, 40 exist in the
  milstd package's APP-6D tables), a letter symbol by its dimension letter as in `nffi.py`.
- Our own tracks come back when the server's source layer holds them (here, the layer our `nffi_layer` fills): `fusion.is_own_force_track`
  drops them in the decoders, so a TAK user does not return into TAK as a second marker.
- Not decoded because no capture showed them: FFI speed, course and altitude elements, and an NFFI-1.4 document from a real SitaWare (6.21 writes 1.3).
- Not tried live yet when this was written: the bridge dialing a Classic Server end to end through Zenoh into TAK.

### Telling our own tracks from a partner's when they come back (2026-10-13)

The first filter dropped anything with source system `EFDI` and subsystem `TAK`. Every EFDI pod writes those same words, so a partner pod's TAK
users arriving through SitaWare would have been dropped as well. Two marks tell this pod's own tracks instead (`fusion.is_own_force_track`):

- the source system is `EFDI-` plus this pod's namespace (`PARTNER_NAMESPACE`, for example `EFDI-LTU-CISB-ZENOH-GATEWAY`, cut to the 30 characters
  NFFI's system field takes), with subsystem `TAK`; a partner pod has another namespace and is kept;
- the transponderId equals an id this pod's own bridge published (the TAK uid, remembered by `fusion.EchoFilter`, `has_id`, exact match, 10
  characters or more). This needs no tag, so it also catches tracks sent before the tag changed, which are still in SitaWare's layer until they
  go stale, and tracks through a layer that does not keep the tag.

A field that this pod writes into its own JSON (an "origin" mark) would not do: a system that re-publishes our data under its own ids loses it, as
the echo filter's notes describe. The system, subsystem and transponder id are the fields SitaWare keeps on the way through.
