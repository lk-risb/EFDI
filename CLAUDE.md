# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## Project: efdi-moon-pod ASTERIX Bridge

### What this is
ASTERIX radar bridge → Zenoh pub/sub → CoT XML → ATAK. Decodes CAT-34 (monoradar service), CAT-48 (monoradar targets), CAT-21 (ADS-B), CAT-20 (MLAT), CAT-62 (system tracks). Key files:
- `compose/bridge/bridges/asterix_bridge.py` — all ASTERIX decode logic
- `compose/bridge/layers/cot_layer.py` — CoT XML builder / ATAK info card

### After every edit
```
python3 -m py_compile compose/bridge/bridges/asterix_bridge.py
python3 -m py_compile compose/bridge/layers/cot_layer.py
```

### ASTERIX bit numbering (most common bug source)
EUROCONTROL numbers bits 8→1 (MSB first). Python uses 7→0. For a single byte:
- EURO bit 8 = Python bit 7 = `0x80`
- EURO bit 7 = Python bit 6 = `0x40`
- EURO bit 1 = Python bit 0 = `0x01`

When reading the spec "bits 8-6" → Python mask `0xE0`, shift `>> 5`.

### ASTERIX FX variable-length fields
Bit 0 of each byte = FX (1 = more bytes follow). Pattern:
```python
while b & 0x01:
    if pos >= len(data): break
    b = data[pos]; pos += 1
```
Never break this into individual bit flags — the whole octet is one repeating unit.

### ASTERIX compound fields (PSF-gated)
First byte = Primary Sub-Field bitmask. Each set bit gates one sub-field that follows in order. Always consume ALL bytes of a sub-field even if only using some bits. Wrong size = corrupts every subsequent FRN.

### BDS register gotchas
BDS registers use ICAO bit numbering (bit 1 = MSB). Helpers `_bit(n)`, `_uns(a,b)`, `_sgn(a,b)` in `_decode_bds50/60` count from bit 1.

- **Heading/track fields are UNSIGNED**: `_uns(start, end) * 360.0 / 1024.0` — NOT `_sgn * 90/512`. Signed encoding cannot represent 90°–270°.
- **Roll angle IS signed**: `_sgn(2, 11) * 45.0 / 256.0` (BDS 5,0 scale, ±90°)
- **I021/230 and I062/380 sub-03 roll**: ASTERIX-native s16, different scale `45/512` — do NOT change to BDS scale
- **Vertical rates**: signed (can be negative) ✓
- **Speeds (GS, TAS, IAS)**: unsigned ✓

### Git commits
Never run `git commit` in this repo. Stage changes and describe them — the user commits manually, every time, no exceptions (including steps in plans/skills that say "commit").

### Security constraints (never violate)
- Certs never in repo
- `compose/.env` is gitignored — stays local only
- `BUNDLE_DIR`, `register_topics.sh` — gitignored, never commit
- Personal namespace UUID removed from all tracked files; bridges read from `PARTNER_NAMESPACE` env var
- `EFDI_PORTAL_KEY`, `EFDI_VENDOR_SLUG` — from environment only, never hardcoded
- Real API keys in local `compose/.env` only

### Known recurring bug patterns (found across multiple audit passes)
1. **Wrong bit position**: spec says "bit 6" but code checks `0x10` instead of `0x20` — always count from 0x80 down
2. **Signed vs unsigned for heading fields**: signed 10-bit at 90/512 can't reach 90°–270°
3. **Compound sub-field size wrong**: pos advances by N but field is N+k bytes — silently corrupts every FRN after it. Example: I048/130 RPD and APD are s8 (1 byte each), not s16 (2 bytes). Check physical plausibility of value range to detect wrong size.
4. **FX loop treated as flag bits**: I048/030 type codes are repeating `(code << 1) | FX` octets, not individual named flags
5. **PSF2 extension bytes consumed as data**: after a compound field's PSF byte, if FX is set, read the NEXT PSF byte — don't treat PSF extension bytes as sub-field data
