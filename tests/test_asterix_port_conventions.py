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
    def test_example_environment_assigns_each_category_port(self):
        text = (ROOT / "compose" / ".env.example").read_text(encoding="utf-8")
        for category, port in EXPECTED.items():
            self.assertRegex(text, rf"(?m)^CAT{category}_PORT={port}\s")

    def test_protocol_defaults_match_the_environment_contract(self):
        for category, port in EXPECTED.items():
            source = (ROOT / "compose" / "protocols" / f"asterix_cat{category}.py").read_text(
                encoding="utf-8"
            )
            self.assertIn(
                f'os.environ.get("CAT{category}_PORT", "{port}")',
                source,
                msg=f"CAT-{category} protocol default drifted",
            )


if __name__ == "__main__":
    unittest.main()
