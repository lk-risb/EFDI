# Symbology (SIDC / CoT types)

Where the symbol codes in `tak_layer.py`, `sitaware_layer.py`, `airframe.py` and `ground_unit.py` come from, and how far they are verified.
Rule: NATO APP-6 is preferred over MIL-STD-2525 where they differ, and codes are checked against a live source, not written from memory.

## What is used

- The SitaWare feed uses letter-based 15-character SIDCs (APP-6B / MIL-STD-2525B-C lineage): index 0 scheme, 1 affiliation, 2 dimension,
  3 status, 4-9 function id, 10-11 symbol modifiers (index 11 is the echelon), 12-13 country, 14 order of battle.
- ATAK / WinTAK are fed CoT types (`a-<affiliation>-<dimension>-...`), whose letters follow the same function tree.
- The numeric 20-digit SIDC of APP-6D/E is not used.

## Verified on 2026-10-13

| Code | Meaning | Source |
|---|---|---|
| echelon at SIDC index 11: F battalion, G regiment, H brigade, I division, J corps, K army | unit size | milsymbol SIDC documentation (https://mintlify.wiki/spatialillusions/milsymbol/concepts/sidc) |
| `UCI`, `UCIZ`, `UCIA`, `UCA`, `UCF`, `UCD`, `UCR`, `UCE` | ground combat unit: infantry, mechanised, airborne, armour, field artillery, air defence, reconnaissance, engineer | symbol.army list of MIL-STD-2525C symbols (https://www.symbol.army/doc/en/more/list-of-mil-std-2525c-symbols/) |
| `APMH` rotary wing, `APCH` civil rotary wing, `APCF` civil fixed wing | air symbols | same list |
| `APMFC` cargo, `APMFU` utility, `APMFP` patrol, `APMFR` reconnaissance, `APMFT` trainer, `APMFK` tanker, `APMFB` bomber, `APMFF` fighter | military fixed-wing roles | same list and the 2525C codes it quotes |
| `OFVPE-----*****` (`OHVPE`, `ONVPE`, `OUVPE` by affiliation) | Stability Operations, Violent Activities, Explosion; used for strike markers | same list (coding scheme O; it was `S?GPIE----` before, which SitaWare drew as an unknown "?") |
| `UH----` special C2 headquarters component, `USM---` medical, `UUS---` signal unit (also `UULM--` military police, `UUA---` CBRN, `USX---` maintenance, listed but not used) | ground units for TAK roles HQ, medic, RTO | same list |
| APP-6D symbol set 27 (Dismounted Individuals): entities `110000` military, `110101` infantry, `110102` medical, `110103` reconnaissance, `110104` signal, `110202` field artillery observer, `110209` sniper; modifier 1 `46` Individual | a TAK client is drawn as a person icon | APP-6D symbol set tables as JSON, `milstd` package (https://cdn.jsdelivr.net/npm/milstd@0.1.6/JSON/APP6-D 27 dismounted-individual.json) |
| number-based SIDC layout: version 0-1 (10 = APP-6D), context 2, identity 3 (3 = friend), symbol set 4-5, status 6, HQ/task force/dummy 7, amplifier 8-9, function id 10-19 (entity 6, modifier 1, modifier 2) | the 20-digit code sent as `app6d:` | milsymbol SIDC documentation |

## Not verified

- The NATO APP-6 text itself was not retrieved; the sources above are third-party renderings of the 2525C / APP-6B code lists.
- `APMFA` (attack) and the ICAO-type-to-role lists in `airframe.py` and the name-to-arm lists in `deepstate_bridge.py` are our own mappings.
- ATAK / WinTAK have no icon for the air role subtypes (a letter is drawn in the frame), so roles go out in the SitaWare SIDC only.
- The CoT type `a-?-G-I-E` that ATAK / WinTAK get for strikes was not checked against TAK's CoTtypes.xml; it draws, but may not be the Explosion symbol.
- Whether each C2 draws every ground-unit subtype icon has not been checked on screen.
- The headquarters / task-force amplifier (symbol modifier 1) has no checked letter, so it is not used: the TAK role HQ is drawn as the C2 headquarters component. Sniper and forward observer were not found in the unit list and stay the plain unit.
- Sources disagree on SIDC index positions of the modifiers (modifiers at positions 11-12 in 1-based counting, echelon the second); the code uses index 10-11 zero-based and the echelon at index 11.
- Whether the SitaWare in use accepts the `app6d:` scheme in the NVG feed, and draws set 27, was not checked; the code that sends it (a TAK client's 20-digit SIDC) has no letter fallback.

## TAK users in SitaWare force tracking (added 2026-10-13)

SitaWare 6.21 draws a TAK user from the NFFI feed (`nffi_layer.py`) as a friendly ground unit with the Team echelon. The NFFI `unitSymbol`
field is `[A-Z0-9-]{15}`, so it can hold only the letter code: the numeric APP-6D dismounted individual (symbol set 27, see above) does not
fit. A user with no role is therefore sent as infantry (`SFGPUCI----A---`), the letter-code counterpart of the APP-6D infantry individual
(`110101`); HQ, medic and signal roles keep their function ids. This pairing is a choice, not a table in either standard.

The numeric individual stays available on the NVG route (`sitaware_layer.py`, `SITAWARE_TAK_INDIVIDUAL_SCHEME`), for a SitaWare that draws
it; whether 6.22 does is not checked. `SITAWARE_TAK_USERS_NVG` switches that route on or off, see `06-atak-and-sitaware-setup.md`.
