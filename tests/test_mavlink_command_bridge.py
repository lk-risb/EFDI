import importlib.util
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
sys.path.insert(0, str(COMPOSE))
sys.path.insert(0, str(COMPOSE / "control"))

spec = importlib.util.spec_from_file_location(
    "mavlink_command_bridge",
    COMPOSE / "bridges" / "mavlink_command_bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bridge)


def test_arm_sets_param1_to_one():
    mav_cmd, params = bridge._build_command({"cmd": "arm"})
    assert mav_cmd == 400  # MAV_CMD_COMPONENT_ARM_DISARM
    assert params[0] == 1


def test_disarm_sets_param1_to_zero():
    mav_cmd, params = bridge._build_command({"cmd": "disarm"})
    assert mav_cmd == 400
    assert params[0] == 0


def test_takeoff_puts_altitude_in_param7():
    mav_cmd, params = bridge._build_command({"cmd": "takeoff", "alt_m": 35})
    assert mav_cmd == 22  # MAV_CMD_NAV_TAKEOFF
    assert params[6] == 35


def test_takeoff_defaults_altitude_when_omitted():
    _, params = bridge._build_command({"cmd": "takeoff"})
    assert params[6] == 20


def test_rtl_has_no_required_params():
    mav_cmd, params = bridge._build_command({"cmd": "rtl"})
    assert mav_cmd == 20  # MAV_CMD_NAV_RETURN_TO_LAUNCH
    assert params == (0, 0, 0, 0, 0, 0, 0)


def test_land_command_id():
    mav_cmd, _ = bridge._build_command({"cmd": "land"})
    assert mav_cmd == 21  # MAV_CMD_NAV_LAND


def test_hold_uses_reposition_with_zero_lat_lon():
    mav_cmd, params = bridge._build_command({"cmd": "hold"})
    assert mav_cmd == 192  # MAV_CMD_DO_REPOSITION
    assert params[4] == 0 and params[5] == 0  # lat/lon 0,0 == "hold here"


def test_goto_puts_target_position_in_params():
    mav_cmd, params = bridge._build_command({"cmd": "goto", "lat_deg": 54.68, "lon_deg": 25.28, "alt_m": 100})
    assert mav_cmd == 192  # MAV_CMD_DO_REPOSITION
    assert params[4] == 54.68
    assert params[5] == 25.28
    assert params[6] == 100


def test_goto_requires_lat_lon():
    with pytest.raises(KeyError):
        bridge._build_command({"cmd": "goto"})


def test_goto_rejects_non_finite_coordinates():
    with pytest.raises(bridge.UnknownCommand):
        bridge._build_command({"cmd": "goto", "lat_deg": math.nan, "lon_deg": 25.28})


def test_unknown_command_raises():
    with pytest.raises(bridge.UnknownCommand):
        bridge._build_command({"cmd": "self_destruct"})
