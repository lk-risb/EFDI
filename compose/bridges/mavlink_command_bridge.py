#!/usr/bin/env python3
"""MAVLink drone command bridge — Zenoh command requests -> real MAVLink
COMMAND_LONG messages, straight to a drone's autopilot.

============================================================================
STATUS: untested shell, not a confirmed-working bridge yet
============================================================================
No drone/simulator was connected while this was written (confirmed with the
user). What IS verified:
  - Every MAV_CMD id and COMMAND_LONG param layout below is read directly
    from pymavlink's own bundled MAVLink common-dialect definitions
    (pymavlink.dialects.v20.common), which are generated from the public
    MAVLink XML spec (mavlink.io/en/messages/common.html) — not guessed.
    Confirmed live in this environment: MAV_CMD_COMPONENT_ARM_DISARM=400,
    MAV_CMD_NAV_TAKEOFF=22, MAV_CMD_NAV_RETURN_TO_LAUNCH=20,
    MAV_CMD_NAV_LAND=21, MAV_CMD_DO_REPOSITION=192.
  - command_long_send()'s signature (target_system, target_component,
    command, confirmation, param1..param7) is pymavlink's real, current API
    for MAVLink 2.0, confirmed via inspect.signature() in this environment.
  - The command-translation logic (_build_command below) has unit tests
    (tests/test_mavlink_command_bridge.py) checking each command produces
    the right MAV_CMD id and param layout.
UNVERIFIED — needs a real MAVLink-speaking drone or SITL simulator to
confirm before trusting this against real hardware:
  - That connecting via mavutil.mavlink_connection(MAVLINK_ENDPOINT) and
    waiting for a HEARTBEAT actually reaches a real vehicle and populates
    target_system/target_component correctly.
  - That a real autopilot accepts these specific commands in GUIDED/AUTO
    mode without an additional mode-change first — some ArduPilot/PX4
    configurations require the vehicle already be in GUIDED mode before
    accepting MAV_CMD_DO_REPOSITION; this bridge does not attempt to force
    a mode change (deliberately: mode names/numbers are firmware-specific,
    see the module docstring reasoning in this file's design discussion),
    so a command may be ACKed but ignored if the vehicle is in the wrong
    mode. Confirm against the target firmware.
  - COMMAND_ACK result handling end-to-end (this bridge decodes and
    publishes it; whether a real vehicle sends one promptly for every
    command above needs a live check).
Scoped to generic MAVLink-speaking airframes (ArduPilot/PX4-class: DJI
drones with a MAVLink bridge, Parrot with MAVLink firmware, and similar) —
explicitly NOT Autel's EVO Max line, which uses Autel's own proprietary SDK
rather than open MAVLink and would need separate, dedicated integration.
============================================================================

Command flow:
  WebUI -> POST /api/terminal/entities/{id}/command (admin/superadmin only)
         -> zenoh publish on <TOPIC_ROOT>/air/mavlink/cmd/<entity_id>
         -> this bridge's subscriber -> COMMAND_LONG over MAVLink
         -> COMMAND_ACK read back -> published on
            <TOPIC_ROOT>/air/mavlink/ack/<entity_id> for the WebUI to show
            feedback (mirrors the "goto executing/succeeded" event log
            mainline.inc's own TERMINAL app shows for the same kind of
            action).

Command JSON shape (published to the cmd topic):
  {"cmd": "arm"}
  {"cmd": "disarm"}
  {"cmd": "takeoff", "alt_m": 20}
  {"cmd": "rtl"}
  {"cmd": "land"}
  {"cmd": "hold"}                                  # loiter at current position
  {"cmd": "goto", "lat_deg": .., "lon_deg": .., "alt_m": ..}
Every shape may include optional "target_system"/"target_component" ints;
default is whatever system/component this bridge's most recent HEARTBEAT
came from (the single-vehicle case — one MAVLINK_ENDPOINT per bridge
instance, matching one drone/simulator connected at a time).
"""

import argparse
import json
import math
import os
import threading
import time

from pymavlink import mavutil
from pymavlink.dialects.v20 import common as mavlink_common

from namespace_prefix import topic_root
from protocols.gateway import open_session
import zenoh

MAVLINK_ENDPOINT = os.environ.get("MAVLINK_ENDPOINT", "udp:127.0.0.1:14550")
TOPIC_ROOT = topic_root()
CMD_KEY_EXPR = "{}/air/mavlink/cmd/**".format(TOPIC_ROOT)
RECONNECT_S = 10

_lock = threading.Lock()


class UnknownCommand(ValueError):
    pass


def _entity_id_from_topic(key_expr: str) -> str:
    return key_expr.rsplit("/", 1)[-1]


def _ack_topic(entity_id: str) -> str:
    return "{}/air/mavlink/ack/{}".format(TOPIC_ROOT, entity_id)


NAN = float("nan")


