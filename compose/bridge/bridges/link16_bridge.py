#!/usr/bin/env python3
"""link16_bridge.py — Link 16 / JREAP-C → Zenoh bridge.

Receives Link 16 J-series messages encapsulated in JREAP-C UDP packets
(MIL-STD-3011 / STANAG 5602) and publishes decoded tactical tracks to
the EFDI Zenoh fabric so cot_layer.py can forward them to ATAK.

Protocol stack:
    UDP (port 3010)
      └── JREAP-C header (4 bytes)
            └── J-series words (75 bits each, LSB-padded to 10 bytes)
                  └── Message label → J3.2 / J2.2 / J3.5 / J3.7 ...

Message types decoded:
    J2.2  PPLI Air        — friendly air unit (own-force position report)
    J2.5  PPLI Surface    — friendly surface unit
    J3.2  Air Track       — surveillance air track (cooperative / PSR)
    J3.5  Surface Track   — surface track
    J3.7  Land Track      — land track

Position encoding (Binary Angular Measurement — BAM):
    All positions in Link 16 use signed integer BAM fractions of the full circle.
    The conversion is:  degrees = raw × (360 / 2^bits)
    (Some fields use 25-bit lat / 26-bit lon within the 75-bit word.)

NOTE: Bit field positions in this file are based on MIL-STD-6016F / STANAG 5516
(Edition 5) unclassified summary tables.  If your terminal uses an earlier edition
(MIL-STD-6016C/D/E) verify the field offsets before operational use — minor
revisions moved some sub-fields.

Zenoh topics published:
    air/link16/jreap/friendly/aircraft/tracks/v1   — J2.2 / J3.2 friend
    air/link16/jreap/hostile/aircraft/tracks/v1    — J3.2 hostile
    air/link16/jreap/unknown/tracks/v1             — J3.2 unknown
    sea/link16/jreap/friendly/vessel/tracks/v1     — J2.5 / J3.5 friend
    sea/link16/jreap/hostile/vessel/tracks/v1      — J3.5 hostile
    land/link16/jreap/friendly/unit/tracks/v1      — J3.7 friend
    land/link16/jreap/hostile/unit/tracks/v1       — J3.7 hostile

Configuration (compose/.env):
    LINK16_PORT=3010           # JREAP-C UDP listen port (default: 3010)
    LINK16_TCP=0               # set 1 for TCP server mode instead of UDP

Run:
    venv/bin/python3 link16_bridge.py
    venv/bin/python3 link16_bridge.py --port 3010 --verbose
"""

import argparse
import json
import os
import socket
import struct
import threading
import time

import zenoh

ROUTER    = "tls/zenoh.efdi.netbird.efdi-backbone.net:7447"
ORG       = os.environ.get("PARTNER_NAMESPACE", "")
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("GOAT_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", ROUTER)

JREAP_PORT   = int(os.environ.get("LINK16_PORT", "3010"))
WORD_BITS    = 75
WORD_BYTES   = 10     # 75 bits padded to 80 bits (5 unused LSBs per word)

# J-series message labels (J-number × 8 + sub-label encoding)
# Label field: bits 2-7 = J-number (0-63), bits 8-11 = sub-label (0-15)
_LABEL = {
    (2, 2): "J2.2",   # PPLI Air
    (2, 5): "J2.5",   # PPLI Surface
    (3, 2): "J3.2",   # Air Track
    (3, 5): "J3.5",   # Surface Track
    (3, 7): "J3.7",   # Land Track
}

# Force/identity codes → affiliation slug
_ID_AFF = {
    0b000: "friendly",   # Friend
    0b001: "neutral",    # Neutral
    0b010: "hostile",    # Hostile/Suspect
    0b011: "unknown",    # Unknown
    0b100: "hostile",    # Suspect (treat as hostile)
    0b101: "friendly",   # Assumed Friend
}

