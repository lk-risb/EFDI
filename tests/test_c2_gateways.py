"""Focused lifecycle tests for the unified C2 gateway processes."""

from __future__ import annotations

import json
import os
import pathlib
import queue
import sys
from unittest import mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "compose"))

from c2 import sitaware_gateway, tak_gateway  # noqa: E402
from bridges import nvg_bridge, tak_bridge  # noqa: E402


def test_tak_out_only_passes_explicit_empty_argv():
    with (
        mock.patch.object(tak_gateway.cot_layer, "main") as egress,
        mock.patch.object(tak_gateway.tak_bridge, "main") as ingress,
    ):
        tak_gateway.main(["--direction", "out"])

    egress.assert_called_once_with([])
    ingress.assert_not_called()


def test_tak_worker_failure_is_reported_and_wakes_main():
    failures: queue.Queue[tak_gateway.Failure] = queue.Queue()

    def fail(_argv):
        raise ValueError("broken ingress")

    with mock.patch.object(tak_gateway._thread, "interrupt_main") as wake:
        tak_gateway._run_worker("TAK ingress", fail, failures, True)

    wake.assert_called_once_with()
    with pytest.raises(RuntimeError, match="TAK ingress failed: broken ingress"):
        tak_gateway._raise_failure(failures)


def test_sitaware_configured_legs_respect_explicit_feed_disable():
    env = {
        "SITAWARE_HQ_NVG_ENABLE": "0",
        "SITAWARE_URL": "https://sitaware.invalid",
        "SITAWARE_API_PATH": "/documented/units",
    }
    with mock.patch.dict(os.environ, env, clear=True):
        assert sitaware_gateway.configured_legs() == {
            "feed": False,
            "nvg_ingress": True,
            "rest_ingress": True,
        }


def test_sitaware_rejects_direction_with_no_configured_leg():
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(SystemExit, match="no configured SitaWare gateway legs"):
            sitaware_gateway.main(["--direction", "both"])


def test_sitaware_out_only_passes_explicit_empty_argv():
    with (
        mock.patch.dict(
            os.environ,
            {"SITAWARE_HQ_NVG_ENABLE": "1"},
            clear=True,
        ),
        mock.patch.object(sitaware_gateway.nvg_layer, "main") as feed,
    ):
        sitaware_gateway.main(["--direction", "out"])

    feed.assert_called_once_with([])


def test_nvg_import_rejects_non_network_url():
    with (
        mock.patch.object(nvg_bridge, "run") as run,
        pytest.raises(SystemExit, match=r"must be an http:// or https:// URL"),
    ):
        nvg_bridge.main(["--url", "file:///etc/passwd"])

    run.assert_not_called()


def test_untrusted_c2_xml_does_not_expand_entities():
    malicious = """\
<!DOCTYPE event [<!ENTITY local SYSTEM "file:///etc/passwd">]>
<event uid="unsafe" type="a-f-G-U-C"><detail><remarks>&local;</remarks></detail></event>
"""

    assert nvg_bridge.parse_nvg(malicious.encode()) == []
    assert tak_bridge._parse_event_xml(malicious) is None


def test_tak_tls_verifies_configured_server_name():
    raw = mock.MagicMock()
    wrapped = mock.MagicMock()
    context = mock.MagicMock()
    context.wrap_socket.return_value = wrapped

    with (
        mock.patch.object(tak_bridge.socket, "create_connection", return_value=raw),
        mock.patch.object(
            tak_bridge.ssl,
            "create_default_context",
            return_value=context,
        ) as create_context,
    ):
        result = tak_bridge._connect(
            "tak.efdi.ltu",
            8089,
            True,
            "/cert.pem",
            "/key.pem",
            "/ca.pem",
            "takserver",
        )

    assert result is wrapped
    create_context.assert_called_once_with(
        tak_bridge.ssl.Purpose.SERVER_AUTH,
        cafile="/ca.pem",
    )
    assert context.check_hostname is True
    context.load_cert_chain.assert_called_once_with("/cert.pem", "/key.pem")
    context.wrap_socket.assert_called_once_with(raw, server_hostname="takserver")


def test_tak_ingress_drops_explicit_fabric_reflection():
    event = tak_bridge._parse_event_xml(
        """\
<event uid="EFDI-SENS-DRONU-48CAB607" type="a-n-G-E-S"
       time="2026-07-28T10:00:00Z" start="2026-07-28T10:00:00Z"
       stale="2026-07-28T10:02:00Z" how="m-g">
  <point lat="54.7" lon="25.2" hae="100" ce="10" le="10"/>
  <detail>
    <efdi role="fabric-export"/>
    <contact callsign="sensor"/>
  </detail>
</event>
"""
    )

    assert event is not None
    assert tak_bridge._normalize_event(event) is None


def test_tak_ingress_keeps_unmarked_efdi_uid():
    event = tak_bridge._parse_event_xml(
        """\
<event uid="EFDI-EXTERNAL-MARKER" type="a-f-G-U-C"
       time="2026-07-28T10:00:00Z" start="2026-07-28T10:00:00Z"
       stale="2026-07-28T10:02:00Z" how="h-e">
  <point lat="54.7" lon="25.2" hae="100" ce="10" le="10"/>
  <detail><contact callsign="External marker"/></detail>
</event>
"""
    )

    assert event is not None
    normalized = tak_bridge._normalize_event(event)
    assert normalized is not None
    assert normalized[1]["uid"] == "EFDI-EXTERNAL-MARKER"


def test_tak_ingress_keeps_tak_originated_marker():
    event = tak_bridge._parse_event_xml(
        """\
<event uid="ANDROID-operator-1" type="a-f-G-U-C"
       time="2026-07-28T10:00:00Z" start="2026-07-28T10:00:00Z"
       stale="2026-07-28T10:02:00Z" how="h-e">
  <point lat="54.7" lon="25.2" hae="100" ce="10" le="10"/>
  <detail><contact callsign="Operator 1"/></detail>
</event>
"""
    )

    assert event is not None
    normalized = tak_bridge._normalize_event(event)
    assert normalized is not None
    _topic, record = normalized
    assert record["uid"] == "ANDROID-operator-1"
    assert record["_ingress"] == "tak_server"


def test_sitaware_ingress_keeps_new_data_and_audits_reflections():
    document = b"""\
<nvg xmlns="https://tide.act.nato.int/schemas/2012/10/nvg" version="2.0.2">
  <point uri="urn:efdi:EFDI-SENS-DRONU-48CAB607"
         symbol="2525b:SNGPES----*****" x="25.2" y="54.7"/>
  <point uri="sitaware-unit-1"
         symbol="2525b:SFGPU-----*****" x="25.3" y="54.8"/>
  <polyline uri="route-1">
    <point x="25.4" y="54.9"/>
    <point x="25.5" y="55.0"/>
  </polyline>
</nvg>
"""

    tracks, reflected = nvg_bridge._decode_nvg(document)
    assert reflected == 1
    assert [track["uid"] for track in tracks] == ["sitaware-unit-1"]


def test_sitaware_output_does_not_echo_sitaware_ingress():
    cache = mock.MagicMock()
    sample = mock.MagicMock()
    sample.payload = json.dumps({
        "_src": "sitaware-nvg",
        "_ingress": "sitaware_nvg",
        "uid": "sitaware-unit-1",
        "lat_deg": 54.8,
        "lon_deg": 25.3,
    }).encode()

    sitaware_gateway.nvg_layer.make_handler(
        "SFGPU-----*****", cache, False
    )(sample)

    cache.upsert.assert_not_called()

