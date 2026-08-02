# ASTERIX category sources

Every row below is decoded in `../../../compose/protocols/vendors/asterix/cat.py`.
"Local copy" is the actual `.ast` DSL file pulled into this repo alongside
this file — open it directly to check any field against the primary
source rather than trusting this table or the code comments.

| CAT | Edition | Source URL | Local copy | Trust |
| --- | --- | --- | --- | --- |
| 001 | 1.4 | [specs/cat001/cat-1.4.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat001/cat-1.4.ast) | `cat001/cat-1.4.ast` | Verified against machine-readable spec, unverified against traffic |
| 002 | 1.2 | [specs/cat002/cat-1.2.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat002/cat-1.2.ast) | `cat002/cat-1.2.ast` | Verified against machine-readable spec, unverified against traffic |
| 004 | 1.13 | [specs/cat004/cat-1.13.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat004/cat-1.13.ast) | `cat004/cat-1.13.ast` | Verified against machine-readable spec, unverified against traffic — I004/170 RIM-style raw blocks kept undecoded where the bit order could not be confirmed |
| 007 | 1.12 | [specs/cat007/cat-1.12.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat007/cat-1.12.ast) | `cat007/cat-1.12.ast` | Verified against machine-readable spec, unverified against traffic — I007/415 RIM (~30 Mode 4/5 control bits) kept as a raw 4-byte block, not decoded bit-by-bit; see the code comment in `cat.py` |
| 008 | 1.3 | [specs/cat008/cat-1.3.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat008/cat-1.3.ast) | `cat008/cat-1.3.ast` | Verified against machine-readable spec, unverified against traffic — coordinate items kept as raw pre-scale integers (F-dependent scale ambiguity) |
| 009 | 2.1 | [specs/cat009/cat-2.1.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat009/cat-2.1.ast) | `cat009/cat-2.1.ast` | Verified against machine-readable spec, unverified against traffic — same coordinate-scale caveat as CAT-008 |
| 010 | 1.1 | [specs/cat010/cat-1.1.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat010/cat-1.1.ast) | `cat010/cat-1.1.ast` | Verified against machine-readable spec, unverified against traffic — I010/250 GICB extraction also resolves BDS 1,0 (Data Link Capability Report) and BDS 1,7 (Common Usage GICB Capability Report) codes using layouts read directly from [pyModeS](https://github.com/junzis/pyModeS)'s `src/pyModeS/decoder/bds/bds10.py` and `bds17.py` |
| 011 | 1.3 | [specs/cat011/cat-1.3.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat011/cat-1.3.ast) | `cat011/cat-1.3.ast` | Verified against machine-readable spec, unverified against traffic — flight-plan-management subitems inside I011/390 (IFPSFLIGHTID/FLIGHTCAT/STS sub-bit tables) kept as raw ints, their enum tables were not independently confirmed. GICB extraction also resolves BDS 1,0/1,7 using [pyModeS](https://github.com/junzis/pyModeS)'s `bds10.py`/`bds17.py` |
| 015 | 1.2 | [specs/cat015/cat-1.2.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat015/cat-1.2.ast) | `cat015/cat-1.2.ast` | Verified against machine-readable spec, unverified against traffic — this category's full text (not just an AI-summarized fetch) was read directly line-by-line before implementing, so every field is decoded, none kept raw |
| 016 | 1.0 | [specs/cat016/cat-1.0.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat016/cat-1.0.ast) | `cat016/cat-1.0.ast` | Verified against machine-readable spec, unverified against traffic — small category (221-line spec), full text read directly, no field kept raw |
| 017 | 1.3 | [specs/cat017/cat-1.3.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat017/cat-1.3.ast) | `cat017/cat-1.3.ast` | Verified against machine-readable spec, unverified against traffic — full text read directly; I017/045's declared +-90/+-180 lat/lon range doesn't fit its literal 24-bit width at the declared scale (tops out ~+-45 deg), decoded literally as transmitted with a code comment explaining the discrepancy rather than guessed |
| 018 | 1.8 | [specs/cat018/cat-1.8.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat018/cat-1.8.ast) | `cat018/cat-1.8.ast` | Verified against machine-readable spec, unverified against traffic — full text read directly. I018/029 "GICB Extracted" is typed `bds ?` in the spec (content depends on the BDS code named by I018/027, decoded earlier in the same record) and is resolved using this file's own BDS 1,0/1,7/3,0/4,0/5,0/6,0 helpers when the code matches, else kept as raw hex — nothing guessed. BDS 1,0/1,7 layouts sourced from [pyModeS](https://github.com/junzis/pyModeS)'s `bds10.py`/`bds17.py`. Only I018/019 "Mode S Packet" (a genuinely payload-opaque explicit field) is kept as a raw hex blob |
| 019 | 1.3 | [specs/cat019/cat-1.3.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat019/cat-1.3.ast) | `cat019/cat-1.3.ast` | Verified against machine-readable spec, unverified against traffic |
| 020 | 1.11 | [specs/cat020/cat-1.11.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat020/cat-1.11.ast) | `cat020/cat-1.11.ast` | Verified against machine-readable spec, unverified against traffic — GICB extraction also resolves BDS 1,0/1,7 using [pyModeS](https://github.com/junzis/pyModeS)'s `bds10.py`/`bds17.py` |
| 021 | 2.7 | [specs/cat021/cat-2.7.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat021/cat-2.7.ast) | `cat021/cat-2.7.ast` | Verified against machine-readable spec, unverified against traffic — I021/090's NAC_p code (0-11) is additionally translated to a `position_accuracy_m` (95% EPU) value using the table in [pyModeS](https://github.com/junzis/pyModeS)'s `src/pyModeS/_uncertainty.py` (`NACp` dict), itself citing RTCA DO-260B Table 2-14; codes 12-15 are reserved and left untranslated rather than guessed. GICB extraction also resolves BDS 1,0/1,7 using pyModeS's `bds10.py`/`bds17.py` |
| 023 | 1.3 | [specs/cat023/cat-1.3.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat023/cat-1.3.ast) | `cat023/cat-1.3.ast` | Verified against machine-readable spec, unverified against traffic |
| 025 | 1.6 | [specs/cat025/cat-1.6.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat025/cat-1.6.ast) | `cat025/cat-1.6.ast` | Verified against machine-readable spec, unverified against traffic — full text read directly; error-code and statistics-type tables (values 10-63/5-255) are almost entirely "reserved for allocation" placeholders in the spec, kept as their numeric code rather than a fabricated name |
| 032 | 1.2 | [specs/cat032/cat-1.2.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat032/cat-1.2.ast) | `cat032/cat-1.2.ast` | Verified against machine-readable spec, unverified against traffic — full text read directly; I032/035's NATURE subfield is a `case 035/FAMILY` construct resolved by decoding FAMILY first, not guessed |
| 034 | 1.29 | [specs/cat034/cat-1.29.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat034/cat-1.29.ast) | `cat034/cat-1.29.ast` | **Verified against real traffic** — EFDI has received and decoded actual CAT-034 radar service messages |
| 048 | 1.32 | [specs/cat048/cat-1.32.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat048/cat-1.32.ast) | `cat048/cat-1.32.ast` | **Verified against real traffic** — EFDI has received and decoded actual CAT-048 target reports. GICB extraction also resolves BDS 1,0/1,7 using [pyModeS](https://github.com/junzis/pyModeS)'s `bds10.py`/`bds17.py` |
| 062 | 1.21 | [specs/cat062/cat-1.21.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat062/cat-1.21.ast) | `cat062/cat-1.21.ast` | Verified against machine-readable spec, unverified against traffic — I062/295 partially sum-and-discard, I062/390 partially decoded (see code comments). Both of this category's GICB extraction sites also resolve BDS 1,0/1,7 using [pyModeS](https://github.com/junzis/pyModeS)'s `bds10.py`/`bds17.py` |
| 063 | 1.7 | [specs/cat063/cat-1.7.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat063/cat-1.7.ast) | `cat063/cat-1.7.ast` | Verified against machine-readable spec, unverified against traffic |
| 065 | 1.6 | [specs/cat065/cat-1.6.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat065/cat-1.6.ast) | `cat065/cat-1.6.ast` | Verified against machine-readable spec, unverified against traffic — small category (152-line spec), full text read directly, no field kept raw |
| 150 | 3.0 | [specs/cat150/cat-3.0.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat150/cat-3.0.ast) | `cat150/cat-3.0.ast` | Verified against machine-readable spec, unverified against traffic — full text read directly; I150/151 (WGS-84 route point position) is defined in the spec's item catalogue but is genuinely absent from this edition's own UAP list (confirmed by reading the raw uap block, not summarized), so it is not implemented — nothing was guessed in its place |
| 205 | 1.0 | [specs/cat205/cat-1.0.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat205/cat-1.0.ast) | `cat205/cat-1.0.ast` | Verified against machine-readable spec, unverified against traffic — full text read directly, no field kept raw |
| 240 | 1.3 | [specs/cat240/cat-1.3.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat240/cat-1.3.ast) | `cat240/cat-1.3.ast` | Verified against machine-readable spec, unverified against traffic — full text read directly; the video cell blocks (I240/050/051/052) have no ASTERIX-defined internal structure beyond the bit-resolution stated in I240/048, so they are preserved as raw hex rather than guessed apart — a genuine, documented difference from every other category, not an ambiguity gap |
| 247 | 1.3 | [specs/cat247/cat-1.3.ast](https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/cat247/cat-1.3.ast) | `cat247/cat-1.3.ast` | Verified against machine-readable spec, unverified against traffic — small category (81-line spec), full text read directly, no field kept raw |

**Only CAT-034 and CAT-048 have been checked against real sensor traffic.**
Every other row was implemented purely from the public specification
(method described in [`../../../README.md`](../README.md)) because EFDI's operating
rule is to translate every protocol it can find a public spec for, not just
the ones currently in active use. Treat any of those rows as **unverified
against a live feed** until real captured traffic is available for it —
the same caveat that applied to CAT-034/048 before real data existed for
them.

Every ASTERIX category with a public spec in the `asterix-specs` repository
is now implemented: CAT-011, CAT-015, CAT-016, CAT-017, CAT-018, CAT-025,
CAT-032, CAT-065, CAT-150, CAT-205, CAT-240, and CAT-247 were all completed
in this pass, alongside the categories already implemented before it
(001, 002, 004, 007, 008, 009, 010, 019, 020, 021, 023, 034, 048, 062, 063).
Twenty-seven categories total, all with a decoder in `cat.py`, none guessed
past what their source text actually says.

## If a new category appears later

`asterix-specs` occasionally gains new categories or new editions of
existing ones. The source URL pattern is
`https://raw.githubusercontent.com/zoranbosnjak/asterix-specs/master/specs/catNNN/cat-X.Y.ast`
— check the repository's `specs/catNNN/` directory listing for the current
edition before implementing or re-verifying one.
