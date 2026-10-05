"""Drop ghost aircraft: the same callsign under an ICAO address one or two bits away.

A feed that decodes Mode-S without checking the CRC reports an occasional corrupted
address, so one flight shows up twice (seen live: ASL124 as 4C11EC and 4C01EC, CFC3455 as
C2C36D and 82C36D). The real address is the one seen first; a later, different address with
the same callsign that differs by at most MAX_BITS bits is treated as a bit-flip copy.
"""

import threading
import time

MAX_BITS = 2
WINDOW_S = 600.0


class GhostFilter:
    def __init__(self, window_s: float = WINDOW_S):
        self._window = window_s
        self._seen: dict = {}           # callsign -> {icao24: last seen}
        self._lock = threading.Lock()

    def is_ghost(self, icao24: str, callsign: str, now: float | None = None) -> bool:
        """Register (icao24, callsign) and say whether it is a bit-flip copy of an address
        already seen with the same callsign. Records without a callsign are never ghosts."""
        callsign = (callsign or "").strip().upper()
        if not callsign or not icao24:
            return False
        now = time.time() if now is None else now
        address = int(icao24, 16)
        with self._lock:
            known = {a: t for a, t in self._seen.get(callsign, {}).items() if now - t <= self._window}
            if address in known:
                known[address] = now
                self._seen[callsign] = known
                return False
            ghost = any(bin(address ^ other).count("1") <= MAX_BITS for other in known)
            if not ghost:
                known[address] = now
            self._seen[callsign] = known
            if len(self._seen) > 20000:     # bound the memory on a long run
                self._seen = {c: v for c, v in self._seen.items() if v}
        return ghost
