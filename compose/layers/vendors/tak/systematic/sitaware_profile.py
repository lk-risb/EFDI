"""SitaWare version profile: which defaults the SitaWare layers use for the version of SitaWare Headquarters they talk to.

SITAWARE_VERSION is 6.21 (the default) or 6.22. A profile only sets the DEFAULT of a setting; any setting that is set itself (in the
WebUI or the .env) wins, so versions can be mixed and matched.

6.21 is measured on a live SitaWare Headquarters 6.21 (docs/references/nffi/NFFI.md): NFFI 1.3, the letter symbol only (it drew the numeric
APP-6D individual as a "?" in NVG and as a plain frame in FFI), so the TAK users go out as NFFI force tracks and stay out of the NVG feed.
6.22 is an ASSUMPTION that has not been measured: the numeric APP-6D dismounted individual is expected to draw only through the NVG feed, so
its profile sends the TAK users there under the app06 scheme tag (2525d is the other tag to try); the NFFI and FFI routes are assumed to stay letter-only (NFFI 1.3, the APP-6(B)
symbol standard). Check each of them on a real 6.22 and correct the table below.
"""

import os

VERSION = os.environ.get("SITAWARE_VERSION", "6.21").strip() or "6.21"

PROFILES = {
    "6.21": {
        "NFFI_LAYER_VERSION": "1.3",
        "FFI_LAYER_SYMBOL_STANDARD": "APP-6(B)",
        "SITAWARE_TAK_INDIVIDUAL_SCHEME": "",
        "SITAWARE_TAK_USERS_NVG": "",          # blank: the NVG feed draws the TAK users unless an NFFI or FFI feed is configured
    },
    "6.22": {
        "NFFI_LAYER_VERSION": "1.3",
        "FFI_LAYER_SYMBOL_STANDARD": "APP-6(B)",
        "SITAWARE_TAK_INDIVIDUAL_SCHEME": "app06",      # untested guess; "2525d" is the other candidate; "app6d" drew a ? on 6.21
        "SITAWARE_TAK_USERS_NVG": "true",
    },
}


def profile() -> dict:
    """The defaults of the selected version; an unknown version falls back to 6.21 with a note."""
    if VERSION not in PROFILES:
        print("SITAWARE_VERSION {!r} is not one of {}: using the 6.21 defaults".format(VERSION, ", ".join(PROFILES)), flush=True)
    return PROFILES.get(VERSION, PROFILES["6.21"])


def setting(key: str) -> str:
    """The value of `key` from the environment, else the version profile's default."""
    value = os.environ.get(key, "").strip()
    return value if value else profile().get(key, "")
