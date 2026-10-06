"""check_docker_disk_space: incremental needs a small fixed allowance, a full rebuild scales with the image, nothing is pruned."""

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _run(tmp_path, free_mb, image_mb, mode="", env=""):
    stub = tmp_path / "bin"
    stub.mkdir()
    log = tmp_path / "docker.log"
    (stub / "docker").write_text(
        '#!/usr/bin/env bash\necho "$*" >> {log}\n'
        'case "$1 $2" in\n'
        '  "info --format") echo "{root}";;\n'
        '  "image inspect") echo {size};;\n'
        '  "compose -f") echo myimage;;\n'
        'esac\n'.format(log=log, root=tmp_path, size=image_mb * 1048576))
    (stub / "df").write_text('#!/usr/bin/env bash\nprintf "Filesystem 1M-blocks Used Available\\nx 0 0 {}\\n"\n'.format(free_mb))
    for f in stub.iterdir():
        f.chmod(0o755)
    script = ('fail(){ echo "FAIL: $*"; exit 1; }; warn(){ echo "WARN: $*"; }; ok(){ echo "OK: $*"; }; '
              'COMPOSE_FILE=c ENV_FILE=e; . %s; %s check_docker_disk_space %s') % (
        ROOT / "scripts/check_docker_disk_space.sh", env, mode)
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={"PATH": "{}:/usr/bin:/bin".format(stub)})
    return out.returncode, out.stdout, log.read_text() if log.exists() else ""


def test_incremental_update_needs_only_a_small_allowance(tmp_path):
    code, out, _ = _run(tmp_path, free_mb=2000, image_mb=6000)
    assert code == 0 and "1536 MiB needed" in out


def test_full_rebuild_scales_with_the_largest_image_and_never_prunes(tmp_path):
    code, out, log = _run(tmp_path, free_mb=9000, image_mb=5000, mode="full")
    assert code == 1 and "11024 MiB needed" in out
    assert "prune" not in log


def test_explicit_override_wins(tmp_path):
    code, out, _ = _run(tmp_path, free_mb=100, image_mb=5000, mode="full", env="EFDI_UPDATE_MIN_FREE_MB=50")
    assert code == 0 and "50 MiB needed" in out
