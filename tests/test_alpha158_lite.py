"""Alpha158-lite PIT feature, label, model and exit tests."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuAlpha158Lite import (
    ALPHA158_LITE_DIAGNOSTIC_HORIZONS, ALPHA158_LITE_FEATURES,
    Alpha158LiteConfig, Alpha158LiteExitEngine, Alpha158LiteFeatureEngine,
    Alpha158LiteLowTurnoverPolicy, Alpha158LiteModel,
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
    load_alpha158_lite_turnover_config,
)
from tests.test_vcp_strategy import make_vcp_panel
from scripts.research_alpha158_lite_v1 import (
    daily_rank_ic, daily_selection_uplift, factor_diagnostics,
    moving_block_mean_interval,
)
from scripts.backtest_alpha158_lite_v1 import (
    dropout_rank_exits, exit_reason, fixed_path_cost_attribution, rank_frame,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import annual_returns


def config(**updates):
    values = {
        "minimum_history_sessions": 120,
        "minimum_median_amount_20d": 1.0,
    }
    values.update(updates)
    return Alpha158LiteConfig(**values)


class Alpha158LiteTest(unittest.TestCase):

    def test_feature_snapshot_is_future_invariant(self):
        panel, day = make_vcp_panel()
        engine = Alpha158LiteFeatureEngine(panel, config())
        first = engine.snapshot(day, include_labels=False)
        panel.open[day+1:] *= 7
        panel.high[day+1:] *= 7
        panel.low[day+1:] *= 7
        panel.close[day+1:] *= 7
        panel.amount[day+1:] *= 11
        second = engine.snapshot(day, include_labels=False)
        pd.testing.assert_frame_equal(first, second)
        self.assertEqual(tuple(first.columns[5:5+len(ALPHA158_LITE_FEATURES)]),
                         ALPHA158_LITE_FEATURES)

    def test_label_starts_at_next_open_and_rejects_large_gap(self):
        panel, day = make_vcp_panel()
        engine = Alpha158LiteFeatureEngine(panel, config(max_gap_atr=1.0))
        panel.open[day+1] = 10.0
        panel.exec_open[day+1] = 10.0
        panel.close[day+20] = 11.0
        panel.benchmark_open[day+1] = 100.0
        panel.benchmark_close[day+20] = 100.0
        labeled = engine.snapshot(day, include_labels=True)
        self.assertTrue(np.allclose(labeled.excess_return_20d, .1))
        panel.exec_open[day+1, 0] = 99.0
        rejected = engine.snapshot(day, include_labels=True)
        first = rejected[rejected.symbol == "sz000001"].iloc[0]
        self.assertTrue(np.isnan(first.excess_return_20d))

    def test_label_rejects_limit_up_and_slippage_beyond_max_buy(self):
        panel, day = make_vcp_panel()
        limit_engine = Alpha158LiteFeatureEngine(
            panel, config(max_gap_atr=100.0))
        panel.exec_open[day+1, 0] = 11.55
        panel.open[day+1, 0] = 11.55
        at_limit = limit_engine.snapshot(day, include_labels=True)
        row = at_limit[at_limit.symbol == "sz000001"].iloc[0]
        self.assertTrue(np.isnan(row.excess_return_20d))

        panel, day = make_vcp_panel()
        slip_engine = Alpha158LiteFeatureEngine(
            panel, config(max_gap_atr=1.0, label_slippage_bps=25.0))
        panel.exec_open[day+1, 0] = 10.699
        panel.open[day+1, 0] = 10.699
        slipped = slip_engine.snapshot(day, include_labels=True)
        row = slipped[slipped.symbol == "sz000001"].iloc[0]
        self.assertTrue(np.isnan(row.excess_return_20d))

    def test_intent_freezes_two_atr_stop_without_future_read(self):
        panel, day = make_vcp_panel()
        engine = Alpha158LiteFeatureEngine(panel, config())
        intent = engine.make_intent(day, 0, .25)
        self.assertAlmostEqual(
            intent.initial_stop_adjusted,
            panel.close[day, 0]-2*panel.atr21[day, 0])
        self.assertEqual(intent.signal_asof, int(panel.dates[day]))
        self.assertEqual(intent.metadata["rank_exit_buffer"], 20)

    def test_ridge_uses_custom_features_and_predicts(self):
        rng = np.random.default_rng(19)
        rows = 1200
        frame = pd.DataFrame({
            name: rng.normal(size=rows) for name in ALPHA158_LITE_FEATURES
        })
        frame["target_rank"] = .4*frame.return_20d-.2*frame.realized_vol_20d
        dates = np.repeat(np.arange(20200101, 20200113), 100)
        frame["signal_asof"] = dates
        model = Alpha158LiteModel(config()).fit(frame, dates)
        score = model.predict(frame.iloc[:10])
        self.assertEqual(len(score), 10)
        self.assertTrue(np.isfinite(score).all())
        self.assertEqual(
            model.manifest["sample_weighting"],
            "equal_total_weight_per_signal_date")

    def test_event_exit_has_no_fixed_holding_exit(self):
        panel, day = make_vcp_panel()
        engine = Alpha158LiteFeatureEngine(panel, config())
        intent = engine.make_intent(day, 0, .5)
        exits = Alpha158LiteExitEngine(panel, config())
        exits.register_entry(
            intent, SimpleNamespace(fill_price_raw=intent.signal_price_raw), day)
        panel.close[day+1, 0] = intent.initial_stop_adjusted-.01
        self.assertEqual(exits.signal(day+1, intent.symbol), "INITIAL_STOP")

    def test_config_loader_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"bad.json"
            path.write_text('{"unknown": 1}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_alpha158_lite_config(path)

    def test_research_metrics_are_same_day_cross_sectional(self):
        rows = []
        for date in (20250102, 20250103):
            for value in range(25):
                rows.append({
                    "signal_asof": date, "alpha_score": value,
                    "excess_return_20d": value/100,
                })
        frame = pd.DataFrame(rows)
        ic = daily_rank_ic(frame, "alpha_score")
        uplift = daily_selection_uplift(frame, "alpha_score", topk=10)
        self.assertTrue((ic.ic == 1).all())
        self.assertTrue((uplift.uplift > 0).all())

    def test_factor_diagnostics_and_block_interval_are_deterministic(self):
        rows = []
        for date in (20250102, 20250103, 20250106):
            for value in range(25):
                rows.append({
                    "signal_asof": date, "return_20d": value,
                    "excess_return_20d": value/100,
                })
        daily, summary = factor_diagnostics(
            pd.DataFrame(rows), features=("return_20d",), horizons=(20,))
        self.assertTrue((daily.rank_ic == 1).all())
        self.assertGreater(summary.iloc[0].mean_top_bottom_spread, 0)
        first = moving_block_mean_interval(daily.rank_ic, replicates=100)
        second = moving_block_mean_interval(daily.rank_ic, replicates=100)
        self.assertEqual(first, second)
        self.assertEqual(first, (1.0, 1.0))

    def test_diagnostic_horizons_keep_training_horizon(self):
        self.assertIn(config().label_horizon_sessions,
                      ALPHA158_LITE_DIAGNOSTIC_HORIZONS)

    def test_topk_buffer_keeps_only_frozen_depth(self):
        frame = pd.DataFrame({
            "signal_asof": [20250102]*4,
            "symbol": ["d", "a", "c", "b"],
            "column": [3, 0, 2, 1],
            "alpha_score": [1., 4., 2., 3.],
        })
        ranked = rank_frame(frame, "alpha_score", depth=3)
        self.assertEqual(ranked.symbol.tolist(), ["a", "b", "c"])
        self.assertEqual(ranked.daily_rank.tolist(), [1, 2, 3])
        self.assertIsNone(exit_reason(None, "b", {"a", "b"}))
        self.assertEqual(exit_reason(None, "c", {"a", "b"}), "RANK_EXIT")
        self.assertEqual(exit_reason("INITIAL_STOP", "b", {"a", "b"}),
                         "INITIAL_STOP")

    def test_fixed_path_cost_attribution_keeps_trade_path(self):
        fills = pd.DataFrame({
            "status": ["filled", "filled"], "quantity": [100, 100],
            "reference_price": [10., 11.], "commission": [5., 5.],
            "transfer_fee": [1., 1.], "stamp_tax": [0., 5.],
            "slippage_cost": [2.5, 2.5],
        })
        result = fixed_path_cost_attribution(fills, -1., 1000.)
        self.assertAlmostEqual(result["total_friction_pct_initial"], 2.2)
        self.assertAlmostEqual(result["fixed_path_reference_return_pct"], 1.2)
        self.assertAlmostEqual(result["round_trip_turnover_multiple"], 1.05)

    def test_dropout_replaces_only_worst_holding_with_better_candidate(self):
        daily = pd.DataFrame({
            "symbol": ["new", "held_good", "held_bad"],
            "daily_rank": [1, 2, 30],
        })
        result = dropout_rank_exits(
            {"held_good", "held_bad"}, daily, maximum=1)
        self.assertEqual(result, ["held_bad"])
        blocked = dropout_rank_exits(
            {"held_good", "held_bad"}, daily, maximum=1,
            blocked={"held_bad"})
        self.assertEqual(blocked, ["held_good"])

    def test_turnover_config_matches_frozen_source_hash(self):
        root = Path(__file__).resolve().parents[1]
        source = load_alpha158_lite_config(
            root/"configs/selection/alpha158_lite_v1.json")
        turnover = load_alpha158_lite_turnover_config(
            root/"configs/selection/alpha158_lite_turnover_v2.json")
        self.assertEqual(turnover.source_config_sha256, source.sha256)
        self.assertEqual(turnover.max_rank_replacements_per_day, 1)

    def test_low_turnover_policy_requires_persistent_entry_and_exit(self):
        root = Path(__file__).resolve().parents[1]
        cfg = load_alpha158_lite_low_turnover_config(
            root/"configs/selection/alpha158_lite_low_turnover_v3.json")
        policy = Alpha158LiteLowTurnoverPolicy(cfg)
        daily = pd.DataFrame({
            "symbol": ["new", "held"], "daily_rank": [1, 101]})
        exits, entries = policy.review(
            daily, {"held"}, {"held": 12})
        self.assertEqual(exits, [])
        self.assertEqual(entries, [])
        exits, entries = policy.review(
            daily, {"held"}, {"held": 17})
        self.assertEqual(exits, ["held"])
        self.assertEqual(entries, ["new"])

    def test_low_turnover_policy_honors_minimum_holding(self):
        root = Path(__file__).resolve().parents[1]
        cfg = load_alpha158_lite_low_turnover_config(
            root/"configs/selection/alpha158_lite_low_turnover_v3.json")
        policy = Alpha158LiteLowTurnoverPolicy(cfg)
        daily = pd.DataFrame({
            "symbol": ["new", "held"], "daily_rank": [1, 101]})
        policy.review(daily, {"held"}, {"held": 2})
        exits, _ = policy.review(daily, {"held"}, {"held": 7})
        self.assertEqual(exits, [])

    def test_low_turnover_annual_returns_chain_year_end_capital(self):
        curve = pd.DataFrame({
            "date": [20231229, 20240102, 20241231],
            "capital": [1_100_000., 1_100_000., 1_210_000.],
        })
        result = annual_returns(curve)
        self.assertEqual(result.year.tolist(), [2023, 2024])
        self.assertTrue(np.allclose(result.return_pct, [10., 10.]))


if __name__ == "__main__":
    unittest.main()
