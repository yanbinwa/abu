"""Ensure the experiment cannot silently relax unrelated safety constraints."""
import unittest
from dataclasses import asdict
from abupy.AlphaBu.ABuPortfolioRisk import RiskConfig
from scripts.backtest_alpha158_exposure_ablation_v1 import scaled_risk, BUDGET_FIELDS


class ExposureAblationTest(unittest.TestCase):
    def test_scaled_budget_keeps_execution_and_stress_caps_fixed(self):
        original = RiskConfig()
        for multiplier in (1.0, 1.5, 2.0):
            actual = scaled_risk(original, multiplier)
            for key, value in asdict(original).items():
                expected = value * multiplier if key in BUDGET_FIELDS else value
                self.assertEqual(getattr(actual, key), expected)
        self.assertEqual(original.single_trade_risk_fraction, 0.0025)

    def test_unregistered_multiplier_rejected(self):
        with self.assertRaisesRegex(ValueError, 'registered'):
            scaled_risk(RiskConfig(), 1.75)


if __name__ == '__main__':
    unittest.main()
