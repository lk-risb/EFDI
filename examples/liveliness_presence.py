#!/usr/bin/env python3
"""Reference: presence via Zenoh liveliness — know when a peer is up or gone.

Illustrative, not production. A runnable shape of the goat "liveliness presence"
pattern you can adapt.

The problem it solves: inferring "is this producer alive?" from its data traffic
is a guess. A producer that goes QUIET looks the same as one that DIED — and at
the tactical edge that difference matters. Liveliness is the native answer: a
producer declares a token that says "I exist." Subscribers are told the instant
it drops (a session close, a crash, a partition), and a late joiner can query the
current set of who's-alive on declaration.

This is push-based and cheap — no polling, no heartbeat parsing. The token's
liveliness is tied to the session, so you don't have to remember to "send a
goodbye": if your process dies, the token is withdrawn for you.

Two roles in one script:
  - PRODUCER side: declare a liveliness token under your own prefix.
  - MONITOR side:  subscribe to a presence key expression with history=True
                   (reports who's already alive), then receive PUT (joined) /
                   DELETE (left) events live.

Run two copies in different terminals (one as monitor, one as producer) to watch
a join/leave, or run it once to see your own token reported back.

Connection config comes from the goat profile the pod/bundle gives you:
  EFDI_ROUTER     e.g. tls/<router-host>:7447
  EFDI_MTLS_CERT  path to mtls.cert.pem
  EFDI_MTLS_KEY   path to mtls.key.pem
  EFDI_CA_ROOTS   path to ca-roots.pem
  PARTNER_NAMESPACE     your onboarded prefix, e.g. acme
  EFDI_ROLE       "monitor" (watch presence) or "producer" (declare a token).
                  Default "monitor".
  EFDI_NODE       a name for this node's token, e.g. sensor-7. Default "node-1".

Run:  EFDI_ROLE=monitor  python3 liveliness_presence.py
      EFDI_ROLE=producer python3 liveliness_presence.py   # in another terminal
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


def run_producer(session: zenoh.Session, prefix: str, node: str) -> None:
    # The token's key expression IS the presence signal. Keep it inside your
    # onboarded prefix so the ACL admits it; the monitor watches the parent.
    token_key = f"{prefix}/_meta/alive/{node}"
    token = session.liveliness().declare_token(token_key)
    print(f"[producer] liveliness token declared at '{token_key}' — I am present.")
    print("[producer] Ctrl-C (or kill -9) to drop it; the monitor learns immediately.")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        # Explicit undeclare is tidy, but note: even a hard crash withdraws the
        # token, because its liveliness is tied to the session.
        token.undeclare()
        print("[producer] token undeclared — I am gone.")


def run_monitor(session: zenoh.Session, prefix: str) -> None:
    presence = f"{prefix}/_meta/alive/**"
    # history=True => on declaration, the already-present tokens are reported,
    # so a late-joining monitor sees the current roster, not just future changes.
    sub = session.liveliness().declare_subscriber(presence, history=True)
    print(f"[monitor] watching presence on '{presence}' (history=on). Ctrl-C to stop.")
    try:
        for sample in sub:
            # PUT  = a token appeared (peer present / late-join history)
            # DELETE = a token withdrawn (peer gone)
            if sample.kind == zenoh.SampleKind.PUT:
                print(f"[monitor] UP    {sample.key_expr}")
            else:
                print(f"[monitor] DOWN  {sample.key_expr}")
    except KeyboardInterrupt:
        pass


def main() -> None:
    prefix = os.environ["PARTNER_NAMESPACE"]
    role = os.environ.get("EFDI_ROLE", "monitor")
    node = os.environ.get("EFDI_NODE", "node-1")
    session = open_session()
    try:
        if role == "producer":
            run_producer(session, prefix, node)
        else:
            run_monitor(session, prefix)
    finally:
        session.close()


if __name__ == "__main__":
    main()
