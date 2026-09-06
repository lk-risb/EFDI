# TAK / CoT sources

`../../../compose/layers/tak_layer.py` (Zenoh → CoT XML → TAK Server/ATAK)
is an egress translator, not a decoder of an inbound wire format the way
`cat.py`/`flex335.py`/`stanag.py` are — the risk profile here is "does the
*symbology and schema* match what the standard actually defines," not "did
we read a bit layout correctly."

## Trust: best-effort from mixed/secondary sources

Unlike the ASTERIX work (a machine-readable spec repo fetched and diffed
against this session, see `../asterix-specs/ASTERIX.md`) or SAPIENT (an
official vendored `.proto` schema, see `../sapient/SAPIENT.md`), the CoT and
MIL-STD-2525C/APP-6 symbology in `tak_layer.py` were implemented from
general, prior public-standard knowledge — not a specification fetched and
read during this project. Treat the CoT event structure and SIDC-derived
type codes as **best-effort against a widely-documented public standard**,
the same trust tier `../stanag/STANAG.md` gives STANAG 4609's KLV structure,
not the higher "verified against machine-readable spec" tier.

### Cursor on Target (CoT)

CoT was created by MITRE and is documented in their own technical paper,
["Cursor on Target: The Face of Every Combat ID Program You've Ever
Seen..."](https://www.mitre.org/sites/default/files/pdf/09_4937.pdf) — the
originating description of the `<event>` schema (`uid`, `type`, `how`,
`time`/`start`/`stale`, `<point>`, `<detail>`) that `tak_layer.py`'s
`_normalize_event`/`track_to_cot` build and parse. The open-source
[ATAK-CIV](https://github.com/deptofdefense/AndroidTacticalAssaultKit-CIV)
and [WinTAK-CIV](https://github.com/deptofdefense/WinTAK-CIV) repositories
(both public releases from the US DoD's TAK product line at
[tak.gov](https://tak.gov/)) are the actual reference clients this schema
targets, but neither was fetched or diffed against during this project —
the CoT builder was written from established knowledge of the schema, then
checked the only way that actually matters for this kind of code: against
a real client.

**What has real verification:** live iTAK rendering of a radar CoT marker,
its coverage-circle overlay, and its bearing/beam indication — confirmed
working end to end against an actual TAK client, not just schema-valid
XML (see the project changelog entry for the radar-relay/multi-sensor C2
hardening pass). That is real evidence for the CAT-34/CAT-48 radar CoT
path specifically. It is not evidence that every CoT type this file emits
(ships, ground vehicles, emergency-squawk GeoChat alerts, sensor-site
markers) has been visually confirmed in a TAK client — only the radar path
has.

### Cross-check against `snstac/pytak` (2026-09-06)

Fetched `github.com/snstac/pytak` (252★, actively maintained, the standard
Python TAK client library, published by Sensors & Signals LLC) and diffed
its `src/pytak/functions.py` `cot_event()`/`cot_point()` builders against
`track_to_cot()` here. Event attribute set (`version="2.0"`, `type`, `uid`,
`how="m-g"`, `time`/`start`/`stale`) and point attribute set
(`lat`/`lon`/`hae`/`ce`/`le`) match exactly. Our lat/lon precision (6
decimal places) is finer than pytak's own default helper (4 decimal
places) — no correction needed, ours is the more precise choice. This
upgrades the CoT event/point structure specifically (not the SIDC/MIL-STD
symbology below) from "best-effort" to "cross-checked against an
independent, actively-maintained reference implementation."

### Classification markings: `access`/`caveat` event attributes (2026-09-06)

`track_to_cot()` sets the CoT `<event>`'s `access` and `caveat` attributes
from a track's generic `classification`/`classification_caveat` keys, when
present. These are real, standard CoT event attributes — confirmed as part
of the same `pytak` cross-check above: `cot_event()` accepts `access`,
`qos`, `opex`, `caveat`, and `relto` as named optional parameters, each
mapped straight onto the identically-named (`relto`→`rel_to`) XML
attribute. Only `access`/`caveat` are wired here, since those are the two
that map onto data this project currently decodes (classification level,
handling/dissemination marking); `qos`/`opex`/`relto` are left unset rather
than guessed at. `protocols/random/nffi.py` is the first, and currently
only, producer of these two keys — see `../nffi/NFFI.md` and
`../mip/MIP.md` for where the underlying values come from. Mirrored to
EFDI-Allies' `tak_layer.py`. Covered by
`tests/test_sitaware_hq_nvg_feed.py::ClassificationMarkingTests`.

### MIL-STD-2525C / NATO APP-6

Affiliation coloring (`_BLUE`/`_GREEN`/`_YELLOW`/`_RED`), the CoT
two-letter-plus-suffix type codes (`a-f-A-C-F`, `a-h-G-E-V`, etc.), and the
icon-frame shapes (arc=air, box=ground, diamond=sea) implemented in
`tak_layer.py` follow [MIL-STD-2525C](https://www.dau.edu) (the US DoD
Common Warfighting Symbology standard) and its NATO equivalent,
[APP-6](https://nso.nato.int/nso/nsdd/main/standards) (STANAG 2019). Both
are public military standards with no single canonical machine-readable
schema the way ASTERIX has `asterix-specs` — implemented from the same
established, widely-cited symbol-code conventions every TAK/ATAK CoT
producer uses, not from a fresh fetch of either standard's own text during
this project.

### `_TOPIC_COT`'s Air/Sea/Space `friendly/unit` entries (no `-U-C` there)

`land/**/friendly/unit/**` maps to `a-f-G-U-C`, but Air/Sea/Space have no
equivalent of Ground's "-U-C" (Unit, Combat) function ID — confirmed by
grepping a real CoT type catalog
([dB-SPL/cot-types](https://github.com/dB-SPL/cot-types)'s `CoTtypes.xml`)
for any `A-U`/`S-U` "unit" entry: none exist. MIL-STD-2525's
Unit/Equipment/Installation split is Ground-only. `air/**/friendly/unit/**`
and `sea/**/friendly/unit/**` use the dimension-only types the catalog does
define instead (`a-f-A`, `a-f-S`), the same style already used elsewhere in
this file for `a-u-A`/`a-f-P`, rather than inventing a finer-grained code
that isn't real. Full rationale, including why this needed fixing at all
(`nffi.py` routes tracks off decoded domain now, not a hardcoded `land`),
in `../nffi/NFFI.md`.

## Why this file looks different from `../asterix-specs/ASTERIX.md`

ASTERIX and SAPIENT both have an authoritative structured source this
project actually fetched and checked its own work against — a `.ast` DSL
repo, an official `.proto` schema. CoT and 2525C/APP-6 do not have an
equivalent freely-fetchable machine-readable source that was pulled into
this project; they're long-established public standards implemented from
prior knowledge and validated the only way available for this kind of
output-facing code — real client behavior — rather than a spec diff.
Rather than borrow the ASTERIX table's higher-confidence language, this
file says plainly that the symbology mapping itself is best-effort, while
the rendering path that has actually been observed against a real TAK
client is called out specifically. See `../sitaware/SITAWARE.md` for the
equivalent assessment of the SitaWare/NVG egress path.
