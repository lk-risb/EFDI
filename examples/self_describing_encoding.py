#!/usr/bin/env python3
"""Reference: self-describing payloads via the Zenoh `Encoding` field.

Illustrative, not production. A runnable shape of the goat "set the encoding"
best practice you can adapt.

The problem it solves: every Zenoh sample carries an `Encoding`, but if you
don't set it the wire is mute — a consumer has to know out-of-band what format
your bytes are in (JSON? CBOR? which protobuf message?). Setting the encoding on
publish lets any consumer pick a decoder straight from the sample, and lets the
fabric's inspection and schema-viewer tooling render your payload without a side
lookup.

It's additive and display-only: it does not change routing, access, or your
topic registration — it just stops the wire from being silent about format.

Best practice: drive the encoding off the ONE format your producer already
knows it emits — don't hand-classify per message.
  - JSON   -> Encoding.APPLICATION_JSON
  - CBOR   -> Encoding.APPLICATION_CBOR
  - text   -> Encoding.TEXT_PLAIN
  - protobuf -> Encoding.APPLICATION_PROTOBUF.with_schema("<MessageName>")
    (the schema name is what the viewer uses to pick the .proto descriptor)

This script publishes one sample in each of a few encodings, then subscribes and
shows how a consumer dispatches a decoder off `sample.encoding`. Run it against a
key expression you're onboarded for.

Connection config comes from the goat profile the pod/bundle gives you:
  EFDI_ROUTER     e.g. tls/<router-host>:7447
  EFDI_MTLS_CERT  path to mtls.cert.pem
  EFDI_MTLS_KEY   path to mtls.key.pem
  EFDI_CA_ROOTS   path to ca-roots.pem
  PARTNER_NAMESPACE     your onboarded prefix, e.g. acme

Run:  python3 self_describing_encoding.py
Deps: pip install eclipse-zenoh==1.9.0
"""

import json
import os
import time

import zenoh


def open_session() -> zenoh.Session:
    """Open an mTLS client session against the goat router, from env config."""
    conf = zenoh.Config()
    conf.insert_json5("mode", json.dumps("client"))
    conf.insert_json5("connect/endpoints", json.dumps([os.environ["EFDI_ROUTER"]]))
    conf.insert_json5("transport/link/tls/root_ca_certificate",
                      json.dumps(os.environ["EFDI_CA_ROOTS"]))
    conf.insert_json5("transport/link/tls/connect_certificate",
                      json.dumps(os.environ["EFDI_MTLS_CERT"]))
    conf.insert_json5("transport/link/tls/connect_private_key",
                      json.dumps(os.environ["EFDI_MTLS_KEY"]))
    return zenoh.open(conf)


def encoding_for(fmt: str) -> zenoh.Encoding:
    """Map your producer's one declared format string to a wire Encoding.

    This is the whole pattern: a single source of truth for 'what do I emit',
    turned into the self-describing tag. `protobuf:<Msg>` carries the message
    name through `with_schema` so the consumer/viewer knows which descriptor.
    """
    if fmt == "json":
        return zenoh.Encoding.APPLICATION_JSON
    if fmt == "cbor":
        return zenoh.Encoding.APPLICATION_CBOR
    if fmt == "text":
        return zenoh.Encoding.TEXT_PLAIN
    if fmt.startswith("protobuf:"):
        msg = fmt.split(":", 1)[1]
        return zenoh.Encoding.APPLICATION_PROTOBUF.with_schema(msg)
    # Unknown -> leave it to the platform default; don't guess.
    return zenoh.Encoding.APPLICATION_OCTET_STREAM


def decode(sample: zenoh.Sample) -> str:
    """Consumer side: pick a decoder off the wire instead of an out-of-band lookup."""
    enc = str(sample.encoding)
    raw = sample.payload.to_bytes()
    # Encoding stringifies by prefix: "application/json", "text/plain",
    # "application/protobuf;TrackUpdate" (the part after ';' is the schema name).
    if enc.startswith("application/json"):
        return f"json -> {json.loads(raw)!r}"
    if enc.startswith("text/plain"):
        return f"text -> {raw.decode()!r}"
    if enc.startswith("application/protobuf"):
        # enc looks like "application/protobuf;TrackUpdate" — the name after ';'
        # tells you which .proto descriptor to decode with (omitted here).
        schema = enc.split(";", 1)[1] if ";" in enc else "<unspecified>"
        return f"protobuf (schema={schema}) -> {len(raw)} bytes (decode with that descriptor)"
    if enc.startswith("application/cbor"):
        return f"cbor -> {len(raw)} bytes (decode with a cbor lib)"
    return f"unknown encoding {enc!r} -> {len(raw)} bytes"


def main() -> None:
    prefix = os.environ["PARTNER_NAMESPACE"]
    session = open_session()

    # Subscriber first, so it's alive when we publish (a classic footgun is
    # publishing before the subscriber exists — see the goat pub/sub patterns).
    sub = session.declare_subscriber(f"{prefix}/encoding-demo/v1/**")

    samples = [
        ("json", json.dumps({"track": "T-1", "lat": 12.3, "lon": 45.6}).encode()),
        ("text", b"plain status line"),
        ("protobuf:TrackUpdate", b"\x08\x01\x12\x03T-1"),  # bytes stand in for a real encode
    ]

    print(f"[pub] publishing {len(samples)} samples, each with its encoding set")
    for fmt, payload in samples:
        key = f"{prefix}/encoding-demo/v1/{fmt.split(':')[0]}"
        session.put(key, payload, encoding=encoding_for(fmt))

    # Drain what came back and decode off the wire.
    time.sleep(1.0)
    print("[sub] decoding received samples by their self-describing encoding:")
    while True:
        sample = sub.try_recv()
        if sample is None:
            break
        print(f"  {sample.key_expr}: {decode(sample)}")

    session.close()


if __name__ == "__main__":
    main()
