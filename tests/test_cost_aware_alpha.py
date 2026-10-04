"""Causal calibration, cost hurdles and protective-exit isolation."""
import unittest
from dataclasses import replace
from types import SimpleNamespace
import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuCostAwareAlpha import (
    CalibrationConfig, PastScoreCalibration, CostAwareReview,
    switching_cost_bps, hac_mean_se,
)
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig
from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval


def fixture():
    dates = pd.bdate_range('2020-01-01', periods=80).strftime('%Y%m%d').astype(int).to_numpy()
    rows = []
    for i, date in enumerate(dates[:60]):
        for j in range(20):
            rows.append(dict(signal_asof=date, symbol=f's{j}', column=j,
                train_end=20190101, alpha_score=j,
                excess_return_20d=j*.003+(i%3)*.0001))
    config = CalibrationConfig(minimum_calibration_dates=3, maximum_calibration_dates=10)
    return dates, pd.DataFrame(rows), config


class CostAwareAlphaTest(unittest.TestCase):
    def test_future_labels_and_scores_cannot_change_past_decision(self):
        dates, frame, config = fixture()
        first = PastScoreCalibration(frame, dates, config).edge(dates[35], 's19', 's1')
        future = frame.signal_asof >= dates[15]  # label ends >= decision date
        frame.loc[future, 'excess_return_20d'] = 999
        frame.loc[frame.signal_asof > dates[35], 'alpha_score'] *= -999
        second = PastScoreCalibration(frame, dates, config).edge(dates[35], 's19', 's1')
        self.assertEqual(first, second)
        self.assertLess(first['latest_label_end'], dates[35])

    def test_exact_maturity_boundary_is_excluded(self):
        dates, frame, config = fixture()
        calibration = PastScoreCalibration(frame, dates, config)
        self.assertEqual(len(calibration.window(dates[20])), 0)
        self.assertEqual(len(calibration.window(dates[21])), 1)
        self.assertEqual(calibration.first_ready_date(), dates[23])
        self.assertEqual(len(calibration.window(dates[50])), 10)

    def test_same_bucket_has_zero_edge_and_missing_score_fails_closed(self):
        dates, frame, config = fixture()
        calibration = PastScoreCalibration(frame, dates, config)
        self.assertEqual(calibration.edge(dates[35], 's1', 's2')['lower_edge_bps'], 0)
        self.assertEqual(calibration.edge(dates[35], 'absent', 's2')['status'], 'MISSING_CURRENT_SCORE')
        self.assertEqual(calibration.edge(dates[20], 's19', 's2')['status'], 'INSUFFICIENT_CALIBRATION')

    def test_in_sample_or_duplicate_inputs_are_rejected(self):
        dates, frame, config = fixture()
        with self.assertRaises(ValueError):
            PastScoreCalibration(pd.concat([frame, frame.iloc[:1]]), dates, config)
        frame.loc[0, 'train_end'] = frame.loc[0, 'signal_asof']
        with self.assertRaises(ValueError):
            PastScoreCalibration(frame, dates, config)

    def test_date_weighting_not_stock_row_weighting(self):
        dates, frame, config = fixture()
        original = PastScoreCalibration(frame, dates, config)
        copies = frame[frame.signal_asof == dates[10]].copy()
        copies['symbol'] += '_copy'
        duplicated_day = PastScoreCalibration(pd.concat([frame, copies]), dates, config)
        # Compare a lower bin whose membership is invariant to duplicating ties.
        self.assertAlmostEqual(original.daily.loc[dates[10], 0], duplicated_day.daily.loc[dates[10], 0])

    def test_fee_hurdle_includes_minimum_tax_and_both_slippage_legs(self):
        config = ExecutionConfig()
        cost = switching_cost_bps(1000, 10000, config)
        # 50 bp slippage + 50 bp buy minimum + 5 bp sell minimum + tax/transfer.
        self.assertGreater(cost, 110)
        self.assertLess(cost, 111)
        self.assertGreater(switching_cost_bps(1000, 10000, replace(config, slippage_bps=60)), cost)
        self.assertTrue(np.isnan(switching_cost_bps(0, 10000, config)))

    def test_hac_handles_overlapping_serial_observations(self):
        smooth = np.repeat([-1., 1.], 50)
        self.assertGreater(hac_mean_se(smooth, 19), hac_mean_se(smooth, 0))

    def test_paired_bootstrap_preserves_identical_paths(self):
        nav = np.cumprod(1+np.tile([.01, -.009, .003], 30))
        result = paired_block_interval(nav, nav, replicates=100)
        self.assertEqual(result['ci95_low_pp'], 0.)
        self.assertEqual(result['ci95_high_pp'], 0.)
        self.assertEqual(result['resampled_nonpositive_fraction'], 1.)
        positive = paired_block_interval(nav, nav*np.exp(np.arange(len(nav))*.001), replicates=100)
        self.assertGreater(positive['ci95_low_pp'], 0.)

    def test_overlay_preserves_non_rank_decisions_and_filters_fallbacks(self):
        class Calibration:
            config = CalibrationConfig()
            def edge(self, asof, candidate, holding):
                return dict(status='READY', lower_edge_bps=200 if candidate == 'good' else 0)
        panel = SimpleNamespace(dates=np.array([20240101]),
            symbol_index={'held':0,'good':1,'bad':2}, exec_close=np.array([[10.,10.,10.]]))
        executor = SimpleNamespace(config=ExecutionConfig(),
            positions={'held':SimpleNamespace(quantity=1000)})
        overlay = CostAwareReview(Calibration())
        self.assertEqual(overlay.filter_review(panel, executor, 0, [], ['bad']), ([], ['bad']))
        self.assertEqual(overlay.filter_review(panel, executor, 0, ['held'], ['bad','good']), (['held'], ['good']))
        self.assertEqual(overlay.filter_review(panel, executor, 0, ['held'], ['bad']), ([], ['bad']))
        disabled = CostAwareReview(suppress_rank_exits=True)
        self.assertEqual(disabled.filter_review(panel, executor, 0, ['held'], ['good']), ([], ['good']))


if __name__ == '__main__':
    unittest.main()
