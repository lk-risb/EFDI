"""stop.sh must not orphan a service that ignores SIGTERM, and start.sh must retire a
leftover copy of a service whose script moved (the duplicate-tak_layer incident)."""
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(shutil.which("bash") is None or not Path("/proc/self").exists(),
                                reason="needs bash and /proc")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:                                    # a zombie is dead for our purposes
        return Path("/proc/{}/stat".format(pid)).read_text().split()[2] != "Z"
    except OSError:
        return False


def _spawn(script_path, *args, ignore_term=False):
    trap = 'trap "" TERM; ' if ignore_term else ""
    proc = subprocess.Popen(["bash", "-c", trap + "while :; do sleep 0.2; done", str(script_path), *args],
                            start_new_session=True)
    time.sleep(0.3)                         # let bash install the trap before anyone signals it
    return proc


def _reap(*procs):
    for proc in procs:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGKILL)
        proc.wait()


def test_stop_sh_kills_a_process_that_ignores_sigterm_before_dropping_its_pidfile(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "compose").mkdir()
    shutil.copy(ROOT / "scripts/stop.sh", tmp_path / "scripts/stop.sh")
    pid_dir = tmp_path / "state/.pids"
    pid_dir.mkdir(parents=True)
    stubborn = _spawn(tmp_path / "compose/svc.py", ignore_term=True)
    (pid_dir / "svc.pid").write_text(str(stubborn.pid))
    try:
        subprocess.run(["bash", str(tmp_path / "scripts/stop.sh"), "native"], check=True, timeout=30,
                       env={**os.environ, "POD_STATE_DIR": str(tmp_path / "state"), "STOP_GRACE_S": "1"},
                       capture_output=True)
        stubborn.wait(timeout=5)
        assert not _alive(stubborn.pid)
        assert not (pid_dir / "svc.pid").exists()
    finally:
        _reap(stubborn)


def _bash_functions(text, *names):
    out = []
    for name in names:
        match = re.search(r"^{}\(\) \{{.*?^\}}$".format(re.escape(name)), text, re.S | re.M)
        assert match, name
        out.append(match.group(0))
    return "\n".join(out)


def test_start_sh_retires_a_moved_copy_but_leaves_other_processes(tmp_path):
    compose = tmp_path / "compose"
    pid_dir = tmp_path / ".pids"
    pid_dir.mkdir()
    functions = _bash_functions((ROOT / "scripts/start.sh").read_text(), "pid_claimed_by_other", "pid_has_args",
                                "stop_pid", "retire_moved_instances")
    old_copy = _spawn(compose / "layers/tak_layer.py", "--host", "a")
    same_path = _spawn(compose / "layers/vendors/tak/tak_layer.py", "--host", "a")   # the live service itself
    other_args = _spawn(compose / "layers/tak_layer.py", "--host", "b")
    other_name = _spawn(compose / "layers/other_layer.py", "--host", "a")
    try:
        script = 'COMPOSE_DIR="{}"; PID_DIR="{}"; DIM=""; R=""\n{}\nretire_moved_instances tak_layer ' \
                 "layers/vendors/tak/tak_layer.py --host a".format(compose, pid_dir, functions)
        subprocess.run(["bash", "-c", script], check=True, timeout=30, capture_output=True,
                       env={**os.environ, "STOP_GRACE_S": "1"})
        old_copy.wait(timeout=5)
        assert not _alive(old_copy.pid)
        assert _alive(same_path.pid) and _alive(other_args.pid) and _alive(other_name.pid)
    finally:
        _reap(old_copy, same_path, other_args, other_name)


def test_start_sh_never_starts_a_service_listed_in_efdi_disabled_services():
    functions = _bash_functions((ROOT / "scripts/start.sh").read_text(), "svc_disabled")
    script = functions + '\nfor n in socbx backbone-bridge socbx2 tak_layer; do svc_disabled "$n" && echo "$n"; done; true'
    for value in ("socbx backbone-bridge", "socbx,backbone-bridge", "  socbx   backbone-bridge "):
        out = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True,
                             env={**os.environ, "EFDI_DISABLED_SERVICES": value}).stdout.split()
        assert out == ["socbx", "backbone-bridge"]               # exact names only, not socbx2 or tak_layer
    unset = subprocess.run(["bash", "-c", script], check=True, capture_output=True, text=True,
                           env={k: v for k, v in os.environ.items() if k != "EFDI_DISABLED_SERVICES"}).stdout
    assert unset == ""
