# STANAG sources

`../../../compose/protocols/vendors/stanag/stanag.py` implements four unrelated
NATO standards behind one `--proto {4586,4607,4609,5516}` switch. Unlike the
ASTERIX categories, there is no single authoritative machine-readable
open-source repository covering all of these — the source material is
mixed, and confidence is correspondingly uneven across the four. This is
reflected directly in the code: `stanag.py` already carries explicit
disclaimers on the lower-confidence paths (quoted below) rather than
presenting uniform confidence.

## STANAG 4586 (`--proto 4586`)

- **What it is:** UAV Control Segment / Vehicle Specific Module interface.
- **Trust: low, explicitly scoped down in code.** The implementation
  targets one historical deployment's ICD layout, not a general STANAG 4586
  decoder. The code's own doc comment: *"Historical deployment layout,
  disabled by default; not claimed as a generic STANAG 4586 decoder."*
  `STANAG4586_PROFILE=legacy_ed3_approx` requires validating against the
  actual VSM ICD in use before trusting it for a new integration.
- **Why no bulk source copy here:** the full STANAG 4586 VSM ICD is a
  NATO-restricted-distribution document, not a freely redistributable
  public standard — nothing from it is bundled into this repository.

## STANAG 4607 (`--proto 4607`)

- **What it is:** NATO GMTI (Ground Moving Target Indicator) Format — a
  binary, packet/segment-oriented format for ground-radar moving-target
  reports (the AGS/JSTARS-class "ASTERIX equivalent" for GMTI radar).
- **Trust: verified against machine-readable ground truth, unverified
  against traffic.** The primary STANAG text is NATO-restricted, same
  situation as 4586's VSM ICD — but unlike 4586, there is no vendor-specific
  ambiguity here, because Wireshark ships a real, actively-maintained
  open-source dissector for this exact wire format
  ([`epan/dissectors/packet-stanag4607.c`](https://raw.githubusercontent.com/wireshark/wireshark/master/epan/dissectors/packet-stanag4607.c),
  GPL-2.0-or-later). Every byte offset, existence-mask bit position, and
  scale-factor formula in `stanag.py`'s `_4607_*` functions was read
  directly from that file, not summarized or transcribed from memory — the
  dissector's own `prt_sa32`/`prt_ba32`/`prt_centimeters`/etc. functions are
  the literal source of the degree/meter/m-per-s conversions used here.
- Segments implemented: Mission(1), Dwell(2) (including every Target Report
  inside it — the actual per-target track data), Job Definition(5),
  Platform Location(13). Unknown segment types are skipped by their
  declared size rather than guessed.
- Target Report's Delta Latitude/Longitude (a compact 2-byte alternative to
  the 4-byte absolute Hi-Res Latitude/Longitude) has no resolved scale even
  in the reference dissector itself — Wireshark displays it as a raw signed
  integer with no unit conversion. Kept exactly that way here (`delta_lat_raw`/
  `delta_lon_raw`): a track's `lat_deg`/`lon_deg` is only ever populated from
  the absolute fields, which do have a confirmed formula.
- A hand-constructed synthetic packet (built independently from the same
  scale formulas, not copy-pasted from the decoder) round-trips correctly,
  including a case exercising the Dwell Segment's optional D28-D31 fields —
  this caught a real bit-position transcription error (`sensor_heading`/
  `sensor_pitch`/`sensor_roll`/`mdv` were off by one field, colliding with
  the Target Report's own D32 bits) before it shipped.
- STANAG 4607 defines the message, not the bearer — real deployments carry
  it over TCP, UDP, or a tactical data link depending on installation. Like
  4609, this decodes whatever complete packets a bridge has already placed
  on the fabric; it never owns a socket itself.
- **Not verified against real traffic** at time of writing.

## STANAG 4609 (`--proto 4609`)

- **What it is:** motion imagery metadata (KLV local sets carried over
  MPEG-TS, per MISB ST 0601).
- **Trust: moderate.** MISB ST 0601 is a public standard published by the
  US National Geospatial-Intelligence Agency's Motion Imagery Standards
  Board (publicly available at gwg.nga.mil), unlike the 4586 ICD. The
  implementation decodes the published KLV local-set tag/length/value
  structure. SRT is EFDI's chosen transport for the metadata stream, not
  part of the KLV schema itself — this is called out in
  `../../08-integrations.md` to avoid implying SRT is a STANAG 4609
  requirement.
- **Not verified against real traffic** at time of writing.

## STANAG 5516 / Link 16 (`--proto 5516`)

- **What it is:** MIL-STD-6016F / STANAG 5516 J-series tactical data link
  messages, carried over JREAP-C (MIL-STD-3011).
- **Trust: moderate, narrow scope.** MIL-STD-6016F itself is not a freely
  redistributable public document; the J2.2/J2.5/J3.2/J3.5/J3.7 message
  subset implemented here was built from publicly documented bit-layout
  descriptions in open literature (academic/industry papers describing
  Link 16 J-series word formats) rather than the primary military
  standard text, and is explicitly scoped to that subset — not a general
  Link 16 decoder.
- This was restored from git history (commit `228d90b~1`, where it had
  been previously removed) and merged into the unified `stanag.py` +
  `stanag.proto` alongside 4586/4609 during this project.
- **Not verified against real traffic** at time of writing.

## Why this file looks different from `../asterix-specs/ASTERIX.md`

ASTERIX has one open, BSD-3-licensed, machine-readable spec repository that
covers every category, so `../asterix-specs/ASTERIX.md` can point at one exact URL per
category with a copy of the real file in this repo. STANAG has no single
source that covers all four: 4586's real ICD is restricted, 4609's real
standard (MISB ST 0601) is public but wasn't bulk-copied here, 5516's real
standard (MIL-STD-6016F) is restricted, and 4607 — the one exception — has
a real open-source machine-readable reference (Wireshark's dissector) even
though the primary STANAG text itself is restricted, same trust level as
the ASTERIX categories. Rather than overstate confidence to match the
ASTERIX table's uniform format, this file says plainly where each of the
four stands.
