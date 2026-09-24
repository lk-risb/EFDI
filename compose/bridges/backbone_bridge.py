#!/usr/bin/env python3
"""backbone_bridge.py — EFDI Backbone trial fabric -> Zenoh raw ingress.

Thin raw relay, same role as any other "-raw" bridge (sapient-raw,
stanag4586-raw, ...): move bytes off the external source onto a local raw
topic, unmodified. Decoding happens downstream, in protocols/ — this file
has no schema knowledge of its own on purpose, so a new participant's shape
never requires touching this file, only adding (or already having) a
normalizer that watches the raw topic.

Holds its OWN direct mTLS session to the real backbone router, using the
same identity uploaded via /api/certs/backbone/bootstrap — read from the
dedicated zenoh-router-backbone container's own identity directory,
${POD_STATE_DIR}/zenoh-backbone/tls/{cert,key,ca-roots}.pem (a host path,
readable directly since this is a native host process, not a container).

This does NOT go through zenoh-router-backbone's router-to-router relay.
Confirmed live, with Zenoh's own debug logging: a subscription declared on
this pod's local router (tcp/127.0.0.1:7448) DOES get forwarded from the
primary router into zenoh-router-backbone (visible in its own debug log at
the exact right timestamp, registered internally) but zenoh-router-backbone
then never transmits anything for it onward to the real backbone link —
total silence on that link after its one-time connect-time declaration
dump. Every other integration in this codebase already works by holding two
independent zenoh sessions and explicitly relaying between them in
application code — never by relying on automatic multi-hop router-to-router
pub/sub propagation across more than one hop.

Excludes our own vendor prefix (this identity's cert CN, e.g.
"f2121acad40b27c52728fa461b63a5d7") from what it relays in. Without this:
layers/backbone_layer.py publishes our own tracks out under
<our_prefix>/efdi/**; this bridge's own "**" subscription on the SAME
remote router matches that too (the router doesn't know or care who
published it), so our own outbound data would echo straight back in,
get renamed by generic_json.py to look like a foreign track (mislabeled "_src":
"backbone:json:<our_prefix>"), and show up as a duplicate phantom marker
on the map. backbone_layer.py's own "don't re-export /backbone/-tagged
topics" guard stops this from amplifying further, but doesn't stop the one
initial duplicate — the fix belongs here, at the point data re-enters the
pod, not downstream.

Every sample lands one of two places:
  - A valid ASTERIX frame (category+length header checks out, same
    validation bridges/asterix_bridge.py already does) -> republished to
    TOPIC_ROOT/raw/asterix/cat<N> — the exact topic
    protocols/vendors/asterix/cat.py's existing --zenoh-raw mode for that
    category already reads from (confirmed live: category 34 and 48 frames
    are actually present on this fabric right now, both already-default
    categories). No ASTERIX parsing lives here; cat.py's own decoders do it.
  - Everything else -> republished as-is to TOPIC_ROOT/raw/backbone/<key>,
    for protocols/random/generic_json.py and protocols/random/geojson.py (or any
    future normalizer) to try against. Undecodable payloads (opaque
    protobuf with no shared .proto, encrypted envelopes) just sit there
    unclaimed — nothing crashes, nothing is lost, there's simply no decoder
    for them yet.
"""

import argparse
import json
import os
import struct
import time

import zenoh
from cryptography import x509
from cryptography.x509.oid import NameOID
from protocols.gateway import TOPIC_ROOT, open_session, payload_bytes, subscribe

_POD_STATE_DIR = os.environ.get("POD_STATE_DIR", "/root/efdi-router/compose/state")
_BACKBONE_TLS_DIR = os.path.join(_POD_STATE_DIR, "zenoh-backbone", "tls")

_RAW_BACKBONE_ROOT = TOPIC_ROOT + "/raw/backbone"
_RAW_ASTERIX_ROOT = TOPIC_ROOT + "/raw/asterix/cat"


def _asterix_category(raw: bytes) -> int | None:
    """Same header check as asterix_bridge.py's validate_frame: category byte
    + declared 16-bit length must match the frame actually received. Cheap
    and specific enough that opaque protobuf/encrypted bytes essentially
    never pass it by accident (they'd need one of 256 category bytes to line
    up with a length field that happens to equal the true frame length)."""
    if len(raw) < 3:
        return None
    try:
        category, declared_length = struct.unpack(">BH", raw[:3])
    except struct.error:
        return None
    if declared_length != len(raw):
        return None
    return category


