#!/usr/bin/env python3
"""intcore_bridge.py — INT-CORE Dissemination (HTTP Post ADT) -> Zenoh.

C2 INGRESS half of the INT-CORE (NATO Integration Core) pair. Runs a small
HTTP listener that INT-CORE's own Dissemination service POSTs to whenever a
Subscription scoped to EFDI-NVG-Topic matches an item.

This replaced an earlier design that consumed RabbitMQ's TopicSubQueue
directly — live-tested against a real INT-CORE 7 instance and found unsafe:
TopicSubQueue is a private internal pipe with exactly one legitimate
consumer, IntCoreSubscriptionApi (confirmed by decompiling that service's
own appsettings.json: SourceQueues.TopicSubQueue -> ["Topic"]). A second
consumer there doesn't get a copy — AMQP queues are competing-consumer, so
it round-robins messages away from IntCoreSubscriptionApi at random,
silently breaking dissemination for every Topic on that INT-CORE instance,
not just ours. The real pipeline is:
    TopicApi.SaveItems -> TopicSubQueue -> IntCoreSubscriptionApi
        (matches configured Subscriptions per Topic)
    -> SubscriptionQueue -> Dissemination service -> configured ADT
An HTTP Post ADT, wired to a dedicated Subscription + Dissemination scoped to
EFDI-NVG-Topic (never shared with the SW/TAK ADT on the same INT-CORE
instance), is the same integration point already proven live for that SW/TAK
traffic — this bridge is the receiving end of it.

Loop prevention: EFDI's own layers/intcore_layer.py pushes into this same
Topic via SaveItems. A real HttpPostAdt was stood up and captured on the wire
(confirmed by the INT-CORE peer session, not guessed): the POST body is raw
NVG XML with NO envelope, and dataSourceId appears nowhere — not in the body,
not in any header. This bridge therefore CANNOT tell its own echoed items
apart from a real external source by content alone. Self-origin filtering
has to happen on INT-CORE's side instead: EFDI-NVG-Topic's Subscription must
be configured to exclude items whose dataSourceId equals whatever
intcore_layer.py sends (INTCORE_DATA_SOURCE_ID, "efdi" by default) — ask
before wiring dissemination for a new Topic, don't assume this bridge will
catch it.

Content is parsed as NVG 2.0.2 (the only format intcore_layer.py's own Topic
is schema-validated for) — position, uid, label and SIDC only. NVG's optional
ExtendedData fields are not round-tripped.

Egress to INT-CORE belongs in layers/intcore_layer.py; this file only reads.

Configuration (compose/.env):
    INTCORE_BRIDGE_BIND=0.0.0.0         # must be reachable from INT-CORE's containers
    INTCORE_BRIDGE_PORT=8092
    INTCORE_BRIDGE_PATH=/intcore/dissemination
    INTCORE_BRIDGE_TOKEN=               # shared secret INT-CORE sends as X-EFDI-Token —
                                         # confirmed custom Adt headers pass through intact
    INTCORE_SOURCE=intcore              # _src / topic segment tag

Run:
    venv/bin/python3 intcore_bridge.py
    venv/bin/python3 intcore_bridge.py --verbose
"""

import argparse
import hmac
import json
import os
import signal
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from namespace_prefix import topic_root
import zenoh
from protocols.gateway import open_session
from protocols.track_views import semantic_topic, add_version
from bridges.sitaware_bridge import _AFF_SLUG, _DIM_CONFIG

TOPIC_ROOT = topic_root()
NVG_NS = "{https://tide.act.nato.int/schemas/2012/10/nvg}"
MAX_BODY = 1_000_000

_SOURCE = os.environ.get("INTCORE_SOURCE", "intcore")
_TOKEN  = os.environ.get("INTCORE_BRIDGE_TOKEN", "")


# ---------------------------------------------------------------------------
# NVG 2.0.2 -> track (best-effort: position, uid, label, SIDC — see docstring)
# ---------------------------------------------------------------------------