# Topic templates per (domain, affiliation)
_TOPIC_MAP = {
    ("air",  "friendly"): "{}/air/link16/jreap/friendly/aircraft/tracks/v1".format(ORG),
    ("air",  "hostile"):  "{}/air/link16/jreap/hostile/aircraft/tracks/v1".format(ORG),
    ("air",  "neutral"):  "{}/air/link16/jreap/neutral/aircraft/tracks/v1".format(ORG),
    ("air",  "unknown"):  "{}/air/link16/jreap/unknown/tracks/v1".format(ORG),
    ("sea",  "friendly"): "{}/sea/link16/jreap/friendly/vessel/tracks/v1".format(ORG),
    ("sea",  "hostile"):  "{}/sea/link16/jreap/hostile/vessel/tracks/v1".format(ORG),
    ("sea",  "neutral"):  "{}/sea/link16/jreap/neutral/vessel/tracks/v1".format(ORG),
    ("sea",  "unknown"):  "{}/sea/link16/jreap/unknown/vessel/tracks/v1".format(ORG),
    ("land", "friendly"): "{}/land/link16/jreap/friendly/unit/tracks/v1".format(ORG),
    ("land", "hostile"):  "{}/land/link16/jreap/hostile/unit/tracks/v1".format(ORG),
    ("land", "neutral"):  "{}/land/link16/jreap/neutral/unit/tracks/v1".format(ORG),
    ("land", "unknown"):  "{}/land/link16/jreap/unknown/unit/tracks/v1".format(ORG),
}


# ---------------------------------------------------------------------------
# Zenoh
# ---------------------------------------------------------------------------

def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


def _netbird_ip() -> str:
    for iface in ("wt0", "netbird0"):
        try:
            import subprocess
            out = subprocess.check_output(["ip", "-4", "addr", "show", iface],
                                          stderr=subprocess.DEVNULL, text=True)
            for tok in out.split():
                if "/" in tok and tok[0].isdigit():
                    return tok.split("/")[0]
        except Exception:
            pass
    return socket.gethostname()


# ---------------------------------------------------------------------------
# Bit-level word reader
# ---------------------------------------------------------------------------

class BitReader:
    """Extract arbitrary-width fields from a 75-bit Link 16 word.

    Words are transmitted MSB-first, padded to 80 bits (10 bytes) with 5
    unused LSBs.  Field offsets are 0-based from the MSB of the 75-bit word.
    """

    def __init__(self, data: bytes):
        # Use only the first 75 bits (top bits of the 10-byte block)
        self._val = int.from_bytes(data[:10], "big") >> 5   # shift off 5 padding bits
        self._bits = 75

    def u(self, offset: int, width: int) -> int:
        """Unsigned field: offset bits from MSB, width bits wide."""
        shift = self._bits - offset - width
        if shift < 0:
            return 0
        return (self._val >> shift) & ((1 << width) - 1)

    def s(self, offset: int, width: int) -> int:
        """Signed (two's complement) field."""
        v = self.u(offset, width)
        if v >= (1 << (width - 1)):
            v -= (1 << width)
        return v

    def bam(self, offset: int, width: int) -> float:
        """Signed BAM field → decimal degrees.  Full circle = 2^width LSBs."""
        return self.s(offset, width) * (360.0 / (1 << width))


# ---------------------------------------------------------------------------
# JREAP-C framing
# ---------------------------------------------------------------------------

def _parse_jreap_header(data: bytes) -> tuple[int, int, bytes]:
    """Parse 4-byte JREAP-C PDU header.  Returns (pdu_type, seq_num, payload)."""
    if len(data) < 4:
        raise ValueError("Packet too short for JREAP-C header")
    version  = data[0]   # should be 0x01
    pdu_type = data[1]   # 0x00=init 0x01=J-series 0x02=keepalive 0x03=term
    seq_num  = struct.unpack(">H", data[2:4])[0]
    return pdu_type, seq_num, data[4:]


def extract_words(payload: bytes) -> list[bytes]:
    """Split JREAP-C payload into individual 75-bit words (each padded to 10 bytes)."""
    words = []
    off = 0
    while off + WORD_BYTES <= len(payload):
        words.append(payload[off:off + WORD_BYTES])
        off += WORD_BYTES
    return words


def word_label(w: BitReader) -> tuple[int, int]:
    """Extract (J-number, sub-label) from the label field of a word header.

    Per MIL-STD-6016F, the label occupies bits 2-7 (J-number, 6 bits) and
    bits 8-11 (sub-label, 4 bits) of the initial word.
    """
    jnum  = w.u(2, 6)    # bits 2-7
    sub   = w.u(8, 4)    # bits 8-11
    return jnum, sub


# ---------------------------------------------------------------------------
# J-message decoders
# ---------------------------------------------------------------------------
#
# Bit positions below follow MIL-STD-6016F Table A-E-III (Air Track J3.2)
# and Table A-E-I (PPLI J2.2).  Unclassified field positions only.
#
# Initial word (word 0): bits 0-74
# Continuation word 1:   bits 75-149
# Continuation word 2:   bits 150-224
#
# For 3-word messages, concatenate the three 75-bit words into a 225-bit
# stream, then read fields by absolute bit offset.

