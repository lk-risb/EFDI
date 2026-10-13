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
ECHELON = {"battalion": "F", "regiment": "G", "brigade": "H", "division": "I", "corps": "J", "army": "K"}


def cot_suffix(arm) -> str:
    """"-I-Z" for mechanised infantry: the function letters after UC, as CoT writes them; "" for an unknown arm."""
    function = FUNCTION.get(str(arm or ""), "UC----")[2:].rstrip("-")
    return "".join("-" + letter for letter in function)