def _backbone_endpoints() -> list[str]:
    """Same endpoint the WebUI's identity upload staged for display purposes
    (backbone_bootstrap.py's _control_env_update) — read fresh from the
    environment on every start (start.sh sources compose/.env before
    launching any native process), not baked in like the zenoh-admin
    container's own copy of this same variable."""
    raw = os.environ.get("EFDI_BACKBONE_FABRIC_ENDPOINTS", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return [raw]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if item]
    return [str(parsed)]


def _vendor_prefix(cert_path: str) -> str | None:
    try:
        with open(cert_path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
        return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    except Exception:
        return None


def _open_backbone_session():
    """Direct mTLS session to the real backbone fabric, bypassing
    zenoh-router-backbone's router-to-router relay entirely (see module
    docstring for why). Retries forever — the identity may not be uploaded
    yet, or the fabric endpoint may be temporarily unreachable. Also returns
    this identity's own vendor prefix (its cert's CN — see
    layers/backbone_layer.py, which publishes our own tracks out under
    exactly this prefix) so the caller can refuse to re-import them."""
    cert = os.path.join(_BACKBONE_TLS_DIR, "cert.pem")
    key = os.path.join(_BACKBONE_TLS_DIR, "key.pem")
    ca = os.path.join(_BACKBONE_TLS_DIR, "ca-roots.pem")
    while True:
        endpoints = _backbone_endpoints()
        missing = [p for p in (cert, key, ca) if not os.path.isfile(p)]
        vendor_prefix = None if missing else _vendor_prefix(cert)
        if not endpoints or missing or not vendor_prefix:
            print(
                "backbone bridge: no backbone identity/endpoint configured yet "
                "(missing {}) — upload one via the Certificates page. Retry in 15s"
                .format(missing or ("EFDI_BACKBONE_FABRIC_ENDPOINTS" if not endpoints else "a readable vendor CN")),
                flush=True,
            )
            time.sleep(15)
            continue
        conf = zenoh.Config()
        conf.insert_json5("mode", '"client"')
        conf.insert_json5("connect/endpoints", json.dumps(endpoints))
        conf.insert_json5("transport/link/tls", json.dumps({
            "root_ca_certificate": ca,
            "connect_certificate": cert,
            "connect_private_key": key,
            "enable_mtls": True,
            "verify_name_on_connect": True,
        }))
        try:
            return zenoh.open(conf), vendor_prefix
        except Exception as exc:
            print("backbone bridge: backbone connect failed: {} — retry in 15s".format(exc), flush=True)
            time.sleep(15)


def _handle(local_session, vendor_prefix: str, sample, verbose: bool):
    key = str(sample.key_expr)
    if key == vendor_prefix or key.startswith(vendor_prefix + "/"):
        return  # our own layers/backbone_layer.py output, echoed back by the fabric — not foreign data
    raw = payload_bytes(sample)

    category = _asterix_category(raw)
    if category is not None:
        topic = "{}{}".format(_RAW_ASTERIX_ROOT, category)
        local_session.put(topic, raw)
        if verbose:
            print("backbone ASTERIX cat{} {} -> {} ({}B)".format(category, key, topic, len(raw)), flush=True)
        return

    topic = "{}/{}".format(_RAW_BACKBONE_ROOT, key)
    local_session.put(topic, raw)
    if verbose:
        print("backbone raw {} -> {} ({}B)".format(key, topic, len(raw)), flush=True)


def main():
    ap = argparse.ArgumentParser(description="EFDI Backbone trial fabric -> Zenoh raw ingress")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    while True:
        try:
            local_session = open_session()
            break
        except Exception as exc:
            print("backbone bridge: local Zenoh connect failed: {} — retry in 10s".format(exc), flush=True)
            time.sleep(10)

    backbone_session, vendor_prefix = _open_backbone_session()
    print(
        "backbone bridge starting — relaying backbone fabric (excluding our own {}/**) to {} and {}<N>"
        .format(vendor_prefix, _RAW_BACKBONE_ROOT, _RAW_ASTERIX_ROOT),
        flush=True,
    )
    sub = subscribe(backbone_session, "**", lambda sample: _handle(local_session, vendor_prefix, sample, args.verbose))

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        sub.undeclare()
        backbone_session.close()
        local_session.close()


if __name__ == "__main__":
    main()
