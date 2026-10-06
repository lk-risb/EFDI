"""Drop ghost aircraft: the same callsign (or registration) under an ICAO address one or two bits away.

A feed that decodes Mode-S without checking the CRC reports an occasional corrupted
address, so one flight shows up twice (seen live: ASL124 as 4C11EC and 4C01EC, CFC3455 as
C2C36D and 82C36D). The real address is the one seen first; a later, different address with
the same callsign that differs by at most MAX_BITS bits is treated as a bit-flip copy.
A registration is checked the same way as a second key: it stays the same across flights
(a callsign does not), so it also catches a copy that carries a different callsign.
"""

import threading
import time

MAX_BITS = 2
WINDOW_S = 600.0


class GhostFilter:
    def __init__(self, window_s: float = WINDOW_S):
        self._window = window_s
        self._seen: dict = {}           # "C:callsign" / "R:registration" -> {icao24: last seen}
        self._lock = threading.Lock()

    def is_ghost(self, icao24: str, callsign: str, now: float | None = None, registration: str | None = None) -> bool:
        """Register (icao24, callsign, registration) and say whether it is a bit-flip copy of an
        address already seen with the same callsign or the same registration. Records with
        neither are never ghosts."""
        keys = [prefix + value for prefix, value in (("C:", (callsign or "").strip().upper()),
                                                     ("R:", (registration or "").strip().upper())) if value]
        if not keys or not icao24:
            return False
        now = time.time() if now is None else now
        address = int(icao24, 16)
        ghost = False
        with self._lock:
            known_by_key = {k: {a: t for a, t in self._seen.get(k, {}).items() if now - t <= self._window} for k in keys}
            for known in known_by_key.values():
                if address not in known and any(bin(address ^ other).count("1") <= MAX_BITS for other in known):
                    ghost = True
            for key, known in known_by_key.items():
                if address in known or not ghost:
                    known[address] = now
                self._seen[key] = known
            if len(self._seen) > 20000:     # bound the memory on a long run
                self._seen = {c: v for c, v in self._seen.items() if v}
        return ghost
