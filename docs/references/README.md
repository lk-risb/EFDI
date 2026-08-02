# References

This directory answers one question: **where did the wire-format knowledge
in `compose/protocols/vendors/*` actually come from, and how trustworthy is
it?**

EFDI decodes protocols EFDI itself does not use operationally today — the
rule is "translate everything we can, because not using it doesn't mean
somebody else won't." That means most of the ASTERIX categories in `cat.py`
were implemented against a public specification with **no real captured
traffic to validate against**. This directory makes that traceable: every
source URL, every fetch/verification step, and a copy of the actual spec
text used, so a reviewer can check our work without re-deriving it.

## Contents

- [`asterix-specs/ASTERIX.md`](asterix-specs/ASTERIX.md) — per-category source, edition, trust
  assessment, and verification method for every category in `cat.py`.
- [`asterix-specs/`](asterix-specs/) — the actual `.ast` DSL spec files this
  work was decoded from, pulled directly from the upstream repository (not
  paraphrased), one per implemented category, plus that repository's
  `LICENSE`.
- [`sapient/SAPIENT.md`](sapient/SAPIENT.md) — source and trust assessment for
  `vendors/sapient/flex335.py` and the vendored `compose/vendor/sapient_msg`
  protobuf schema.
- [`stanag/STANAG.md`](stanag/STANAG.md) — source and trust assessment for
  `vendors/stanag/stanag.py` (4586/4609/5516), including where confidence is
  lower and why.

## How to read the trust levels

Every entry below is rated one of:

- **Verified against real traffic** — decoded output has been checked
  against actual captured/received data from a real sensor. Highest
  confidence.
- **Verified against machine-readable spec, unverified against traffic** —
  decoded strictly from an authoritative, structured specification source
  (not prose), cross-checked for internal consistency, but never run
  against a real feed. This is most of the newly added ASTERIX categories.
- **Best-effort from mixed/secondary sources** — no single authoritative
  machine-readable spec existed; decoded from a combination of publicly
  documented standard text, widely-cited open literature, and/or existing
  open-source implementations. Lower confidence; specific gaps are called
  out per item.

## Methodology used for the ASTERIX categories (applies to all of `asterix-specs/ASTERIX.md`)

1. Fetch the category's `.ast` DSL file (see below) via `WebFetch`, which
   summarizes the page through a smaller/faster model rather than returning
   the literal text.
2. **This summarization step is the main source of risk.** Across this
   work it repeatedly produced internally self-contradictory bit-counts or
   byte-counts within a single response (e.g. stating an item is "2 bytes"
   while also listing subfields that sum to 3 bytes — caught for I011/600
   during this pass; similar catches happened for CAT-004, CAT-007,
   CAT-008, and CAT-009 items earlier in this project). Every such
   contradiction was resolved with a targeted re-fetch asking for the
   literal DSL syntax and bit widths verbatim, cross-checked by summing
   subfield bit widths against the item's stated total byte length.
