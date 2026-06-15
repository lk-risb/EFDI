#!/usr/bin/env python3
"""request_reply.py — the query (get) + queryable (serve) pattern.

Zenoh isn't just pub/sub: a "queryable" answers on-demand requests for a key, like a
lightweight RPC / key-value read. Two roles:

    python3 request_reply.py serve     # run a queryable that answers <namespace>/status
    python3 request_reply.py get       # query it and print the reply(ies)

Useful for "what's the current value?" / health / config-read without a standing subscription.
"""
import sys
import os
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "connect", "python"))
import goat_connect


def serve() -> None:
    key = goat_connect.key("status")
    with goat_connect.session() as s:
        def on_query(query):
            query.reply(key, b'{"status":"ok","uptime_s":42}')
            print(f"answered query for {query.selector}", flush=True)

        q = s.declare_queryable(key, on_query)
        print(f"serving queryable: {key} (Ctrl-C to stop)", flush=True)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            q.undeclare()


def get() -> None:
    key = goat_connect.key("status")
    with goat_connect.session() as s:
        replies = s.get(key)
        for reply in replies:
            try:
                ok = reply.ok
                print(f"reply: {ok.key_expr}  {bytes(ok.payload).decode('utf-8')}", flush=True)
            except Exception as e:
                print(f"reply error: {e}", flush=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "get"
    (serve if mode == "serve" else get)()
