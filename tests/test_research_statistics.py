"""Causal regimes, block bootstrap and admission-gate tests."""

import tempfile
import unittest

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuResearchStatistics import (
    benjamini_hochberg, block_bootstrap, classify_market_regimes,
    closed_trades_from_fills, cluster_bootstrap_mean, evaluate_admission, top_profit_concentration,
    write_forward_registration,
)


class FakePanel:
    def __init__(self):
        rng = np.random.default_rng(1)
        returns = rng.normal(0.0002, 0.01, 400)
        self.benchmark_returns = returns
        self.benchmark_close = 100 * np.cumprod(1 + returns)
        self.market_ma200 = pd.Series(self.benchmark_close).rolling(
            200, min_periods=200).mean().to_numpy()
        self.dates = pd.bdate_range("2024-01-02", periods=400).strftime(
            "%Y%m%d").astype(int).to_numpy()


class ResearchStatisticsTest(unittest.TestCase):

    def test_regime_threshold_does_not_use_future(self):
        first = FakePanel(); second = FakePanel()
        second.benchmark_returns[351:] *= 20
        second.benchmark_close[351:] *= 2
        a = classify_market_regimes(first)
        b = classify_market_regimes(second)
        pd.testing.assert_frame_equal(a.iloc[:351], b.iloc[:351])
        self.assertEqual(a.market_state.iloc[250], "UNKNOWN")
        self.assertNotEqual(a.market_state.iloc[300], "UNKNOWN")

    def test_block_bootstrap_is_reproducible_and_reports_censoring(self):
        rng = np.random.default_rng(3)
        returns = rng.normal(0.0001, 0.01, 120)
        one = block_bootstrap(returns, paths=30, seed=9,
                              methods=("circular_5", "stationary_10"))
        two = block_bootstrap(returns, paths=30, seed=9,
                              methods=("circular_5", "stationary_10"))
        pd.testing.assert_frame_equal(one[0], two[0])
        pd.testing.assert_frame_equal(one[1], two[1])
        self.assertEqual(len(one[1]), 60)
        self.assertIn("right_censored_fraction", one[0])
        self.assertTrue(all("interpretation" in row for row in
                            one[0].to_dict("records")))
        self.assertTrue(all(not table.empty for table in one[2].values()))

    def test_cluster_ci_bh_and_profit_concentration(self):
        ci = cluster_bootstrap_mean(
            [1, 2, -1, 3, 1, 2], [1, 1, 2, 3, 3, 4], samples=100, seed=4)
        self.assertEqual(ci["clusters"], 4)
        self.assertLessEqual(ci["lower"], ci["mean"])
        q = benjamini_hochberg([0.01, 0.04, 0.03, 0.20])
        np.testing.assert_allclose(q, [0.04, 0.0533333333, 0.0533333333, 0.2])
        self.assertAlmostEqual(top_profit_concentration([10, 5, 1], top=1), 10/16)

    def test_admission_defaults_to_research_when_evidence_is_missing(self):
        decision = evaluate_admission(
            closed_trades=20, entry_clusters=10, known_states=2,
            cluster_ci_lower=-0.1, placebo_percentile=0.80,
            annual_blocks_above_median=1, top5_contribution=0.8,
            q_value=0.2, liquidation_drawdown_ok=False,
        )
        self.assertEqual(decision.status, "research_only")
        self.assertIn("INSUFFICIENT_SAMPLE", decision.reasons)

    def test_forward_registration_is_frozen_and_hashed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = directory + "/forward.json"
            payload = write_forward_registration(
                path, "vcp_core_v1", {"core": "abc"}, "python daily.py",
                ["PIT bars"], "halt and report")
            self.assertEqual(payload["minimum_calendar_months"], 6)
            self.assertEqual(len(payload["registration_sha256"]), 64)

    def test_fills_pair_into_cost_after_closed_trades(self):
        fills = pd.DataFrame([
            {"order_id": "a", "date": 20250102, "symbol": "sz000001",
             "side": "buy", "status": "filled", "quantity": 100,
             "fill_price_raw": 10.0, "commission": 5.0, "transfer_fee": 0.1,
             "stamp_tax": 0.0, "actual_initial_r_cash": 100.0},
            {"order_id": "b", "date": 20250110, "symbol": "sz000001",
             "side": "sell", "status": "filled", "quantity": 100,
             "fill_price_raw": 11.0, "commission": 5.0, "transfer_fee": 0.11,
             "stamp_tax": 0.55, "actual_initial_r_cash": 0.0},
        ])
        closed = closed_trades_from_fills(fills)
        self.assertEqual(len(closed), 1)
        self.assertAlmostEqual(closed.iloc[0].profit, 89.24)
        self.assertAlmostEqual(closed.iloc[0].r_multiple, 0.8924)


if __name__ == "__main__":
    unittest.main()