def _build_command(request: dict) -> tuple[int, tuple[float, float, float, float, float, float, float]]:
    """Translate a command JSON request into a (MAV_CMD id, 7 params) pair.

    Every command here is a standalone COMMAND_LONG the MAVLink common
    dialect defines as vehicle-firmware-agnostic (no ArduPilot/PX4-specific
    custom_mode numbers involved) — see the module docstring for why
    mode-based commands (e.g. a raw "set mode to LOITER") were deliberately
    avoided instead.
    """
    cmd = request.get("cmd")
    if cmd == "arm":
        return mavlink_common.MAV_CMD_COMPONENT_ARM_DISARM, (1, 0, 0, 0, 0, 0, 0)
    if cmd == "disarm":
        return mavlink_common.MAV_CMD_COMPONENT_ARM_DISARM, (0, 0, 0, 0, 0, 0, 0)
    if cmd == "takeoff":
        alt_m = float(request.get("alt_m", 20))
        return mavlink_common.MAV_CMD_NAV_TAKEOFF, (0, 0, 0, NAN, 0, 0, alt_m)
    if cmd == "rtl":
        return mavlink_common.MAV_CMD_NAV_RETURN_TO_LAUNCH, (0, 0, 0, 0, 0, 0, 0)
    if cmd == "land":
        return mavlink_common.MAV_CMD_NAV_LAND, (0, 0, 0, NAN, 0, 0, 0)
    if cmd == "hold":
        # DO_REPOSITION with no lat/lon (0, 0) means "hold current position"
        # per the common-dialect spec: param5/param6 of 0 are treated as
        # "use current position" by MAVLink-compliant autopilots for this
        # command, the same way MAV_CMD_NAV_LAND's 0/0 above means "land
        # where you are" rather than 0,0 lat/lon (off the coast of Africa).
        return mavlink_common.MAV_CMD_DO_REPOSITION, (-1, 0, 0, NAN, 0, 0, 0)
    if cmd == "goto":
        lat = float(request["lat_deg"])
        lon = float(request["lon_deg"])
        alt = float(request.get("alt_m", 0))
        if not math.isfinite(lat) or not math.isfinite(lon):
            raise UnknownCommand("goto requires finite lat_deg/lon_deg")
        return mavlink_common.MAV_CMD_DO_REPOSITION, (-1, 0, 0, NAN, lat, lon, alt)
    raise UnknownCommand("unknown cmd: {!r}".format(cmd))


class _CommandLink:
    def __init__(self, session, verbose: bool):
        self._session = session
        self._verbose = verbose
        self._master = None
        self._target_system = 0
        self._target_component = 0
        self._connect()

    def _connect(self) -> None:
        self._master = mavutil.mavlink_connection(MAVLINK_ENDPOINT)
        print("mavlink_command_bridge connecting to {} ...".format(MAVLINK_ENDPOINT), flush=True)
        self._master.wait_heartbeat(timeout=30)
        self._target_system = self._master.target_system
        self._target_component = self._master.target_component
        print(
            "mavlink_command_bridge got heartbeat from system {} component {}".format(
                self._target_system, self._target_component
            ),
            flush=True,
        )

    def handle_command(self, sample) -> None:
        entity_id = _entity_id_from_topic(str(sample.key_expr))
        try:
            request = json.loads(bytes(sample.payload).decode())
        except (ValueError, TypeError, UnicodeDecodeError) as exc:
            self._publish_ack(entity_id, {"cmd": None, "result": "REJECTED", "detail": "bad JSON: {}".format(exc)})
            return
        try:
            mav_cmd, params = _build_command(request)
        except (UnknownCommand, KeyError, ValueError) as exc:
            self._publish_ack(entity_id, {"cmd": request.get("cmd"), "result": "REJECTED", "detail": str(exc)})
            return

        target_system = int(request.get("target_system") or self._target_system)
        target_component = int(request.get("target_component") or self._target_component)

        with _lock:
            self._master.mav.command_long_send(target_system, target_component, mav_cmd, 0, *params)
            ack = self._master.recv_match(type="COMMAND_ACK", blocking=True, timeout=5)

        if ack is None:
            self._publish_ack(entity_id, {"cmd": request.get("cmd"), "result": "TIMEOUT"})
            return
        result_name = mavutil.mavlink.enums["MAV_RESULT"][ack.result].name
        self._publish_ack(entity_id, {"cmd": request.get("cmd"), "result": result_name})
        if self._verbose:
            print("mavlink cmd {} -> {}".format(request.get("cmd"), result_name), flush=True)

    def _publish_ack(self, entity_id: str, payload: dict) -> None:
        payload["_ts"] = time.time()
        self._session.put(
            _ack_topic(entity_id),
            json.dumps(payload).encode(),
            encoding=zenoh.Encoding.APPLICATION_JSON,
        )


def main():
    ap = argparse.ArgumentParser(description="MAVLink drone command bridge")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    while True:
        try:
            session = open_session()
            break
        except Exception as exc:
            print("mavlink_command_bridge Zenoh connect failed: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
            time.sleep(RECONNECT_S)

    while True:
        try:
            link = _CommandLink(session, args.verbose)
            session.declare_subscriber(CMD_KEY_EXPR, link.handle_command)
            print("mavlink_command_bridge ready, listening on {}".format(CMD_KEY_EXPR), flush=True)
            while True:
                time.sleep(60)
        except Exception as exc:
            print("mavlink_command_bridge connection error: {} — retry in {}s".format(exc, RECONNECT_S), flush=True)
            time.sleep(RECONNECT_S)


if __name__ == "__main__":
    main()
