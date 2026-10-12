"""Generate the protobuf bindings on first use: they are not committed, and a fresh checkout otherwise fails to
collect every test that imports them."""

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def pytest_configure(config):
    if not (ROOT / "compose" / "generated").exists():
        subprocess.run(["bash", str(ROOT / "scripts" / "generate-protobuf.sh")], check=False, capture_output=True)