class MultiWordReader:
    """Read bit fields across multiple 75-bit words (concatenated)."""

    def __init__(self, words: list[bytes]):
        bits = b""
        for w in words:
            # Each 10-byte block: top 75 bits are data, bottom 5 are padding
            val = int.from_bytes(w[:10], "big") >> 5
            bits += val.to_bytes(10, "big")
        # total available bits = len(words) × 75 (stored in top bits of each 10B)
        self._total_bytes = len(words) * 10
        self._val  = int.from_bytes(bits, "big")
        self._bits = len(words) * 80  # stored width (includes per-word padding bytes)
        self._word_count = len(words)

    def _effective_offset(self, bit: int) -> int:
        """Map logical bit offset (across 75-bit words) to storage bit offset."""
        word_idx  = bit // 75
        bit_in_w  = bit % 75
        return word_idx * 80 + bit_in_w   # 80 stored bits per word (5 padding at end)

    def u(self, bit: int, width: int) -> int:
        """Unsigned field at logical bit offset across concatenated 75-bit words."""
        eff = self._effective_offset(bit)
        shift = self._bits - eff - width
        if shift < 0 or width <= 0:
            return 0
        return (self._val >> shift) & ((1 << width) - 1)

    def s(self, bit: int, width: int) -> int:
        v = self.u(bit, width)
        if v >= (1 << (width - 1)):
            v -= (1 << width)
        return v

    def bam(self, bit: int, width: int) -> float:
        return self.s(bit, width) * (360.0 / (1 << width))


# ---------------------------------------------------------------------------
# J3.2  Air Track  (3 words, 225 bits)
# Ref: MIL-STD-6016F, Table A-E-III / STANAG 5516 Ed.5, Annex E
# ---------------------------------------------------------------------------
#
# Word 0 (bits 0-74):
#   0- 1  Word type = 00 (initial)
#   2- 7  Label = 000011 (J-number 3)
#   8-11  Sub-label = 0010 (J3.2)
#  12-13  Link name (network)
#  14-16  Track quality (1-7)
#  17-19  Identity/force: 000=friend 001=neutral 010=hostile 011=unknown
#  20-21  Exercise (00=live 01=exercise 10=sim)
#  22-23  Track number MSBs (2 bits)
#  24-34  Track number (10 bits)  [total 12-bit track number]
#  35-59  Latitude (25-bit signed BAM → ×180/2^24 deg)
#  60-74  Longitude MSBs (15 bits)
#
# Word 1 (bits 75-149):
#  75-85  Longitude LSBs (11 bits, combined with above = 26-bit signed BAM)
#  86-96  Altitude (11-bit, 100 ft per LSB, offset -1000 ft → alt = raw*100 - 100000)
#  97-108 Speed (12-bit unsigned, 1 kt per LSB)
# 109-119 Heading (11-bit unsigned BAM → ×360/2048)
# 120-149 (additional fields, environment type, etc.)

def decode_j32(words: list[bytes]) -> dict | None:
    """Decode J3.2 Air Track from a list of 3 word buffers."""
    if len(words) < 3:
        return None
    r = MultiWordReader(words)

    identity_raw = r.u(17, 3)
    aff          = _ID_AFF.get(identity_raw, "unknown")

    track_num    = (r.u(22, 2) << 10) | r.u(24, 10)   # 12-bit track number

    lat_raw      = r.s(35, 25)
    lat_deg      = lat_raw * (180.0 / (1 << 24))

    lon_msb      = r.u(60, 15)
    lon_lsb      = r.u(75, 11)
    lon_raw_u    = (lon_msb << 11) | lon_lsb           # 26-bit unsigned
    lon_raw_s    = lon_raw_u - (1 << 26) if lon_raw_u >= (1 << 25) else lon_raw_u
    lon_deg      = lon_raw_s * (360.0 / (1 << 25))

    alt_raw      = r.u(86, 11)
    alt_ft       = alt_raw * 100 - 100_000             # offset encoding

    spd_raw      = r.u(97, 12)
    spd_ms       = spd_raw * 0.514444                  # kt → m/s

    hdg_raw      = r.u(109, 11)
    hdg_deg      = hdg_raw * (360.0 / 2048.0)

    if abs(lat_deg) > 90 or abs(lon_deg) > 180:
        return None

    return {
        "_ts":         time.time(),
        "_src":        "Link 16 J3.2",
        "track_num":   track_num,
        "affiliation": aff,
        "lat_deg":     round(lat_deg, 6),
        "lon_deg":     round(lon_deg, 6),
        "alt_baro_ft": round(alt_ft),
        "speed_ms":    round(spd_ms, 1),
        "heading_deg": round(hdg_deg, 1),
    }