def nvg_item_to_track(xml_str: str) -> tuple[dict, str] | None:
    try:
        root = SafeET.fromstring(xml_str)
    except (DefusedXmlException, ET.ParseError, ValueError):
        return None
    # Real vendor-produced NVG (confirmed live: INT-CORE's KML->NVG 2.0
    # transform) wraps the point inside a <g> group element rather than
    # putting it directly under <nvg> — NVG's own schema allows this, and
    # sitaware_layer.py's own track_to_nvg_item() just happens not to use
    # it. A direct-child-only find() silently matched nothing against real
    # traffic while every hand-built test fixture (no <g>) kept passing.
    point = root.find(".//" + NVG_NS + "point")
    if point is None:
        return None
    try:
        lon = float(point.get("x"))
        lat = float(point.get("y"))
    except (TypeError, ValueError):
        return None

    track: dict = {"lat_deg": lat, "lon_deg": lon, "_ts": time.time(),
                   "_src": _SOURCE, "_ingress": "intcore"}

    uri = point.get("uri", "")
    uid = urllib.parse.unquote(uri.rsplit(":", 1)[-1]) if uri else None
    if uid:
        track["uid"] = uid
    label = point.get("label")
    if label:
        track["callsign"] = label

    z = point.get("z")
    if z is not None:
        try:
            track["alt_m"] = float(z)
        except ValueError:
            pass
    speed = point.get("speed")  # NVG speed is knots (nvg.data.xsd)
    if speed is not None:
        try:
            track["speed_ms"] = float(speed) * 0.514444
        except ValueError:
            pass
    course = point.get("course")
    if course is not None:
        try:
            track["heading_deg"] = float(course)
        except ValueError:
            pass

    timestamp = point.find(NVG_NS + "TimeStamp")
    if timestamp is not None and timestamp.text:
        try:
            track["_ts"] = datetime.fromisoformat(timestamp.text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass

    symbol = point.get("symbol", "")
    sidc = symbol.split(":", 1)[1] if ":" in symbol else symbol
    return track, sidc


def sidc_to_topic(sidc: str) -> str:
    sidc = (sidc or "").upper().replace("*", "-").replace("-", "")
    aff_char = sidc[1] if len(sidc) > 1 else "U"
    dim_char = sidc[2] if len(sidc) > 2 else "G"
    aff = _AFF_SLUG.get(aff_char, "unknown")
    domain, entity, _ = _DIM_CONFIG.get(dim_char, ("land", "unit", "G"))
    return "{}/{}/{}/c2/{}/{}".format(TOPIC_ROOT, domain, _SOURCE, aff, entity)


def publish_item(session, xml_content: str, verbose: bool) -> None:
    result = nvg_item_to_track(xml_content)
    if result is None:
        if verbose:
            print("INT-CORE item ignored: not a parseable NVG point", flush=True)
        return
    track, sidc = result
    topic = add_version(semantic_topic(sidc_to_topic(sidc), track))
    session.put(topic, json.dumps(track).encode(), encoding=zenoh.Encoding.APPLICATION_JSON)
    if verbose:
        print("INT-CORE -> {}".format(topic), flush=True)


def _handle_dissemination(session, body: bytes, verbose: bool) -> None:
    """Publish one disseminated item. No envelope, no loop-prevention field to
    check here — see the module docstring: the real HttpPostAdt POST body is
    raw NVG XML with no dataSourceId anywhere, confirmed on the wire. Anything
    not parseable as an NVG point (including a non-XML body) is dropped."""
    text = body.decode("utf-8", errors="replace").strip()
    if text[:1] != "<":
        if verbose:
            print("INT-CORE dissemination: not an XML body: {}".format(text[:200]), flush=True)
        return
    publish_item(session, text, verbose)


def make_handler_class(session, verbose: bool, path: str):
    class DisseminationHandler(BaseHTTPRequestHandler):
        server_version = "EFDI-INTCORE-Bridge/1.0"
        sys_version = ""

        def _authorized(self) -> bool:
            if not _TOKEN:
                return True
            return hmac.compare_digest(self.headers.get("X-EFDI-Token", ""), _TOKEN)

        def do_POST(self) -> None:
            if self.path.split("?", 1)[0] != path:
                self.send_response(404)
                self.end_headers()
                return
            if not self._authorized():
                self.send_response(401)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self.send_response(400)
                self.end_headers()
                return
            body = self.rfile.read(length)
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            try:
                _handle_dissemination(session, body, verbose)
            except Exception as exc:
                print("INT-CORE dissemination handling error: {}".format(exc), flush=True)

        def log_message(self, fmt, *fmt_args) -> None:
            if verbose:
                print("INT-CORE bridge: " + fmt % fmt_args, flush=True)

    return DisseminationHandler


def run(args) -> None:
    global _SOURCE, _TOKEN
    _SOURCE, _TOKEN = args.source, args.token

    zenoh_retry_s = 5
    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("intcore_bridge Zenoh connect failed: {} — retry in {}s".format(exc, zenoh_retry_s), flush=True)
            time.sleep(zenoh_retry_s)

    handler_class = make_handler_class(session, args.verbose, args.path)
    server = ThreadingHTTPServer((args.bind, args.port), handler_class)
    server.daemon_threads = True

    print("INT-CORE bridge listening on http://{}:{}{}{}".format(
        args.bind, args.port, args.path,
        " — token required" if args.token else ""), flush=True)
    if not args.token:
        print("WARNING: INTCORE_BRIDGE_TOKEN not set — this endpoint accepts "
              "unauthenticated POSTs from anything that can reach it.", flush=True)

    # ThreadingHTTPServer.serve_forever() blocks the main thread on a socket
    # accept loop, which does not catch SIGTERM — Python's default SIGTERM
    # disposition kills the interpreter immediately regardless of what the
    # main thread is doing, no traceback, no log line (same failure
    # tak_layer.py's own run() had to learn the hard way). Run the server on
    # its own thread and block the main thread on a stop Event instead, so
    # SIGTERM is actually observed and the session/socket get closed.
    server_thread = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": 0.5}, daemon=True)
    server_thread.start()

    stop = threading.Event()

    def _on_signal(signum, _frame):
        print("shutting down on {}".format(signal.Signals(signum).name), flush=True)
        stop.set()

    for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(_sig, _on_signal)
        except (OSError, ValueError):
            pass  # not the main thread, or the platform lacks this signal

    try:
        stop.wait()
    except KeyboardInterrupt:
        print("shutting down on KeyboardInterrupt", flush=True)
    finally:
        server.shutdown()
        server.server_close()
        session.close()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="INT-CORE Dissemination (HTTP Post ADT) -> Zenoh")
    ap.add_argument("--bind", default=os.environ.get("INTCORE_BRIDGE_BIND", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("INTCORE_BRIDGE_PORT", "8092")))
    ap.add_argument("--path", default=os.environ.get("INTCORE_BRIDGE_PATH", "/intcore/dissemination"))
    ap.add_argument("--token", default=os.environ.get("INTCORE_BRIDGE_TOKEN", ""))
    ap.add_argument("--source", default=os.environ.get("INTCORE_SOURCE", "intcore"))
    ap.add_argument("--verbose", "-v", action="store_true")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
