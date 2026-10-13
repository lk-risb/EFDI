"""What an aircraft is, from the names a feed gives it. Shared by the decoders that get a drone with no declared type, and by
the layers that draw a helicopter with the rotary-wing symbol instead of the fixed-wing one."""

import re

_AIRFRAME_WORDS = re.compile(r"drone|uav|copter|(?<!s)quad|mavic|matrice")   # not "squad"


def names_airframe(*names) -> bool:
    for name in names:
        for token in re.split(r"[^a-z0-9]+", str(name or "").lower()):
            if token in ("uas", "dji") or _AIRFRAME_WORDS.search(token):
                return True
    return False


# ICAO type designators (Doc 8643) of helicopters, for the aircraft types ADS-B feeds report ("H60", "MI8", "EC35").
_ROTORCRAFT = frozenset("""
H1 H2 H46 H47 H53 H60 H64 UH1 UH60 CH47 CH53 AH64 HH60 SH60 MH60 S58 S61 S64 S65 S70 S76 S92 S300
NH90 EH10 EC20 EC25 EC30 EC35 EC45 EC55 EC75 AS32 AS33 AS35 AS50 AS55 AS65 AS3B AS3 A109 A119 A139 A149 A169 A189 AW09
TIGR LYNX GAZL PUMA MI2 MI4 MI6 MI8 MI14 MI17 MI24 MI26 MI28 MI34 KA26 KA27 KA29 KA31 KA32 KA50 KA52
B06 B06T B105 B212 B214 B222 B230 B407 B412 B427 B429 B430 B47G B47J R22 R44 R66 H269 H500 MD52 MD60 MD90 MDHI EXPL
""".split())


def is_rotorcraft(aircraft_type) -> bool:
    return re.sub(r"[^A-Z0-9]", "", str(aircraft_type or "").upper()) in _ROTORCRAFT


# The role of a military aircraft, as the 2525 function letter that follows fixed wing (F) / rotary wing (H): A attack, B bomber,
# C cargo, F fighter, K tanker, P patrol, R reconnaissance / airborne early warning, T trainer, U utility. By ICAO type designator;
# a type not listed here is drawn without a role.
_ROLE_TYPES = {
    "F": {
        "F": "F16 F15 F18 F18H FA18 F35 F22 F5 F14 EUFI RFAL GRIF JAS39 MG29 MG31 SU27 SU30 SU34 SU35 SU57 M2KC MIR2 F2TH",
        "A": "A10 SU25 SU24 TORN AV8B",
        "B": "B52 B1 B2 TU95 TU160 TU22",
        "C": "C130 C30J C17 C5M C5 A400 C295 C27J CN35 C160 IL76 AN12 AN26 AN24 AN32 AN72 A124 A225",
        "K": "K35R K35T KC10 K46 IL78",
        "P": "P8 P3 P3C ATLA",
        "R": "E3TF E3CF E3 E2 AN30 U2",
        "T": "T6 T38 T45 T37 M346 PC21 PC9 PC7 HAWK L39 L159",
        "U": "PC12 L410 BE20 DHC6 C208",
    },
    "H": {
        "A": "H64 AH64 MI24 MI28 KA50 KA52 TIGR",
        "C": "H47 CH47 H53 CH53 MI26",
        "U": "H60 UH60 HH60 SH60 MH60 S70 S70A MI8 MI17 NH90 AS32 H1 UH1",
    },
}
_ROLE_OF = {wing: {code: role for role, codes in table.items() for code in codes.split()} for wing, table in _ROLE_TYPES.items()}


def military_role(aircraft_type) -> str:
    """The 2525 role letter of a military aircraft ("C" for a C295, "U" for a Mi-8), or "" when the type is not listed."""
    code = re.sub(r"[^A-Z0-9]", "", str(aircraft_type or "").upper())
    return _ROLE_OF["H" if is_rotorcraft(code) else "F"].get(code, "")
