# SitaWare / NVG sources

`../../../compose/layers/sitaware_layer.py` (Zenoh → NVG 2.0.2 XML →
SitaWare HQ) is an egress translator, not a decoder of an inbound wire
format the way `cat.py`/`flex335.py`/`stanag.py` are — the risk profile
here is "does the *symbology and schema* match what the standard actually
defines," not "did we read a bit layout correctly."

## Trust: best-effort from mixed/secondary sources

Unlike the ASTERIX work (a machine-readable spec repo fetched and diffed
against this session, see `../asterix-specs/ASTERIX.md`) or SAPIENT (an
official vendored `.proto` schema, see `../sapient/SAPIENT.md`), the NVG
2.0.2 schema in `sitaware_layer.py` was implemented from general, prior
public-standard knowledge — not a specification fetched and read during
this project. Treat it as **best-effort against a widely-documented public
standard**, the same trust tier `../stanag/STANAG.md` gives STANAG 4609's
KLV structure, not the higher "verified against machine-readable spec"
tier.

### NVG 2.0.2

`sitaware_layer.py`'s NVG XML namespace,
`NVG_NS = "https://tide.act.nato.int/schemas/2012/10/nvg"`, is the real
namespace URI published by NATO Allied Command Transformation's TIDE
(The Interoperability Data Environment) — the NATO body that maintains the
NVG (NATO Vector Graphics) friendly-force-tracking schema SitaWare's Import
Subscription mechanism consumes. The URI itself was not re-fetched as a
document during this project (TIDE's schema registry requires NATO-affiliated
access for the full XSD); the `<point>`/`<polygon>`/`<polyline>`/`TimeSpan`/
`ExtendedData` element structure and the version string `2.0.2` were
implemented from established knowledge of the format, the same way the CoT
side (`../tak/TAK.md`) was.

**What has real verification:** operational field debugging against a real
SitaWare HQ 6.22 Import Subscription — the health-endpoint counters
(`successful_requests`/`unauthorized_requests`), the "Latest replication"
timestamp behavior, and the specific HTTP response-code semantics documented
in `../../11-troubleshooting.md` were all observed against an
actual SitaWare instance, not inferred from the spec. That confirms the
transport/auth/polling contract works end to end; it is weaker evidence for
whether every NVG element this file emits renders exactly as SitaWare's
symbology engine intends for every SIDC/domain combination.

## External corroboration attempt (2026-09-06)

TIDE's own schema registry (`tide.act.nato.int/tidepedia`, `.../git/nvg/nvg_2.0`)
requires NATO-affiliated login — not fetchable here. Searched more broadly
(web search, not just GitHub) for independent NVG implementations to
corroborate against instead, with mixed results — deliberately weighting
each by how current and how authoritative it actually is, not just whether
it turned up:

- **`github.com/spatialillusions/nvg`** (JS NVG library, real commit
  2025-02-12 — not a stale repo with a recently-touched timestamp) has a
  genuine sample document. It confirms, independently: the namespace URI
  `https://tide.act.nato.int/schemas/2012/10/nvg`, the root element
  `<nvg:nvg version="...">` pattern, and — significant — the `symbol`
  attribute convention `symbol="{scheme}:{SIDC}"` (its sample uses
  `app6a:G*C*MALC--*****`), which matches this file's
  `symbol_scheme + ":" + sidc` exactly. This upgrades those specific pieces
  from "established knowledge" to "cross-checked against an independent,
  currently-maintained implementation."
- **`github.com/daveb1034/NVGTools`** was also found, but its actual commits
  are all from 2015 (the repo's `pushed_at` metadata is misleadingly
  recent) and its namespace table only goes up to version `2.0.0` — it
  predates 2.0.2 and does not corroborate that specific version string.
  Treated as weak/superseded evidence, not used to upgrade trust.
- **NISP Nation** (`nisp.nw3.dk/standard/act-nvg-2.0.2.html`), a defense
  standards catalog, confirms "NVG 2.0.2" is a real, NATO-published
  standard ID dated 2015-05-23 — but the page itself carries no schema
  detail (namespace, elements), so this only corroborates that the version
  string is legitimate, not the structure below.
