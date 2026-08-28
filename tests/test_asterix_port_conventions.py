"""Regression checks for the EFDI ASTERIX listener-port convention."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "10": 50010,
    "20": 50020,
    "21": 50021,
    "34": 50034,
    "48": 50048,
    "62": 50062,
}


class AsterixPortConventionTests(unittest.TestCase):
    def test_example_environment_uses_the_combined_generic_ingress(self):
        """This deployment's radar/gateway sends every category combined on
        one UDP dump (UDP_INGRESS_PORT) — .env.example must not pre-configure
        per-category dedicated listener ports as if that were the default."""
        text = (ROOT / "compose" / ".env.example").read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^UDP_INGRESS_PORT=50000\s")
        self.assertRegex(text, r"(?m)^ASTERIX_CATEGORIES=34,48\s")
        for category in EXPECTED:
            self.assertNotRegex(text, rf"(?m)^CAT{category}_PORT=")

    def test_protocol_defaults_match_the_environment_contract(self):
        for category, port in EXPECTED.items():
            source = (ROOT / "compose" / "protocols" / "vendors" / "asterix" / "cat.py").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                f'os.environ.get("CAT{category}_PORT", "{port}")',
                source,
                msg=f"CAT-{category} protocol default drifted",
            )


if __name__ == "__main__":
    unittest.main()