3. Where a field's exact scale/bit-layout could not be resolved with
   confidence after re-fetching (e.g. CAT-007's I007/415 RIM sub-bits,
   CAT-008/009's F-dependent coordinate scale), it was kept as a raw,
   undecoded value with a code comment explaining why, rather than guessed.
4. Hand-construct synthetic byte sequences matching the spec and assert the
   decoder reads them back to the expected values. This is a
   **self-consistency check**: it proves the code implements what was read
   from the spec, not that the spec text itself was read correctly, and
   not that a real sensor conforms to this exact edition. See the
   per-category "Verified against machine-readable spec, unverified against
   traffic" rating for what this does and doesn't establish.
5. This pass (2026-08-01) additionally pulled the actual `.ast` file bytes
   for every implemented category directly into this repository (see
   `asterix-specs/`) and spot-checked them against what had been decoded,
   rather than relying solely on the AI-summarized fetch. This closes part
   of the gap in step 2 for categories implemented in earlier sessions,
   where the exact WebFetch prompts/responses were not retained verbatim —
   the downloaded file is the same URL pattern used throughout
   (`raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/catNNN/cat-X.Y.ast`),
   and its content was checked line-by-line against the categories' code
   comments and field tables during this pass.

## Full source list (every URL actually fetched and used)

This is the complete list of external sources this project's decoders were
built from — every GitHub repo, website, and PDF actually fetched and read,
not just mentioned in passing. Anything not on this list (stray search-engine
hits that were never opened, unrelated background reading) was **not** used
to write a decoder and is excluded so this list stays honest about what the
code actually rests on.

### ASTERIX (`vendors/asterix/cat.py`)

- [`zoranbosnjak/asterix-specs`](https://github.com/zoranbosnjak/asterix-specs)
  (GitHub repo, BSD-3-Clause) — the primary source for all 27 categories.
  Fetched two ways: the rendered site
  [`zoranbosnjak.github.io/asterix-specs`](https://zoranbosnjak.github.io/asterix-specs/)
  (used for the categories implemented earlier in this project — CAT-001,
  002, 004, 007, 008, 009, 019, 021, 023, 048, 062), and the raw `.ast` DSL
  files under
  [`specs/catNNN/`](https://github.com/zoranbosnjak/asterix-specs/tree/master/specs)
  fetched directly via `raw.githubusercontent.com` and copied into
  [`asterix-specs/`](asterix-specs/) in this repo (used from CAT-015 onward,
  and for CAT-011/016/017/018/025/032/065/150/205/240/247 — see
  `asterix-specs/ASTERIX.md` for the exact file per category). The repo's own
  [`LICENSE`](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/LICENSE)
  was fetched and copied alongside the spec files for attribution.
- [EUROCONTROL — list of ASTERIX categories and their statuses](https://www.eurocontrol.int/publication/list-asterix-categories-and-their-statuses)
  and the [2025-10-22 edition PDF](https://www.eurocontrol.int/sites/default/files/2025-10/categories-and-statuses-2025-10-22.pdf)
  — EUROCONTROL's own index of which category numbers exist and which
  edition is current, used to confirm CAT-011 through CAT-247 were the
  complete remaining set with a public spec before implementing them (task
  #112), and to check `asterix-specs`' eddition numbers against
  EUROCONTROL's own statement of the current edition.
- [pyModeS](https://github.com/junzis/pyModeS) (GitHub repo, GPL-3.0), a
  Mode-S/ADS-B decoder from TU Delft's Aerospace Engineering faculty — used
  as an independent cross-check for the Comm-B/BDS register bit-slices
  reused across several ASTERIX categories (I048/130, I048/120, BDS 3,0/4,0/
  5,0/6,0 helpers referenced by CAT-011/017/018/034/048). Specifically
  fetched:
  [`pyModeS/decoder/bds/bds50.py`](https://github.com/junzis/pyModeS/blob/master/pyModeS/decoder/bds/bds50.py)
  (BDS 5,0 track/turn report — roll angle, true track, ground speed, track
  rate, TAS bit slices) and its rendered API docs at
  [`mode-s.org/pymodes/api/_modules/pyModeS/decoder/bds/bds50.html`](https://mode-s.org/pymodes/api/_modules/pyModeS/decoder/bds/bds50.html).
  This caught a real discrepancy during this project (bit-slice boundaries
  for BDS 5,0 initially transcribed slightly wrong from a summarized fetch,
  corrected against pyModeS's actual source as ground truth).
- [mode-s.org — "The 1090MHz Riddle" (Junzi Sun)](https://mode-s.org/1090mhz/content/mode-s/7-ehs.html)
  and its [Comm-B chapter](https://mode-s.org/1090mhz/content/mode-s/8-commb.html)
  — the free companion book to pyModeS, used as prose cross-reference for
  Enhanced Surveillance (BDS 4,0/5,0/6,0) register layout alongside the raw
  source above.
- [`pyModeS/src/pyModeS/_uncertainty.py`](https://raw.githubusercontent.com/junzis/pyModeS/main/src/pyModeS/_uncertainty.py)
  — the NACp -> EPU (meters) lookup table (RTCA DO-260B Table 2-14), used to
  translate CAT-021 I021/090's NAC_p code into a `position_accuracy_m` field
  (`cat.py`'s `_cat21__NACP_EPU_M`), which feeds `tak_layer.py`'s `ce` (CoT
  position-uncertainty ring) for ADS-B tracks. Fetched directly rather than
  transcribed from memory — pyModeS's v3 restructure moved this file from
  `pyModeS/decoder/bds/` to `src/pyModeS/`, so the older `bds50.py` URL above
  no longer reflects the library's current layout on `main`.
- [`pyModeS/src/pyModeS/decoder/bds/bds10.py`](https://raw.githubusercontent.com/junzis/pyModeS/main/src/pyModeS/decoder/bds/bds10.py)
  and [`bds17.py`](https://raw.githubusercontent.com/junzis/pyModeS/main/src/pyModeS/decoder/bds/bds17.py)
  — BDS 1,0 (Data Link Capability Report) and BDS 1,7 (Common Usage GICB
  Capability Report) bit layouts, added to every category's GICB-extraction
  dispatch alongside the existing BDS 3,0/4,0/5,0/6,0 helpers (CAT-010, 011,
  018, 020, 021, 048, 062). BDS 6,1 (ADS-B Aircraft Status) was considered
  and deliberately not ported — it's an extended-squitter message (TC=28),
  not a ground-initiated Comm-B register, so it doesn't fit any of these
  categories' GICB dispatch sites.

### SAPIENT / BSI Flex 335 (`vendors/sapient/flex335.py`)

- [`dstl/SAPIENT-Proto-Files`](https://github.com/dstl/SAPIENT-Proto-Files)
  (GitHub repo, UK Dstl's own official protobuf schema) — the wire format
  itself, vendored into `compose/vendor/sapient_msg/bsi_flex_335_v2_0/`.
  Fetched directly:
  [`bsi_flex_335_v2_0/sapient_message.proto`](https://raw.githubusercontent.com/dstl/SAPIENT-Proto-Files/main/bsi_flex_335_v2_0/sapient_message.proto)
  and the repo's
  [`LICENCE.txt`](https://raw.githubusercontent.com/dstl/SAPIENT-Proto-Files/main/LICENCE.txt)
  (Apache-2.0), copied in for attribution. See `sapient/SAPIENT.md` for why this
  removes most of the transcription risk that applies to the ASTERIX work —
  the schema itself is the wire format, not a hand-transcribed bit layout.

### STANAG (`vendors/stanag/stanag.py`)

No single fetched URL underlies STANAG 4586/4607/4609/5516 the way
`asterix-specs` or the Dstl repo do for ASTERIX/SAPIENT — see
`stanag/STANAG.md` for why: 4586's real ICD and 5516's real standard
(MIL-STD-6016F) are both NATO/vendor-restricted documents, not freely
published, so nothing was fetched from a primary source for those two.
4609's underlying standard (MISB ST 0601, published by the US NGA's Motion
Imagery Standards Board at `gwg.nga.mil`) is genuinely public, but this
project worked from the already-known public KLV tag/length/value structure
rather than a fetch retained in this session — treat the 4609 KLV decoder as
built from general public-standard knowledge, not a specific cited page, and
the 4586/5516 paths as explicitly lower-confidence per `stanag/STANAG.md`.

- [Wireshark — `epan/dissectors/packet-stanag4607.c`](https://raw.githubusercontent.com/wireshark/wireshark/master/epan/dissectors/packet-stanag4607.c)
  (GPL-2.0-or-later) — the one STANAG exception with a genuine machine-readable
  ground truth. STANAG 4607 (NATO GMTI) itself is NATO-restricted like 4586's
  VSM ICD, but Wireshark's own dissector for this exact wire format is real,
  open-source, and actively maintained. Fetched and read directly (not
  summarized): the 32-byte packet header, every Dwell/Target Report
  existence-mask bit position, and every scale-factor formula (`prt_sa32`,
  `prt_ba32`, `prt_sa16`, `prt_ba16`, `prt_centimeters`, `prt_decimeters`,
  `prt_kilo`, `prt_speed`, `prt_speed_centi`, `prt_speed_deci`,
  `prt_millisec`) in `stanag.py`'s `_4607_*` functions are transcribed
  straight from this file's `dissect_dwell`/`dissect_target`/`dissect_mission`/
  `dissect_jobdef`/`dissect_platform_location` functions and its
  `hf_register_info` field table, not from memory. This is also the category
  where the sourcing paid off directly: a hand-built synthetic test packet
  caught a real transcription bug (four Dwell-segment optional-field bit
  positions off by one, discovered because the decode consumed the wrong
  number of bytes) before it shipped — see `stanag/STANAG.md` for detail.

## Why `asterix-specs` is trustworthy as a source

[`zoranbosnjak/asterix-specs`](https://github.com/zoranbosnjak/asterix-specs)
is a community-maintained, machine-readable transcription of EUROCONTROL's
published ASTERIX category specifications (the same specifications
EUROCONTROL itself distributes as PDF). It is not the official EUROCONTROL
publication — it's a third-party structured re-encoding of it — so it
carries transcription risk independent of our own reading of it. It is
licensed BSD-3-Clause (see `asterix-specs/LICENSE`), which is why its `.ast`
files could be copied directly into this repository with attribution
preserved. EFDI's own genuine captured traffic exists only for CAT-034 and
CAT-048 (see `asterix-specs/ASTERIX.md`); every other category has never been
cross-checked against a real feed and should be treated accordingly before
being relied on operationally.
