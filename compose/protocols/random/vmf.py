#!/usr/bin/env python3
"""VMF (MIL-STD-47001C) protocol → Zenoh.

Receives Variable Message Format (VMF) datagrams over UDP (or TCP) and
publishes decoded position reports to the EFDI Zenoh fabric.

VMF is a bit-packed binary protocol used in US/NATO tactical radio networks
(SINCGARS, HAVE QUICK, Link-11, JREAP-B).  Each message starts with a
multi-field bit-packed header followed by the message body (K-series or
operator-defined content).

Decoded messages:
  K05.2  Position Report (primary ground/air/sea position update)
  K04.4  Unit Report (friendly force position)

Position encoding follows the same BAM convention as Link 16:
  Latitude:  25-bit signed BAM → degrees = raw × (180 / 2**24)
  Longitude: 26-bit signed BAM → degrees = raw × (360 / 2**25)

IMPORTANT: Bit positions are derived from MIL-STD-47001C.  Verify against
the specific edition and profile used by your equipment before deployment.

Config (compose/.env):
  VMF_PORT=    UDP port to listen on (no standard port — check your gateway)
  VMF_TCP=     Set to 1 for TCP mode

Run:
  venv/bin/python3 protocols/random/vmf.py --port 2000
"""

import argparse
import json
import os
import socket
import struct
import time
from protocols.random.vmf_pb2 import VmfTrack

import zenoh
from namespace_prefix import topic_root
from protocols.protobuf_codec import publish_dual
from zenoh_auth import apply_zenoh_auth

ORG       = os.environ.get("PARTNER_NAMESPACE", "")
TOPIC_ROOT = topic_root()
HERE      = os.path.dirname(os.path.abspath(__file__))
_CERT_DIR = os.environ.get("EFDI_CERT_DIR", HERE)
_ENDPOINT = os.environ.get("ZENOH_LOCAL_ENDPOINT", "tcp/127.0.0.1:7448")

# VMF message numbers (MIL-STD-47001C, Table A-I)
VMF_K05_2 = (5, 2)   # Position Report
VMF_K04_4 = (4, 4)   # Unit Report

# Affiliation codes from VMF header Force/Identity field
_VMF_AFF = {
    0b000: "friendly",
    0b001: "neutral",
    0b010: "hostile",
    0b011: "unknown",
    0b111: "unknown",
}

# Domain/entity by K-message operational environment field
_VMF_DOMAIN = {
    0: ("land",  "unit"),
    1: ("air",   "aircraft"),
    2: ("sea",   "vessel"),
    3: ("land",  "unit"),   # subsurface → land fallback
}


def make_config() -> "zenoh.Config":
    conf = zenoh.Config()
    conf.insert_json5("mode", '"client"')
    conf.insert_json5("connect/endpoints", json.dumps([_ENDPOINT]))
    apply_zenoh_auth(conf)
    if _ENDPOINT.startswith("tls"):
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": os.path.join(_CERT_DIR, "efdi-ca-root.pem"),
            "connect_certificate": os.path.join(_CERT_DIR, ORG + "-cert.pem"),
            "connect_private_key": os.path.join(_CERT_DIR, ORG + "-key.pem"),
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
    return conf


# ---------------------------------------------------------------------------
# Bit reader
# ---------------------------------------------------------------------------

class _BitReader:
    def __init__(self, data: bytes):
        self._data = data
        self._pos  = 0   # current bit position

    def read(self, n: int) -> int:
        val = 0
        for _ in range(n):
            byte_idx = self._pos >> 3
            bit_idx  = 7 - (self._pos & 7)
            if byte_idx >= len(self._data):
                raise IndexError("VMF bit stream exhausted")
            val = (val << 1) | ((self._data[byte_idx] >> bit_idx) & 1)
            self._pos += 1
        return val

    def read_signed(self, n: int) -> int:
        val = self.read(n)
        if val & (1 << (n - 1)):
            val -= (1 << n)
        return val

    def skip(self, n: int):
        self._pos += n

    @property
    def bits_remaining(self) -> int:
        return len(self._data) * 8 - self._pos


# ---------------------------------------------------------------------------
# VMF header parser
# ---------------------------------------------------------------------------

