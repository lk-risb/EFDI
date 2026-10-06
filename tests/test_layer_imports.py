"""Every layer script must import under the launcher's PYTHONPATH (compose/, compose/control/, generated/).

sitaware_layer and intcore_layer imported `compose.layers...`, which only resolves with the repo root
on sys.path; the launcher does not put it there, so both died at start-up and their log filled with
ModuleNotFoundError while the rest of the suite (which adds extra paths) stayed green.
"""

import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
LAUNCHER_PATH = os.pathsep.join(str(COMPOSE / p) for p in ("generated", "generated/protocols", "", "control"))


@pytest.mark.parametrize("module", [
    "layers.vendors.tak.systematic.sitaware_layer",
    "layers.vendors.tak.random.intcore_layer",
    "layers.vendors.tak.tak_layer",
])
def test_layer_imports_with_the_launcher_pythonpath(module):
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": LAUNCHER_PATH, "POD_STATE_DIR": "/tmp"}
    out = subprocess.run([sys.executable, "-c", "import " + module], capture_output=True, text=True, env=env, cwd="/")
    assert out.returncode == 0, out.stderr[-600:]