# ---------------------------------------------------------------------------
# J2.2  PPLI Air  (3 words, 225 bits)
# Ref: MIL-STD-6016F, Table A-E-I
# PPLI = Precise Participant Location and Identification (self-report)
# Always friendly — emitting own-force units identify themselves.
# ---------------------------------------------------------------------------
#
# Word 0:
#   0- 1  Word type = 00
#   2- 7  Label = 000010 (J-number 2)
#   8-11  Sub-label = 0010 (J2.2)
#  12-23  Source Track Number (STN, 12-bit own-force ID)
#  24-48  Latitude (25-bit signed BAM)
#  49-74  Longitude MSBs (26 bits, MSB portion)
#
# Word 1:
#  75-85  Longitude LSBs (remaining bits)
#  86-95  Altitude (10-bit, 100 ft/LSB, offset)
#  96-107 Speed (12-bit unsigned, 1 kt/LSB)
# 108-118 Heading (11-bit unsigned BAM)
# 119-131 STN callsign extension / activity fields

def decode_j22(words: list[bytes]) -> dict | None:
    """Decode J2.2 PPLI Air from a list of 3 word buffers."""
    if len(words) < 3:
        return None
    r = MultiWordReader(words)

    stn          = r.u(12, 12)                         # source track number (unit ID)

    lat_raw      = r.s(24, 25)
    lat_deg      = lat_raw * (180.0 / (1 << 24))

    lon_msb      = r.u(49, 26)
    lon_lsb      = r.u(75, 11) if len(words) > 1 else 0
    lon_raw_u    = (lon_msb << 0)                      # MSBs already 26-bit
    lon_raw_s    = lon_raw_u - (1 << 25) if lon_raw_u >= (1 << 25) else lon_raw_u
    lon_deg      = lon_raw_s * (360.0 / (1 << 25))

    alt_raw      = r.u(86, 10)
    alt_ft       = alt_raw * 100 - 100_000

    spd_raw      = r.u(96, 12)
    spd_ms       = spd_raw * 0.514444

    hdg_raw      = r.u(108, 11)
    hdg_deg      = hdg_raw * (360.0 / 2048.0)

    if abs(lat_deg) > 90 or abs(lon_deg) > 180:
        return None

    return {
        "_ts":         time.time(),
        "_src":        "Link 16 J2.2",
        "track_num":   stn,
        "affiliation": "friendly",   # PPLI = always own-force friendly
        "lat_deg":     round(lat_deg, 6),
        "lon_deg":     round(lon_deg, 6),
        "alt_baro_ft": round(alt_ft),
        "speed_ms":    round(spd_ms, 1),
        "heading_deg": round(hdg_deg, 1),
    }


# ---------------------------------------------------------------------------
# J3.5  Surface Track  (same layout as J3.2 except altitude = MSL, no speed)
# J3.7  Land Track     (same layout, altitude = terrain clearance)
# Both are 3-word messages with identical position encoding as J3.2.
# ---------------------------------------------------------------------------

def decode_j35(words: list[bytes]) -> dict | None:
    """Decode J3.5 Surface Track (same layout as J3.2, sea domain)."""
    track = decode_j32(words)
    if track:
        track["_src"] = "Link 16 J3.5"
        track["domain"] = "sea"
    return track


def decode_j25(words: list[bytes]) -> dict | None:
    """Decode J2.5 PPLI Surface (same layout as J2.2, sea domain)."""
    track = decode_j22(words)
    if track:
        track["_src"] = "Link 16 J2.5"
        track["domain"] = "sea"
    return track


def decode_j37(words: list[bytes]) -> dict | None:
    """Decode J3.7 Land Track."""
    track = decode_j32(words)
    if track:
        track["_src"] = "Link 16 J3.7"
        track["domain"] = "land"
    return track


# ---------------------------------------------------------------------------
# Message dispatcher
# ---------------------------------------------------------------------------

# How many continuation words each message type needs (after the initial word)
_MSG_WORD_COUNT = {
    "J2.2": 3,
    "J2.5": 3,
    "J3.2": 3,
    "J3.5": 3,
    "J3.7": 3,
}

