"""Drop ghost aircraft: the same callsign (or registration) under an ICAO address one or two bits away.

A feed that decodes Mode-S without checking the CRC reports an occasional corrupted
address, so one flight shows up twice (seen live: ASL124 as 4C11EC and 4C01EC, CFC3455 as
C2C36D and 82C36D). The real address is the one seen first; a later, different address with
the same callsign that differs by at most MAX_BITS bits is treated as a bit-flip copy.
A registration is checked the same way as a second key: it stays the same across flights
(a callsign does not), so it also catches a copy that carries a different callsign.
"""

import math
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


class TwinFilter:
    """Drop a second record for the same aircraft that has no ICAO address.

    A partner feed also sends an aircraft under a UUID (seen live: "OMCBS" as a ground marker
    beside the ICAO-keyed "OMCBS"). A record without icao24 whose callsign equals that of an
    ICAO-keyed record seen in the last WINDOW_S seconds within RADIUS_KM is that aircraft's twin.
    """

    WINDOW_S = 180.0
    RADIUS_KM = 50.0            # an airliner covers about 45 km in WINDOW_S, and the UUID copy is sent only every few minutes

    def __init__(self):
        self._seen: dict = {}           # callsign -> (lat, lon, time) of an ICAO-keyed record
        self._lock = threading.Lock()

    def is_twin(self, record: dict, now: float | None = None) -> bool:
        callsign = str(record.get("callsign") or "").strip().upper()
        lat, lon = record.get("lat_deg"), record.get("lon_deg")
        if len(callsign) < 3 or lat is None or lon is None:
            return False
        now = time.time() if now is None else now
        with self._lock:
            if record.get("icao24"):
                self._seen[callsign] = (lat, lon, now)
                if len(self._seen) > 20000:
                    self._seen = {c: v for c, v in self._seen.items() if now - v[2] <= self.WINDOW_S}
                return False
            seen = self._seen.get(callsign)
        if not seen or now - seen[2] > self.WINDOW_S:
            return False
        km = ((lat - seen[0]) * 111.0) ** 2 + ((lon - seen[1]) * 111.0 * max(0.1, math.cos(math.radians(lat)))) ** 2
        return km <= self.RADIUS_KM ** 2
