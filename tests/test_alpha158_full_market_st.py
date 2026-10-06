import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validate_alpha158_full_market_st_v1 import (
    stable_json, validate_st_source,
)


class Alpha158FullMarketSTTest(unittest.TestCase):
    def test_stable_json_ignores_mapping_order(self):
        self.assertEqual(stable_json({"b": 1, "a": 2}),
                         stable_json({"a": 2, "b": 1}))

    def test_validate_st_source_requires_both_exchanges(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            panel = root / "panel.npz"
            panel.write_bytes(b"panel")
            audit = root / "audit.json"
            audit.write_text(json.dumps({
                "gate_status": "PASS_ST_EXCLUSION_DATA_GATE",
                "exact_st_coverage": 1.0,
                "exchange_coverage": {
                    "sh": {"coverage": 1.0},
                    "sz": {"coverage": .99},
                },
            }))
            import hashlib
            config = {
                "st_panel": str(panel), "st_audit": str(audit),
                "st_panel_sha256": hashlib.sha256(b"panel").hexdigest(),
                "st_audit_sha256": hashlib.sha256(
                    audit.read_bytes()).hexdigest(),
            }
            with self.assertRaises(ValueError):
                validate_st_source(config)


if __name__ == "__main__":
    unittest.main()