def _parse_vmf_header(br: _BitReader) -> dict:
    """Parse VMF message header fields (MIL-STD-47001C, Section 5).

    Returns a dict with header fields needed for routing and decoding.
    Verify bit counts against your specific VMF edition/profile.
    """
    hdr = {}
    try:
        hdr["compress"]      = br.read(1)    # Field 1: compress/expand
        hdr["msg_number"]    = br.read(7)    # Field 2: message number (K-number)
        hdr["msg_subtype"]   = br.read(4)    # Field 3: message subtype
        hdr["fad"]           = br.read(4)    # Field 4: functional address designator
        hdr["msg_size"]      = br.read(16)   # Field 5: message text size (bits)
        hdr["op_indicator"]  = br.read(2)    # Field 6: operation indicator
        hdr["retransmit"]    = br.read(1)    # Field 7: retransmit indicator
        hdr["msg_precedence"]= br.read(3)    # Field 8: message precedence
        hdr["classification"]= br.read(3)    # Field 9: security classification
        # Field 10: address group (originator/recipient) — variable
        # Skip address group (simplified — 28 bits for single address)
        br.skip(28)
        hdr["originator_urn"]= 0
        # Field 13: acknowledge request
        hdr["ack_request"]   = br.read(1)
        # Field 14: date-time group (56 bits)
        hdr["dtg"]           = br.read(56)
        # Field 15: machine receipt
        hdr["machine_rcpt"]  = br.read(1)
        # Force/Identity (4 bits) — affiliation of originator
        hdr["force_id"]      = br.read(3)
        hdr["aff_code"]      = br.read(1)
    except (IndexError, Exception):
        pass   # partial header is fine — use what we got
    return hdr


# ---------------------------------------------------------------------------
# K05.2 Position Report decoder
# ---------------------------------------------------------------------------

def _bam25_lat(raw: int) -> float:
    return raw * (180.0 / (1 << 24))


def _bam26_lon(raw: int) -> float:
    return raw * (360.0 / (1 << 26))


def _decode_k05_2(br: _BitReader, hdr: dict) -> dict | None:
    """Decode K05.2 Position Report body.

    Bit layout per MIL-STD-47001C Annex A, Table A-II (K05.2).
    VERIFY against your edition before operational use.
    """
    try:
        # Operational environment (2 bits): 0=ground, 1=air, 2=sea
        op_env    = br.read(2)
        # Position validity (1 bit)
        pos_valid = br.read(1)
        # Latitude: 25-bit signed BAM
        lat_raw   = br.read_signed(25)
        # Longitude: 26-bit signed BAM
        lon_raw   = br.read_signed(26)
        # Altitude presence flag
        alt_pres  = br.read(1)
        alt_m     = None
        if alt_pres:
            # Altitude: 16-bit signed, 4 m/LSB
            alt_m = br.read_signed(16) * 4.0

        if not pos_valid:
            return None

        lat = _bam25_lat(lat_raw)
        lon = _bam26_lon(lon_raw)
        if lat < -90 or lat > 90 or lon < -180 or lon > 180:
            return None

        # Speed: 12-bit unsigned, 1 kt/LSB → m/s
        spd_kt  = br.read(12)
        # Heading: 12-bit unsigned BAM
        hdg_raw = br.read(12)

        aff_raw = hdr.get("force_id", 3)
        aff     = _VMF_AFF.get(aff_raw, "unknown")
        domain, entity = _VMF_DOMAIN.get(op_env, ("land", "unit"))

        track = {
            "_ts":       time.time(),
            "_src":      "VMF K05.2",
            "uid":       "vmf-{}-{:.4f}-{:.4f}".format(
                hdr.get("originator_urn", 0), lat, lon),
            "lat_deg":   round(lat, 6),
            "lon_deg":   round(lon, 6),
            "_aff":      aff,
            "_domain":   domain,
            "_entity":   entity,
        }
        if alt_m is not None:
            track["alt_m"] = round(alt_m, 1)
        if spd_kt > 0:
            track["speed_ms"] = round(spd_kt * 0.514444, 2)
        if hdg_raw > 0:
            track["heading_deg"] = round(hdg_raw * (360.0 / 4096), 1)
        return track

    except Exception:
        return None