- **Luciad's public NVG 2.0 SDK reference** (`dev.luciad.com`, a real
  commercial geospatial vendor — Hexagon/Luciad — used across the defense
  GIS industry) documents `TLcdNVG20Content`'s property fields verbatim,
  including exact capitalization: `TIME_STAMP_PROPERTY` → `"TimeStamp"`
  element, `TIME_SPAN_PROPERTY` → `"TimeSpan"` element, `SYMBOL_PROPERTY`
  → `"symbol"` attribute, `URI_PROPERTY` → `"uri"` attribute,
  `LABEL_PROPERTY` → `"label"` attribute. Separately, `TLcdNVG20SimpleData`
  documents `KEY_PROPERTY` → the `"key"` attribute (lowercase) with the
  element's text content as the value. Every one of these matches
  `sitaware_layer.py` exactly, including the `key` attribute name on
  `SimpleData`. This is the strongest single source in this file — a
  commercial SDK vendor's own class reference, not a community project —
  and resolves what the previous pass here called unconfirmed.

## Vendor-shipped NVG XSD found (2026-09-06, via `~/Downloads/INTCORE`)

The user's own INT-CORE (a NATO C2 interoperability integration platform)
installation media — version 5.0's `6-INTCORE-DOCS.iso`,
`06_TestArtefacts/FAT/FAT_TestData_5.0.0/Canonical Schemas/NVG 1.5
Canonical Schema.zip` — ships a **complete, genuine NVG XSD set**:
`nvg.1.5.xsd`, `nvg.data.1.5.xsd`, `nvg.types.1.5.xsd`,
`nvg.capabilities.1.5.xsd`, `nvg.filter.1.4.xsd`, plus the KML/xAL/Dublin
Core schemas NVG imports. This is edition 1.5 (namespace
`http://tide.act.nato.int/schemas/2009/10/nvg`), not our target 2.0.2
(`.../2012/10/nvg`) — editions, not diffable line-for-line — but as a real,
complete, vendor-shipped primary source it independently confirms the
element/attribute shape that carried forward into 2.0.2:

- `nvgBaseAttributesGrp` defines `uri` (`xsd:anyURI`) and `label`
  (`xsd:string`) exactly as used here, with a documented purpose ("uri
  schema that uniquely identifies the object", "a textual representation
  of this element") matching this file's usage.
- `nvgSymbologyAttributesGrp` defines `symbol` (`SymbolCodeType`) with
  documentation reading almost verbatim as our own convention: *"Its
  format is the name of a standard followed by a colon and the text
  representation of the element in that standard"* — i.e. exactly
  `{scheme}:{SIDC}`, confirming `symbol_scheme + ":" + sidc` independent
  of both Luciad and spatialillusions.
- `SimpleDataType` (in `nvg.types.1.5.xsd`) declares `key` (`xsd:QName`,
  **required**) as its identifying attribute, with the value as element
  text content — byte-for-byte the same shape as `sitaware_layer.py`'s
  `ExtendedData`/`SimpleData` construction and Luciad's 2.0 SDK docs above.
  Three independent sources now agree on this one exactly.
- `TimeStamp`/`TimeSpan` are genuinely absent from 1.5's schema — they are
  a real 2.0-era addition, not a gap in this search. Consistent with, not
  contradicting, the Luciad finding above.

### Bug found and fixed: `point`'s `speed` attribute was in the wrong unit

`nvg.data.1.5.xsd`'s `pointType` defines `x`=longitude, `y`=latitude
(WGS-84 decimal degrees, both required), and two optional kinematic
attributes: `course` (`directionType`, 0–360° clockwise from North) and
`speed` (`speedType`) — whose documentation states, twice, independently
(once on the type, once on the attribute use site): *"the speed the object
is moving with, expressed in **knots**."* `track_to_nvg_point()` was
converting to km/h (`_speed_ms(track) * 3.6`) instead — every speed value
sent to SitaWare was off by a factor of ~1.85x (km/h > knots for the same
physical speed). Fixed to `* 1.943844` (m/s → knots, the reciprocal of the
`0.514444` knots→m/s constant already used elsewhere in `tak_layer.py`).
Found by reading the vendored XSD's own attribute documentation, not by
guessing — this is exactly the kind of unit-only bug a structural
schema-shape check (element names, attribute presence) doesn't catch,
since `speed="12.3"` is valid either way; only reading what the schema
*says the number means* catches it.

**Also checked, resolved as correct (not a bug):** `point`'s `z` attribute
isn't defined in the vendored 1.5 XSD's `pointType`/`NvgMapObjectType`/
`NvgDataObjectType`/`NvgBaseType` chain — initially flagged here as an
uncertainty, since it only validated via 1.5's permissive `##any`
attribute wildcard. Resolved by checking Luciad's `TLcdNVG20Point` class
reference for the 2.0 edition directly: it declares `Z_PROPERTY` → the
`z` XML attribute, documented as *"altitude distances... expressed in
meters relative (positive or negative) to the datum surface of WGS-84"* —
a real, formally-defined 2.0-era addition, the same pattern as
`TimeStamp`/`TimeSpan`. `_primary_altitude()` already scales every input
(ft ×0.3048, km ×1000) to metres before this file uses it, so no fix
was needed.

Also found (not yet used, potentially useful for future work):
`INTCORE_SDS_AnnexO_NVGIntegration.docx` documents INT-CORE's own NVG ADS
as a **pull-based SOAP/WSDL web service** (`GetCapabilities`/`GetNvg`
operations, NVG 1.4 WS), a different transport model than
`sitaware_layer.py`'s push/POST Import Subscription — worth knowing if a
future partner's "NVG feed" turns out to be this WS-pull style instead.

### Classification marking: root `classification` attribute (2026-09-06)

`track_to_nvg_item()` sets the NVG document's root `classification`
attribute from a track's generic `classification` key, when present — the
same key `nffi.py` populates from NFFI's `secClassification` (see
`../nffi/NFFI.md` and `../mip/MIP.md`). Confirmed against the vendored 1.5
XSD's `nvgType` (same schema used for the speed-unit fix above):
`classification` is a real, first-class attribute on the `<nvg>` root
element itself, sibling to `version`, documented as *"recommended... at
least one of the words unclassified, restricted, confidential or
secret."* NVG has no equivalent to CoT's `caveat` — a track's
`classification_caveat` key (also set by `nffi.py`, from `secCategory`)
has nothing to map onto here and is correctly left unused for this format.
Only applies to the per-track document builder (`track_to_nvg_item()`);
the aggregate multi-item document builder (`NVGFeedCache.document()`)
merges many tracks' already-serialized XML into one document and
correctly has no single classification to apply at that level. Mirrored
to EFDI-Allies' `sitaware_layer.py`. Covered by
`tests/test_sitaware_hq_nvg_feed.py::ClassificationMarkingTests`.

