import io
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "compose" / "zenoh-admin"))
os.environ.setdefault("ZENOH_ADMIN_DB_USER", "test")
os.environ.setdefault("ZENOH_ADMIN_DB_PASSWORD", "test")
os.environ.setdefault("ZENOH_ADMIN_SECRET_KEY", "test-secret")

from api import streams  # noqa: E402

YML = (ROOT / "compose" / "bridges" / "vendors" / "mediamtx" / "mediamtx.yml").read_text()


def _doc():
    return streams._yaml().load(io.StringIO(YML))


def _dump(doc):
    out = io.StringIO()
    streams._yaml().dump(doc, out)
    return out.getvalue()


class SrtPassphraseTest(unittest.TestCase):
    def test_default_config_has_no_passphrase(self):
        self.assertFalse(streams._settings_from_doc(_doc()).srt_publish_passphrase_set)

    def test_set_then_read_never_echoes_secret(self):
        doc = _doc()
        settings = streams._settings_from_doc(doc).model_copy(update={"srt_publish_passphrase": "abcdefghij1234567890"})
        streams._apply_settings_to_doc(doc, settings)
        self.assertEqual(doc["paths"]["all_others"]["srtPublishPassphrase"], "abcdefghij1234567890")
        back = streams._settings_from_doc(doc)
        self.assertTrue(back.srt_publish_passphrase_set)
        self.assertIsNone(back.srt_publish_passphrase)

    def test_none_keeps_and_empty_clears(self):
        doc = _doc()
        doc["paths"]["all_others"]["srtPublishPassphrase"] = "abcdefghij1234567890"
        keep = streams._settings_from_doc(doc)
        streams._apply_settings_to_doc(doc, keep)
        self.assertIn("srtPublishPassphrase", doc["paths"]["all_others"])
        streams._apply_settings_to_doc(doc, keep.model_copy(update={"srt_publish_passphrase": ""}))
        self.assertNotIn("srtPublishPassphrase", doc["paths"]["all_others"])

    def test_numeric_passphrase_survives_yaml_roundtrip_as_string(self):
        doc = _doc()
        settings = streams._settings_from_doc(doc).model_copy(update={"srt_publish_passphrase": "1234567890123"})
        streams._apply_settings_to_doc(doc, settings)
        reloaded = streams._yaml().load(io.StringIO(_dump(doc)))
        self.assertEqual(reloaded["paths"]["all_others"]["srtPublishPassphrase"], "1234567890123")
        self.assertIsInstance(reloaded["paths"]["all_others"]["srtPublishPassphrase"], str)

    def test_passphrase_pattern(self):
        ok = streams._SRT_PASSPHRASE_RE.fullmatch
        self.assertTrue(ok("a" * 10) and ok("a" * 79))
        self.assertFalse(ok("a" * 9) or ok("a" * 80) or ok("has space 123456"))


if __name__ == "__main__":
    unittest.main()
