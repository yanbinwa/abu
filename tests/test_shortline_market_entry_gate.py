"""Frozen market-entry gate experiment tests."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

import pandas as pd

from scripts.validate_shortline_market_entry_gate_v1 import (
    paired_path_bootstrap, validate_config,
)


ROOT = Path(__file__).resolve().parents[1]


class ShortLineMarketEntryGateTest(unittest.TestCase):

    def test_registered_config_is_frozen_and_valid(self):
        for name in ("shortline_market_entry_gate_v1.json",
                     "shortline_market_transition_gate_v1.json"):
            config = json.loads((
                ROOT / "configs/selection" / name
            ).read_text(encoding="utf-8"))
            validate_config(config)
            config["gate"]["risk_z63_min"] = 0.9
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_identical_curves_have_zero_paired_effect(self):
        curve = pd.DataFrame({
            "date": [20240101, 20240102, 20240103],
            "capital": [1_010_000.0, 1_000_000.0, 1_020_000.0],
        })
        result = paired_path_bootstrap(
            curve, curve.copy(), block=2, replicates=100, seed=7)
        self.assertEqual(result["return_delta_ci95_low"], 0.0)
        self.assertEqual(result["return_delta_ci95_high"], 0.0)
        self.assertEqual(result["drawdown_improvement_ci95_low"], 0.0)
        self.assertEqual(result["drawdown_improvement_ci95_high"], 0.0)


if __name__ == "__main__":
    unittest.main()