### `_TOPIC_SIDC`'s Air/Sea/Space `friendly/unit` entries (2026-09-06)

`land/**/friendly/unit/**` maps to `SFGPU-----*****` — function ID `U`
(Unit) + 5 dashes. Air/Sea/Space have no Unit/Equipment/Installation split
in MIL-STD-2525 the way Ground does (confirmed against a real CoT type
catalog while fixing the TAK-side equivalent — see `../tak/TAK.md`), so
there's no `U`-equivalent letter to use for them. Used a fully unspecified
(all-dash) function ID instead — `SFAP------*****` (air), `SFSP------*****`
(sea, also covers subsurface), `SFPP------*****` (space) — which matches
this file's own existing `space/**/*/satellite/**` entries exactly (same
"no function ID" pattern already in the dict, not a new convention).
Full rationale in `../nffi/NFFI.md`.

### SitaWare has its own real NFFI/FFI capability, separate from this file's NVG path (2026-09-06)

A real SitaWare Headquarters instance has a dedicated "NFFI and FFI
Manager" (Coalition Gateway section) supporting both NFFI/FFI Client and
Server roles, across several transport variants (IP1, IP1 Classic, IP2,
SIP3) — confirmed by direct screenshot of a live instance. This is
completely separate from `sitaware_layer.py`'s NVG feed documented in this
file: NVG is EFDI's own picture pushed OUT to SitaWare; a SitaWare NFFI/FFI
Server would be SitaWare's OWN friendly-unit picture, pulled IN to EFDI via
the new `bridges/nffi_bridge.py` (see `../nffi/NFFI.md`) and `nffi.py`.
Not yet configured or tested against a real SitaWare NFFI/FFI server as of
this writing — the exact wire behavior of IP1/IP1 Classic/IP2/SIP3 is
unconfirmed. Worth checking, once that ingest path is live, that SitaWare's
NFFI/FFI Server doesn't end up re-serving objects that originated from
EFDI's own NVG push in the first place (a single-pass echo, not a runaway
loop, but still a duplicate worth avoiding if EFDI ever exports units of
its own to the same SitaWare instance that would come back through NFFI).

## Why this file looks different from `../asterix-specs/ASTERIX.md`

ASTERIX and SAPIENT both have an authoritative structured source this
project actually fetched and checked its own work against — a `.ast` DSL
repo, an official `.proto` schema. NVG does not have an equivalent
freely-fetchable machine-readable source that was pulled into this
project; it's a long-established NATO standard implemented from prior
knowledge and validated the only way available for this kind of
output-facing code — real server behavior — rather than a spec diff.
Rather than borrow the ASTERIX table's higher-confidence language, this
file says plainly that the schema mapping itself is best-effort, while the
transport/polling path that has actually been observed against a real
SitaWare instance is called out specifically. See `../tak/TAK.md` for the
equivalent assessment of the TAK/CoT egress path.
