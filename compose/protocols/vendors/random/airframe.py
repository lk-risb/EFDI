"""Does a name say the object is an unmanned aircraft? Shared by the decoders that get a drone with no declared type."""

import re

_AIRFRAME_WORDS = re.compile(r"drone|uav|copter|(?<!s)quad|mavic|matrice")   # not "squad"


def names_airframe(*names) -> bool:
    for name in names:
        for token in re.split(r"[^a-z0-9]+", str(name or "").lower()):
            if token in ("uas", "dji") or _AIRFRAME_WORDS.search(token):
                return True
    return False
