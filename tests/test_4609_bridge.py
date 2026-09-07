import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
sys.path.insert(0, str(COMPOSE))
sys.path.insert(0, str(COMPOSE / "control"))


def _load_bridge(monkeypatch, **env):
    monkeypatch.setenv("STANAG4609_SRT_URL", "srt://0.0.0.0:9000?mode=listener")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    spec = importlib.util.spec_from_file_location(
        "bridge_4609_{}".format(id(env)),
        COMPOSE / "bridges" / "4609_bridge.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _captured_cmd(monkeypatch, module):
    captured = {}

    class _FakeProc:
        pass

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(module.subprocess, "Popen", _fake_popen)
    module._ffmpeg_proc()
    return captured["cmd"]


def test_video_relay_disabled_by_default_keeps_single_output(monkeypatch):
    module = _load_bridge(monkeypatch)
    cmd = _captured_cmd(monkeypatch, module)
    assert "tee" not in cmd
    assert cmd[-3:] == ["-f", "data", "pipe:1"]
    assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0:d:0?"


def test_video_relay_enabled_uses_tee_with_onfail_ignore(monkeypatch):
    module = _load_bridge(
        monkeypatch,
        STANAG4609_VIDEO_RELAY_ENABLE="1",
        STANAG4609_VIDEO_PATH="stanag4609",
    )
    cmd = _captured_cmd(monkeypatch, module)
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "tee"
    tee_spec = cmd[-1]
    # KLV slave — same as the disabled case, unchanged behavior for the metadata path.
    assert "[select=d:f=data]pipe:1" in tee_spec
    # Video slave — best-effort: a failure there must never abort the whole process.
    assert "onfail=ignore" in tee_spec
    assert "rtmp://127.0.0.1:1935/stanag4609" in tee_spec
    assert "-map" in cmd and cmd[cmd.index("-map") + 1] == "0"


def test_video_relay_path_name_is_configurable(monkeypatch):
    module = _load_bridge(
        monkeypatch,
        STANAG4609_VIDEO_RELAY_ENABLE="1",
        STANAG4609_VIDEO_PATH="custom-sensor",
    )
    cmd = _captured_cmd(monkeypatch, module)
    assert "rtmp://127.0.0.1:1935/custom-sensor" in cmd[-1]
