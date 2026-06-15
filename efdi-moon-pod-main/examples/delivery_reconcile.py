#!/usr/bin/env python3
"""Reference: producer-side delivery reconciliation (tier 1 + tier 2).

Illustrative, not production. The canonical contract is
the goat pub/sub delivery-reconciliation pattern; this is a
runnable shape of it you can adapt.

The problem it solves: `put` is fire-and-forget — it returns locally even when
the router refuses your write (you're not onboarded for that key-expr, or your
cert doesn't cover it). Nothing tells you. The platform watches this centrally
(operator monitor), so you don't *have* to do anything. This is the OPT-IN
local agency: keep an intent ledger of what you published, periodically read
your own output back, and reconcile — so you know, locally, when your data
stops landing.

  Tier 0 (default): just publish; rely on the operator monitor. Do NOT confirm
                    every message — that loads the fabric for no benefit on
                    loss-tolerant streams.
  Tier 1 (here):    self-canary — periodic read-back of your own recent keys.
  Tier 2 (here):    intent heartbeat — publish "I sent N" so the operator
                    monitor can catch PARTIAL denial (sent 100, 60 landed).
  Tier 3:           must-land messages → Query/Reply admission (not shown;
                    see the goat delivery-reconciliation pattern).

Connection config comes from the goat profile the pod/bundle gives you:
  GOAT_ROUTER         e.g. tls/<router-host>:7447   (profile.toml router_endpoint)
  GOAT_MTLS_CERT      path to mtls.cert.pem
  GOAT_MTLS_KEY       path to mtls.key.pem
  GOAT_CA_ROOTS       path to ca-roots.pem
  GOAT_PREFIX         your onboarded vendor prefix, e.g. acme

Run:  python3 delivery_reconcile.py
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
    conf.insert_json5("connect/endpoints", json.dumps([os.environ["GOAT_ROUTER"]]))
    # mTLS — the trial fabric requires a chain-verifiable client cert.
    conf.insert_json5("transport/link/tls/root_ca_certificate",
                      json.dumps(os.environ["GOAT_CA_ROOTS"]))
    conf.insert_json5("transport/link/tls/connect_certificate",
                      json.dumps(os.environ["GOAT_MTLS_CERT"]))
    conf.insert_json5("transport/link/tls/connect_private_key",
                      json.dumps(os.environ["GOAT_MTLS_KEY"]))
    return zenoh.open(conf)


def self_canary(session: zenoh.Session, prefix: str, intent_ledger: set[str]) -> set[str]:
    """Tier 1 — read your own recent output back and reconcile against intent.

    Returns the set of keys you published that did NOT come back (the gap).
    A non-empty gap means your writes are not landing — you now KNOW, locally,
    and can react (alert your own ops, back off, retry, local dead-letter).

    Note: read-back only tells you a key is *present in storage*. Use a unique
    key/payload per probe (we do — timestamp-suffixed) so a stale retained
    value can't mask a denial. "Not present" conflates denied / no-storage /
    TTL-expired — for a definitive denied-vs-allowed test see goat pub --confirm.
    """
    # QueryTarget.ALL — storage queryables aren't flagged "complete", so
    # AllComplete returns 0 (same shape panoscope uses for stale-recall).
    replies = session.get(
        f"{prefix}/**",
        target=zenoh.QueryTarget.ALL,
        consolidation=zenoh.ConsolidationMode.NONE,
    )
    landed: set[str] = set()
    for reply in replies:
        if reply.ok is not None:
            landed.add(str(reply.ok.key_expr))
    return intent_ledger - landed


def main() -> None:
    prefix = os.environ["GOAT_PREFIX"]
    session = open_session()
    intent_ledger: set[str] = set()
    sent_this_interval = 0

    try:
        while True:
            # --- publish your real data here, at full rate (tier 0) -------------
            # Example: one sample to a unique key, recording intent.
            key = f"{prefix}/example/v1/{int(time.time() * 1000)}"
            session.put(key, b"hello")          # fire-and-forget; returns even if denied
            intent_ledger.add(key)
            sent_this_interval += 1

            # --- tier 1: self-canary every ~30s, NOT every message --------------
            # (here we reconcile once per loop for illustration; in practice run
            #  this on a 30s timer, decoupled from your publish rate.)
            gap = self_canary(session, prefix, intent_ledger)
            if gap:
                print(f"[reconcile] {len(gap)} of {len(intent_ledger)} writes did NOT land "
                      f"— delivery problem (not onboarded for this prefix? cert mismatch?). "
                      f"examples: {sorted(gap)[:3]}")
            else:
                print(f"[reconcile] all {len(intent_ledger)} tracked writes landed")

            # --- tier 2: intent heartbeat -> lets the operator monitor catch
            #     PARTIAL denial (it compares your claimed N to what landed). ----
            hb = json.dumps({"prefix": prefix, "sent": sent_this_interval,
                             "ts_ms": int(time.time() * 1000)})
            session.put(f"{prefix}/_meta/heartbeat", hb.encode())
            sent_this_interval = 0

            # keep the ledger bounded; in practice age out by timestamp.
            if len(intent_ledger) > 1000:
                intent_ledger = set(sorted(intent_ledger)[-500:])

            time.sleep(30)
    finally:
        session.close()


if __name__ == "__main__":
    main()
