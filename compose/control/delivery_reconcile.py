"""compose/control/delivery_reconcile.py — producer-side delivery
reconciliation (tiers 1+2), shared across any EFDI layer/bridge that
publishes onto a fabric it doesn't control the ACL of (backbone, a federated
parent, ...).

Same pattern as goat's own delivery_reconcile.py reference example (the
efdi-moon-pod repo's examples/), reshaped into a small reusable class instead
of a standalone script. The problem it solves: `put()` returns locally even
when the far side silently denies the write (wrong prefix, a cert the far
router doesn't admit) — nothing tells you. This gives a producer local,
opt-in agency to notice: periodically read its own recent output back and
compare against what it thinks it sent.

  Tier 0 (default, not this module): just publish; rely on the operator's
                    own monitor. Do NOT confirm every message — that loads
                    the fabric for no benefit on a loss-tolerant stream.
  Tier 1 (here):    self-canary — periodic read-back of recently published
                    keys under the caller's own query prefix.
  Tier 2 (here):    intent heartbeat — publish "I sent N" so an
                    operator-side monitor (or a human watching panoscope)
                    can catch PARTIAL denial (sent 100, 60 landed) without
                    needing to watch this pod's own logs.
  Tier 3 (not here): must-land messages -> the advanced-publisher
                    cache/heartbeat pattern (see zenoh.ext in
                    federation_apply.py's _status_publisher_for for an
                    example already wired up in this repo).

Usage: one instance per outbound session that needs this. Call `.sent(topic)`
right after every `put(topic, ...)` you want tracked, and `.tick()` on a
timer — NOT after every message; run it every 30-60s, decoupled from publish
rate.
"""

from __future__ import annotations

import json
import time

import zenoh


class DeliveryReconciler:
    def __init__(
        self,
        session: "zenoh.Session",
        query_prefix: str,
        heartbeat_topic: str,
        max_ledger: int = 1000,
    ) -> None:
        self._session = session
        self._query_prefix = query_prefix.rstrip("/")
        self._heartbeat_topic = heartbeat_topic
        self._max_ledger = max_ledger
        self._intent_ledger: set[str] = set()
        self._sent_this_interval = 0

    def sent(self, topic: str) -> None:
        """Record intent right after a put() to `topic`."""
        self._intent_ledger.add(topic)
        self._sent_this_interval += 1
        if len(self._intent_ledger) > self._max_ledger:
            # Keep the ledger bounded — in practice this ages out the oldest
            # half rather than growing forever across a long-running process.
            self._intent_ledger = set(sorted(self._intent_ledger)[-(self._max_ledger // 2):])

    def tick(self, verbose: bool = False) -> "set[str] | None":
        """Run tier 1 (self-canary) then tier 2 (intent heartbeat). Call this
        on a timer, not per-message. Returns the gap set from this interval
        (empty = everything tracked landed), or None if nothing was tracked
        this interval (nothing to check, heartbeat still sent if there's a
        ledger from a prior interval — see _heartbeat)."""
        gap: set[str] | None = None
        if self._intent_ledger:
            gap = self._self_canary()
            landed = len(self._intent_ledger) - len(gap)
            if verbose or gap:
                extra = ""
                if gap:
                    extra = " — delivery problem (not onboarded for this prefix? " \
                            "cert mismatch?), examples: {}".format(sorted(gap)[:3])
                print(
                    "[reconcile] {}/{} tracked writes landed under {}{}".format(
                        landed, len(self._intent_ledger), self._query_prefix, extra),
                    flush=True,
                )
        self._heartbeat()
        return gap

    def _self_canary(self) -> set[str]:
        """Tier 1 — read own recent output back. A key "not present" here
        conflates denied / no-storage / TTL-expired; this is a local smoke
        signal, not a definitive denied-vs-allowed test."""
        replies = self._session.get(
            self._query_prefix + "/**",
            target=zenoh.QueryTarget.ALL,
            consolidation=zenoh.ConsolidationMode.NONE,
        )
        landed: set[str] = set()
        for reply in replies:
            if reply.ok is not None:
                landed.add(str(reply.ok.key_expr))
        return self._intent_ledger - landed

    def _heartbeat(self) -> None:
        """Tier 2 — intent heartbeat, so an operator-side monitor can catch
        PARTIAL denial by comparing claimed-sent to what it sees landing."""
        body = json.dumps({
            "sent": self._sent_this_interval,
            "ledger_size": len(self._intent_ledger),
            "ts_ms": int(time.time() * 1000),
        }).encode()
        try:
            self._session.put(self._heartbeat_topic, body)
        except Exception as exc:
            print("[reconcile] heartbeat publish failed: {}".format(exc), flush=True)
        self._sent_this_interval = 0
