"""What a ground unit is, as the 2525B function and size codes both layers draw it with.

A bridge that knows the arm of service of a unit and its size (DeepState names its Russian units "geoJSON.units.<size>.<name>") puts
`arm` and `echelon` on the track; the layers turn them into a CoT type (ATAK / WinTAK draw the 2525 branch icon of a-h-G-U-C-I and so on)
and a SIDC (SitaWare), instead of the generic Combat unit that ATAK / WinTAK label "CBT".
Codes checked 2026-10-13, see docs/references/symbology/SYMBOLOGY.md.
"""

# arm -> 2525B function id of a ground combat unit (UC = combat arms)
FUNCTION = {
    "infantry": "UCI---", "mech_infantry": "UCIZ--", "airborne": "UCIA--", "armor": "UCA---",
    "artillery": "UCF---", "recon": "UCR---", "engineer": "UCE---", "air_defence": "UCD---",
}
# size -> 2525B size letter (SIDC position 12)
ECHELON_TEAM = "A"       # team / crew: the smallest unit, one TAK user
ECHELON = {"battalion": "F", "regiment": "G", "brigade": "H", "division": "I", "corps": "J", "army": "K"}


def cot_suffix(arm) -> str:
    """"-I-Z" for mechanised infantry: the function letters after UC, as CoT writes them; "" for an unknown arm."""
    function = FUNCTION.get(str(arm or ""), "UC----")[2:].rstrip("-")
    return "".join("-" + letter for letter in function)


# TAK role of a user (the __group role of its position) -> 2525C function id (symbol.army 2525C list, checked 2026-10-13):
# UH special C2 headquarters component, USM medical, UUS signal unit. Other roles (Team Lead, Sniper, Forward Observer, K9...)
# have no entry that was checked and stay the plain unit.
TAK_ROLE_FUNCTION = {"hq": "UH----", "medic": "USM---", "rto": "UUS---"}


def tak_function(cot_type, role):
    """The function id for a TAK user: from its role when that has a checked entry, else from the specific CoT type
    (a-f-G-U-C-I -> UCI---), else "" (the plain unit). Only function ids this module knows are returned."""
    known = set(FUNCTION.values())
    by_role = TAK_ROLE_FUNCTION.get(str(role or "").strip().lower().replace(" ", ""))
    if by_role:
        return by_role
    parts = str(cot_type or "").split("-")
    if len(parts) > 4 and parts[2].upper() == "G" and parts[3].upper() == "U":
        function = "".join(p.upper() for p in parts[3:])[:6].ljust(6, "-")
        if function in known:
            return function
    return ""


# A TAK user is one person: APP-6D symbol set 27 (Dismounted Individuals), a person icon. Entity codes (APP-6D set 27 table, checked
# 2026-10-13): 110000 military, 110101 infantry, 110102 medical, 110103 reconnaissance, 110104 signal, 110202 field artillery observer,
# 110209 sniper. Modifier 1 46 is "Individual". Numeric 20-digit SIDC: version 10, context 0, identity 3 (friend), set 27, status 0,
# no HQ/task force/dummy, no amplifier (00), entity (6), modifier 1 (2), modifier 2 (2).
INDIVIDUAL_ROLE_ENTITY = {"medic": "110102", "rto": "110104", "sniper": "110209", "forwardobserver": "110202"}
INDIVIDUAL_TYPE_ENTITY = {"I": "110101", "R": "110103"}      # a-f-G-U-C-I infantry, -R reconnaissance
INDIVIDUAL_MODIFIER_1 = "46"


def tak_individual_sidc(cot_type, role) -> str:
    """The APP-6D dismounted-individual SIDC of a TAK user, from its role, else its specific CoT type, else a plain military individual."""
    entity = INDIVIDUAL_ROLE_ENTITY.get(str(role or "").strip().lower().replace(" ", ""))
    if not entity:
        parts = str(cot_type or "").upper().split("-")
        entity = INDIVIDUAL_TYPE_ENTITY.get(parts[5], "") if len(parts) > 5 and parts[2:5] == ["G", "U", "C"] else ""
    return "1003270000{}{}00".format(entity or "110000", INDIVIDUAL_MODIFIER_1)
