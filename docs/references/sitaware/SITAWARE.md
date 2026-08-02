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
in `../../INSTALL.md`'s troubleshooting section were all observed against an
actual SitaWare instance, not inferred from the spec. That confirms the
transport/auth/polling contract works end to end; it is weaker evidence for
whether every NVG element this file emits renders exactly as SitaWare's
symbology engine intends for every SIDC/domain combination.

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
