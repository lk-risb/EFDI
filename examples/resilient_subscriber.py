#!/usr/bin/env python3
"""Reference: a resilient subscriber that survives a partition and catches up.

Illustrative, not production. A runnable shape of the goat "advanced
subscriber" pattern you can adapt.

The problem it solves: a plain subscriber only ever receives samples that arrive
AFTER it is declared. If your link drops and reconnects, the subscriber restarts
cold — it silently misses everything published while it was away. At the tactical
edge that is exactly when you most need the data you missed.

The advanced subscriber adds two things, both served by the stock router (this
is a client-side library feature — nothing special is deployed for it):

  * HISTORY  — on (re)connect, query for what was published while you were gone,
               deliver it, then continue live. ("Show me what happened while I
               was disconnected.")
  * RECOVERY — periodically re-query for samples that dropped on the wire, so a
               momentary loss heals on its own.

Pair it with a publisher that enables caching + miss-detection (see
must_deliver_publisher.py) and you also get true, sequence-numbered miss
detection: the subscriber is told *which* samples it missed, by source.
Against a plain publisher the same code degrades gracefully — history pulls
from the router's per-prefix storage and recovery re-pulls the latest retained
value on its timer.

Two knobs to set consciously:
  - history max_age caps the on-connect catch-up so a long-absent subscriber
    pulls the recent past, not the storage's full retention. Pick the window
    your mission actually needs.
  - For a *live tail* (you only care about now; deep replay comes from a
    separate history read), use recovery WITHOUT history so opening the tail
    doesn't dump a backlog — see the `recovery_only` note below.

Connection config comes from the goat profile the pod/bundle gives you:
  EFDI_ROUTER     e.g. tls/<router-host>:7447   (profile.toml router_endpoint)
  EFDI_MTLS_CERT  path to mtls.cert.pem
  EFDI_MTLS_KEY   path to mtls.key.pem
  EFDI_CA_ROOTS   path to ca-roots.pem
  EFDI_KEY_EXPR   the key expression to subscribe to, e.g. acme/tracks/v1/**

Run:  python3 resilient_subscriber.py
Deps: pip install eclipse-zenoh==1.9.0
"""

import json
import os
import threading
import time
from datetime import timedelta

import zenoh
import zenoh.ext


def open_session() -> zenoh.Session:
    """Open an mTLS client session against the goat router, from env config."""
    conf = zenoh.Config()
    conf.insert_json5("mode", json.dumps("client"))
    conf.insert_json5("connect/endpoints", json.dumps([os.environ["EFDI_ROUTER"]]))
    # mTLS — the fabric requires a chain-verifiable client cert.
    conf.insert_json5("transport/link/tls/root_ca_certificate",
                      json.dumps(os.environ["EFDI_CA_ROOTS"]))
    conf.insert_json5("transport/link/tls/connect_certificate",
                      json.dumps(os.environ["EFDI_MTLS_CERT"]))
    conf.insert_json5("transport/link/tls/connect_private_key",
                      json.dumps(os.environ["EFDI_MTLS_KEY"]))
    return zenoh.open(conf)


def main() -> None:
    key_expr = os.environ.get("EFDI_KEY_EXPR", "**")
    session = open_session()

    # HISTORY: on (re)connect, catch up the last hour. detect_late_publishers
    # lets us also pull history from publishers that join after us (only works
    # against publishers that advertise themselves — see must_deliver_publisher.py).
    history = zenoh.ext.HistoryConfig(detect_late_publishers=True, max_age=3600.0)

    # RECOVERY: every 5s, re-query for anything we should have but didn't.
    # Use periodic_queries for sporadic/plain publishers. If your publisher
    # emits heartbeats (must_deliver_publisher.py), prefer RecoveryConfig(
    # heartbeat=True) instead — it recovers from the publisher's sequence
    # numbers rather than blind re-queries.
    recovery = zenoh.ext.RecoveryConfig(periodic_queries=timedelta(seconds=5), heartbeat=None)

    # For a LIVE TAIL (no on-connect backlog), drop `history=` and keep only
    # `recovery=` — you still heal mid-session drops but don't replay the past
    # when you open the tail.

    subscriber = zenoh.ext.declare_advanced_subscriber(
        session,
        key_expr,
        history=history,
        recovery=recovery,
        subscriber_detection=True,  # advertise ourselves so publishers can see us
    )

    # MISS NOTIFICATIONS: a side channel that tells you *which* samples were
    # missed and from whom — populated only by publishers that enable
    # sample-miss detection. Harmless (silent) against plain publishers.
    miss_listener = subscriber.sample_miss_listener()

    def watch_misses() -> None:
        for miss in miss_listener:
            print(f"[miss] {miss.nb} sample(s) missed from source {miss.source}")

    threading.Thread(target=watch_misses, daemon=True).start()

    print(f"[sub] resilient subscriber on '{key_expr}' "
          f"(history: last 1h on connect; recovery: 5s). Ctrl-C to stop.")
    try:
        for sample in subscriber:
            # sample.encoding is self-describing when the publisher set it —
            # see self_describing_encoding.py for picking a decoder off the wire.
            payload = sample.payload.to_bytes()
            ts = sample.timestamp  # router-stamped HLC; use for ordering, not wall-clock
            ts_str = ts.get_time().isoformat() if ts else "<none>"
            print(f"[sub] {sample.key_expr}  ({len(payload)}B, enc={sample.encoding}, t={ts_str})")
    except KeyboardInterrupt:
        pass
    finally:
        time.sleep(0)  # let daemon thread settle
        session.close()


if __name__ == "__main__":
    main()
