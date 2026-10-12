"""Keeps a marker from flipping between a declared and an undeclared identity.

Two feeds can describe one object with the same uid, one declaring its affiliation (friendly, hostile, neutral) and
one not. Whatever arrives last would redraw the marker as friendly, then unknown, then friendly again. MarkerMemory
remembers, per uid, the last symbol that declared an affiliation, and keeps returning it for HOLD_S after an
undeclared one arrives. A declared symbol always replaces the remembered one at once.
"""

import threading
import time

HOLD_S = 300.0
_UNDECLARED_COT = "u", "p"            # a-<affiliation>-...: unknown, pending
_UNDECLARED_SIDC = "U", "P"           # S<affiliation>...: unknown, pending


def _declared(code: str) -> bool | None:
    """Whether a CoT type (a-f-A) or 2525B SIDC (SFAP------*****) declares an affiliation; None if it is neither."""
    if code.startswith("a-") and len(code) > 2:
        return code[2] not in _UNDECLARED_COT
    if len(code) >= 2 and code[0] == "S":
        return code[1] not in _UNDECLARED_SIDC
    return None


class MarkerMemory:
    def __init__(self, hold_s: float = HOLD_S):
        self._hold = hold_s
        self._last: dict = {}           # uid -> (seen, code that declared an affiliation)
        self._lock = threading.Lock()

    def settle(self, uid: str, code: str, now: float | None = None) -> str:
        """The code to draw for uid, given the one its latest message resolved to."""
        declared = _declared(code)
        if declared is None:
            return code
        now = time.time() if now is None else now
        with self._lock:
            if declared:
                self._last[uid] = (now, code)
                return code
            seen, kept = self._last.get(uid, (0.0, None))
            if kept is not None and now - seen <= self._hold:
                return kept
            if len(self._last) > 20000:
                self._last = {u: v for u, v in self._last.items() if now - v[0] <= self._hold}
            return code
