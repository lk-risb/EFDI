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

## Not verified

- The NATO APP-6 text itself was not retrieved; the sources above are third-party renderings of the 2525C / APP-6B code lists.
- `APMFA` (attack) and the ICAO-type-to-role lists in `airframe.py` and the name-to-arm lists in `deepstate_bridge.py` are our own mappings.
- ATAK / WinTAK have no icon for the air role subtypes (a letter is drawn in the frame), so roles go out in the SitaWare SIDC only.
- Whether each C2 draws every ground-unit subtype icon has not been checked on screen.