_MSG_DECODER = {
    "J2.2": decode_j22,
    "J2.5": decode_j25,
    "J3.2": decode_j32,
    "J3.5": decode_j35,
    "J3.7": decode_j37,
}

_MSG_DOMAIN = {
    "J2.2": "air",
    "J2.5": "sea",
    "J3.2": "air",
    "J3.5": "sea",
    "J3.7": "land",
}


def _topic_for(track: dict, msg_type: str) -> str:
    domain = track.get("domain") or _MSG_DOMAIN.get(msg_type, "land")
    aff    = track.get("affiliation", "unknown")
    return _TOPIC_MAP.get((domain, aff),
                           "{}/land/link16/jreap/unknown/unit/tracks/v1".format(ORG))


def process_packet(data: bytes, pub: "zenoh.Session", verbose: bool):
    """Parse one JREAP-C UDP packet and publish any decoded tracks."""
    try:
        pdu_type, seq, payload = _parse_jreap_header(data)
    except ValueError:
        return

    if pdu_type != 0x01:  # not J-series data
        return

    words = extract_words(payload)
    if not words:
        return

    i = 0
    while i < len(words):
        r = BitReader(words[i])
        jnum, sub = word_label(r)
        msg_type  = _LABEL.get((jnum, sub))

        if msg_type is None:
            i += 1
            continue

        needed = _MSG_WORD_COUNT.get(msg_type, 1)
        if i + needed > len(words):
            break   # not enough words remaining

        decoder = _MSG_DECODER.get(msg_type)
        if decoder:
            track = decoder(words[i:i + needed])
            if track:
                topic = _topic_for(track, msg_type)
                pub.put(topic, json.dumps(track).encode(),
                        encoding=zenoh.Encoding.APPLICATION_JSON)
                if verbose:
                    print("PUB link16 {} aff={} lat={} lon={} alt={}ft".format(
                        msg_type,
                        track.get("affiliation", "?"),
                        round(track.get("lat_deg", 0), 4),
                        round(track.get("lon_deg", 0), 4),
                        track.get("alt_baro_ft", "---"),
                    ), flush=True)

        i += needed


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def run_udp(port: int, session: "zenoh.Session", verbose: bool):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    ip = _netbird_ip()
    print("Link 16 JREAP-C UDP listening on 0.0.0.0:{}".format(port), flush=True)
    print("Tell JREAP gateway: send to {}:{}".format(ip, port), flush=True)
    while True:
        data, _ = sock.recvfrom(65535)
        process_packet(data, session, verbose)


def _tcp_client(conn, addr, session, verbose):
    print("Link 16 TCP connected: {}".format(addr), flush=True)
    buf = b""
    try:
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            # JREAP-C over TCP: each PDU is self-delimiting via word count
            # Read 4-byte header, then extract words until payload exhausted
            while len(buf) >= 4:
                # Peek at a word count field if available, otherwise process 4+10 chunks
                process_packet(buf, session, verbose)
                buf = buf[4 + ((len(buf) - 4) // WORD_BYTES) * WORD_BYTES:]
                break
    except OSError:
        pass
    finally:
        conn.close()
        print("Link 16 TCP disconnected: {}".format(addr), flush=True)


def run_tcp(port: int, session: "zenoh.Session", verbose: bool):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(5)
    ip = _netbird_ip()
    print("Link 16 JREAP-C TCP server on 0.0.0.0:{}".format(port), flush=True)
    print("Tell JREAP gateway: connect to {}:{}".format(ip, port), flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(target=_tcp_client, args=(conn, addr, session, verbose),
                         daemon=True).start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Link 16 JREAP-C → Zenoh bridge")
    ap.add_argument("--port", type=int, default=JREAP_PORT,
                    help="JREAP-C listen port (default: {})".format(JREAP_PORT))
    ap.add_argument("--tcp", action="store_true",
                    help="TCP server mode instead of UDP")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    session = zenoh.open(make_config())
    print("Link 16 bridge started", flush=True)
    print("  Topics:", flush=True)
    for (dom, aff), topic in _TOPIC_MAP.items():
        print("    {} {} → {}".format(dom, aff, topic.split(ORG + "/")[1]), flush=True)

    try:
        if args.tcp:
            run_tcp(args.port, session, args.verbose)
        else:
            run_udp(args.port, session, args.verbose)
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


if __name__ == "__main__":
    main()
