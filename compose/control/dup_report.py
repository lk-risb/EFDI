#!/usr/bin/env python3
"""dup_report.py — find duplicated or looping data on the fabric.

Subscribes to every track under <ORG>/** for --seconds, then prints what looks wrong:
  leaks    records tagged "_src: backbone:*" on a topic that is not a backbone topic (we would
           re-export them), or on a topic of our own export prefix
  echoes   backbone-origin records whose uid, callsign or label embeds the uid or callsign of a
           record this pod published itself (a partner copy of our data)
  twins    one callsign under several uids within TWIN_KM of each other
  junk     uids that look like a flattened field path or a bare word (no digits)
  sources  how many distinct uids each _src contributes

Run it where the pod's Zenoh credentials are (same shell/env as start.sh). Read-only: it never
publishes. Fix a finding with EFDI_DISABLED_SERVICES (stop a feed), BACKBONE_DROP_REGEX (drop
matching backbone records without a code change) or the filters in protocols/vendors/random/.
"""

import argparse
import collections
import json
import math
import re
import time

TWIN_KM = 50.0
_JUNK = re.compile(r"(:/|/)|^[A-Za-z_]+$")
MIN_ID_LEN = 8


def _km(a: dict, b: dict) -> float:
    try:
        dlat = (a["lat_deg"] - b["lat_deg"]) * 111.0
        dlon = (a["lon_deg"] - b["lon_deg"]) * 111.0 * max(0.1, math.cos(math.radians(a["lat_deg"])))
    except (KeyError, TypeError):
        return math.inf
    return math.hypot(dlat, dlon)


def analyse(records: list) -> dict:
    """records: [(topic key, track dict)] -> findings dict (see module docstring)."""
    latest: dict = {}
    for key, rec in records:
        if isinstance(rec, dict) and rec.get("uid") and not rec.get("_delete"):
            latest[(key, rec["uid"])] = rec
    ours = {r["uid"]: r for (k, _), r in latest.items() if not str(r.get("_src", "")).startswith("backbone:")}
    ours_ids = {str(u).upper() for u in ours if len(str(u)) >= MIN_ID_LEN}
    ours_names = {str(r.get("callsign")).strip().upper() for r in ours.values() if len(str(r.get("callsign") or "")) >= MIN_ID_LEN}
    leaks, echoes, junk, by_src = [], [], [], collections.defaultdict(set)
    for (key, uid), rec in latest.items():
        src = str(rec.get("_src", ""))
        by_src[src].add(uid)
        if src.startswith("backbone:") and "/backbone/" not in key:
            leaks.append((key, uid, src))
        if _JUNK.search(str(uid)) and src.startswith("backbone:"):
            junk.append((key, uid, src))
        if src.startswith("backbone:"):
            texts = {str(uid).upper(), str(rec.get("callsign") or "").strip().upper(), str(rec.get("label") or "").strip().upper()} - {""}
            if any(i in t for i in ours_ids for t in texts) or texts & ours_names:
                echoes.append((key, uid, src, rec.get("callsign")))
    by_call = collections.defaultdict(list)
    for (key, uid), rec in latest.items():
        call = str(rec.get("callsign") or "").strip().upper()
        if len(call) >= 3 and rec.get("lat_deg") is not None:
            by_call[call].append((uid, rec))
    twins = []
    for call, group in by_call.items():
        uids = {u for u, _ in group}
        if len(uids) > 1 and any(_km(a, b) <= TWIN_KM for i, (_, a) in enumerate(group) for (_, b) in group[i + 1:]):
            twins.append((call, sorted(uids)))
    return {"leaks": leaks, "echoes": echoes, "twins": sorted(twins), "junk": junk,
            "sources": {s or "(none)": len(u) for s, u in sorted(by_src.items())}}


def _print(findings: dict) -> None:
    for section in ("leaks", "echoes", "twins", "junk"):
        rows = findings[section]
        print("== {} ({})".format(section, len(rows)))
        for row in rows[:40]:
            print("  ", *row)
        if len(rows) > 40:
            print("   ... {} more".format(len(rows) - 40))
    print("== sources")
    for src, count in findings["sources"].items():
        print("  {:6d} {}".format(count, src))


def main() -> None:
    from protocols.vendors.random.gateway import TOPIC_ROOT, open_session, payload_json, subscribe
    ap = argparse.ArgumentParser(description="report duplicated or looping data on the fabric (read-only)")
    ap.add_argument("--seconds", type=int, default=30)
    args = ap.parse_args()
    records: list = []

    def on_sample(sample) -> None:
        try:
            obj = payload_json(sample)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(obj, dict) and (obj.get("uid") or obj.get("_delete")):
            records.append((str(sample.key_expr), obj))

    session = open_session()
    sub = subscribe(session, TOPIC_ROOT + "/**", on_sample)
    print("listening {} s on {}/** ...".format(args.seconds, TOPIC_ROOT), flush=True)
    time.sleep(args.seconds)
    sub.undeclare()
    session.close()
    print("{} records".format(len(records)))
    _print(analyse(records))


if __name__ == "__main__":
    main()
