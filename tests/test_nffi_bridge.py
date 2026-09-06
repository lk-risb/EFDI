import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose"
sys.path.insert(0, str(COMPOSE))
sys.path.insert(0, str(COMPOSE / "control"))

spec = importlib.util.spec_from_file_location(
    "nffi_bridge",
    COMPOSE / "bridges" / "nffi_bridge.py",
)
bridge = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(bridge)


def _feed(buffer, chunk: str):
    buffer[0] += chunk
    return bridge._extract_frames(buffer)


def test_extracts_one_complete_nffimessage_document():
    buffer = [""]
    doc = "<NFFIMessage xmlns=\"urn:nato:fft:protocols:nffi14\"><track/></NFFIMessage>"
    frames = _feed(buffer, doc)
    assert frames == [doc]
    assert buffer[0] == ""


def test_extracts_two_back_to_back_documents_split_across_chunks():
    buffer = [""]
    doc1 = "<NFFIMessage xmlns=\"ns\"><track/></NFFIMessage>"
    doc2 = "<NFFIMessage xmlns=\"ns\"><track/></NFFIMessage>"

    frames = _feed(buffer, doc1[:20])
    assert frames == []  # incomplete — nothing to extract yet

    frames = _feed(buffer, doc1[20:] + doc2[:10])
    assert frames == [doc1]

    frames = _feed(buffer, doc2[10:])
    assert frames == [doc2]


def test_tolerates_a_bare_track_with_no_nffimessage_wrapper():
    # parse_nffi() in nffi.py itself accepts a bare <track> root with no
    # NFFIMessage wrapper — the bridge's framing must recognize the same
    # shape, since which one a real server sends is unconfirmed (see the
    # module docstring's STATUS section).
    buffer = [""]
    doc = "<track xmlns=\"ns\"><positionalData/></track>"
    frames = _feed(buffer, doc)
    assert frames == [doc]


def test_ignores_noise_before_the_first_recognized_tag():
    buffer = [""]
    doc = "<NFFIMessage><track/></NFFIMessage>"
    frames = _feed(buffer, "\r\n  " + doc)
    assert frames == [doc]


class _Args:
    host = ""
    port = 0
    tls = False
    cert = key = ca = tls_server_name = None
    verbose = False


def test_run_refuses_to_start_without_a_host():
    with pytest.raises(SystemExit):
        bridge.run(_Args())


def test_run_refuses_to_start_without_a_port(monkeypatch):
    monkeypatch.setenv("NFFI_HOST", "")
    args = _Args()
    args.host = "192.168.1.1"
    with pytest.raises(SystemExit):
        bridge.run(args)
