#!/usr/bin/env python3
"""retract_uids.py — send a CoT delete for each uid read from stdin, one per line.

Clients keep drawing-tool shapes (trail lines, zones) until they are told to delete them, so a shape whose
sender lost track of it stays on the map for good. List the uids as TAK stores them (the full CoT uid, e.g.
EFDI-UID-MAPA-309760-TRAIL) and this sends the same forced delete tak_layer sends for a withdrawn shape.

Typical use, with the TAK_* connection settings of tak_layer in the environment:
  psql ... -At -c "select distinct uid from cot_router where uid like '%-TRAIL'" | \\
      python3 retract_uids.py --dry-run          # then again without --dry-run

Deleting a uid that a bridge is still publishing is harmless: the next update draws it again.
"""

import argparse
import os
import sys
import time

from layers.vendors.tak.tak_layer import _delete_point_cot


def delete_events(uids, link_type: str = "u-d-f") -> list:
    seen, out = set(), []
    for line in uids:
        uid = line.strip()
        if uid and uid not in seen:
            seen.add(uid)
            out.append(_delete_point_cot(uid, None, link_type))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", action="append", default=None, help="TAK Server host (repeatable); default TAK_HOST")
    ap.add_argument("--port", type=int, default=int(os.environ.get("TAK_PORT") or 8089))
    ap.add_argument("--tls", action="store_true", default=os.environ.get("TAK_TLS") == "1")
    ap.add_argument("--cert", default=os.environ.get("TAK_CERT"))
    ap.add_argument("--key", default=os.environ.get("TAK_KEY"))
    ap.add_argument("--ca", default=os.environ.get("TAK_CA"))
    ap.add_argument("--tls-server-name", default=os.environ.get("TAK_TLS_SERVER_NAME"))
    ap.add_argument("--type", default="u-d-f", help="type named in the delete link (default u-d-f, a drawing)")
    ap.add_argument("--dry-run", action="store_true", help="print the uids and send nothing")
    args = ap.parse_args(argv)
    events = delete_events(sys.stdin, args.type)
    if args.dry_run:
        print("{} delete(s) would be sent".format(len(events)))
        return 0
    hosts = [(host, args.port) for host in (args.host or [os.environ.get("TAK_HOST", "")]) if host]
    if not hosts:
        print("no TAK host: pass --host or set TAK_HOST", file=sys.stderr)
        return 2
    if args.tls and not args.ca:
        print("--ca / TAK_CA is required with --tls", file=sys.stderr)
        return 2
    from protocols.vendors.random.tak_transport import TcpSender
    sender = TcpSender(hosts, tls=args.tls, certfile=args.cert, keyfile=args.key, cafile=args.ca,
                       server_name=args.tls_server_name)
    for xml in events:
        sender.send(xml)
    while not sender._q.empty():            # the writer thread owns the socket; wait for it to drain
        time.sleep(0.2)
    time.sleep(1.0)
    sender.close()
    print("sent {} delete(s)".format(len(events)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