def _decode_k04_4(br: _BitReader, hdr: dict) -> dict | None:
    """Decode K04.4 Unit Report — uses same position encoding as K05.2."""
    return _decode_k05_2(br, hdr)   # structure is compatible for position fields


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def _decode_vmf(data: bytes) -> dict | None:
    br  = _BitReader(data)
    hdr = _parse_vmf_header(br)
    msg_key = (hdr.get("msg_number", -1), hdr.get("msg_subtype", -1))

    if msg_key == VMF_K05_2:
        track = _decode_k05_2(br, hdr)
    elif msg_key == VMF_K04_4:
        track = _decode_k04_4(br, hdr)
    else:
        return None   # unsupported message type

    return track


def _topic(track: dict) -> str:
    domain = track.pop("_domain", "land")
    entity = track.pop("_entity", "unit")
    aff    = track.pop("_aff",    "unknown")
    return "{}/{}/vmf/{}/{}".format(TOPIC_ROOT, domain, aff, entity)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    if args.zenoh_raw:
        return run_zenoh_raw(args)
    session = zenoh.open(make_config())
    print("VMF bridge started  mode={} port={}".format(
        "TCP" if args.tcp else "UDP", args.port), flush=True)
    print("NOTE: Bit positions per MIL-STD-47001C — verify against your equipment", flush=True)

    try:
        if args.tcp:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", args.port))
            srv.listen(5)
            while True:
                conn, addr = srv.accept()
                print("VMF TCP connected from {}:{}".format(*addr), flush=True)
                buf = b""
                conn.settimeout(30)
                try:
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        # VMF datagrams framed with 2-byte big-endian length prefix
                        while len(buf) >= 2:
                            frame_len = struct.unpack(">H", buf[:2])[0]
                            if len(buf) < 2 + frame_len:
                                break
                            frame = buf[2:2 + frame_len]
                            buf   = buf[2 + frame_len:]
                            track = _decode_vmf(frame)
                            if track:
                                topic = _topic(track)
                                publish_dual(session, topic, track, VmfTrack, zenoh)
                                if args.verbose:
                                    print("VMF {} lat={} lon={}".format(
                                        track.get("uid", "?")[:20],
                                        round(track.get("lat_deg", 0), 4),
                                        round(track.get("lon_deg", 0), 4)), flush=True)
                except (OSError, socket.timeout):
                    pass
                finally:
                    conn.close()
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("0.0.0.0", args.port))
            sock.settimeout(1)
            while True:
                try:
                    data, _ = sock.recvfrom(65535)
                    track = _decode_vmf(data)
                    if track:
                        topic = _topic(track)
                        publish_dual(session, topic, track, VmfTrack, zenoh)
                        if args.verbose:
                            print("VMF {} lat={} lon={}".format(
                                track.get("uid", "?")[:20],
                                round(track.get("lat_deg", 0), 4),
                                round(track.get("lon_deg", 0), 4)), flush=True)
                except socket.timeout:
                    continue
    except KeyboardInterrupt:
        pass
    finally:
        session.close()


def run_zenoh_raw(args):
    session = zenoh.open(make_config())
    topic = args.raw_topic or TOPIC_ROOT + "/raw/vmf/**"

    def on_sample(sample):
        try:
            data = sample.payload.to_bytes() if hasattr(sample.payload, "to_bytes") else bytes(sample.payload)
            track = _decode_vmf(data)
            if track:
                publish_dual(session, _topic(track), track, VmfTrack, zenoh)
        except Exception as exc:
            print("VMF raw decode error:", exc, flush=True)

    subscriber = session.declare_subscriber(topic, on_sample)
    print("VMF Zenoh raw translator subscribed to {}".format(topic), flush=True)
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        pass
    finally:
        subscriber.undeclare()
        session.close()


def main():
    ap = argparse.ArgumentParser(description="VMF (MIL-STD-47001C) → Zenoh bridge")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("VMF_PORT", "") or "2000"))
    ap.add_argument("--tcp", action="store_true",
                    default=os.environ.get("VMF_TCP", "") == "1")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--zenoh-raw", action="store_true",
                    help="decode bytes from .../raw/vmf/** instead of opening a socket")
    ap.add_argument("--raw-topic", default=os.environ.get("VMF_RAW_TOPIC", ""))
    args = ap.parse_args()
    run(args)


if __name__ == "__main__":
    main()
