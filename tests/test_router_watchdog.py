"""router_watchdog: restart only after N failures in a row, and not more than once per cooldown."""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))
sys.path.insert(0, str(ROOT / "compose" / "control"))

from router_watchdog import Watchdog  # noqa: E402


def test_restarts_after_consecutive_failures_only():
    dog = Watchdog(fails=3, cooldown_s=600)
    assert [dog.step(False, t) for t in (0, 30)] == [False, False]
    assert dog.step(True, 60) is False                  # a success resets the count
    assert [dog.step(False, t) for t in (90, 120, 150)] == [False, False, True]


def test_cooldown_limits_restarts():
    dog = Watchdog(fails=2, cooldown_s=600)
    assert [dog.step(False, t) for t in (0, 30)] == [False, True]
    assert [dog.step(False, t) for t in (60, 90, 120)] == [False, False, False]      # inside the cooldown
    assert [dog.step(False, t) for t in (700, 730)] == [True, False]       # still failing once the cooldown is over
