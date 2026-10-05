"""Selective conviction sizing tests."""
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from abupy.AlphaBu.ABuAlpha158Lite import Alpha158LiteFeatureEngine
from abupy.AlphaBu.ABuConvictionSizing import (
    ConvictionSizingConfig, ConvictionSizingPolicy,
    load_conviction_sizing_config,
)
from tests.test_alpha158_lite import config
from tests.test_vcp_strategy import make_vcp_panel


class ConvictionSizingTest(unittest.TestCase):

    def test_config_loader_is_strict(self):
        root = Path(__file__).resolve().parents[1]
        loaded = load_conviction_sizing_config(
            root / "configs/selection/alpha158_conviction_sizing_v1.json")
        self.assertEqual(loaded.market_volatility_window, 60)
        payload = json.loads(json.dumps(loaded.__dict__))
        payload["unknown"] = True
        with tempfile.NamedTemporaryFile("w", suffix=".json") as output:
            json.dump(payload, output); output.flush()
            with self.assertRaises(ValueError):
                load_conviction_sizing_config(output.name)

    def test_evaluation_is_future_invariant_and_fails_closed(self):
        panel, day = make_vcp_panel()
        engine = Alpha158LiteFeatureEngine(panel, config())
        policy = ConvictionSizingPolicy(
            panel, engine, ConvictionSizingConfig(
                minimum_market_volatility=1e-9,
                maximum_industry_breadth_rank=0.5))
        first = policy.evaluate(day, "sz000001")
        panel.close[day+1:] *= 9
        panel.benchmark_close[day+1:] *= 7
        second = policy.evaluate(day, "sz000001")
        self.assertEqual(first["selected"], second["selected"])
        self.assertEqual(first["risk_fraction"], second["risk_fraction"])
        self.assertTrue(np.isfinite(first["market_volatility_60d"]))

        panel.industry[:, 1] = -1
        missing_policy = ConvictionSizingPolicy(
            panel, engine, policy.config)
        missing = missing_policy.evaluate(day, "sz000002")
        self.assertFalse(missing["selected"])
        self.assertEqual(missing["risk_fraction"],
                         missing_policy.config.normal_risk_fraction)
