"""Accounting, paired inference and no-lookahead guards for the challenge."""
from dataclasses import dataclass, asdict
import unittest

import numpy as np
import pandas as pd

from scripts.validate_alpha158_annual_uplift_v1 import (
    curve_metrics, paired_cagr_interval, scaled_risk, validate_folds,
)


@dataclass(frozen=True)
class Budget:
    single_trade_risk_fraction: float = .0025
    portfolio_open_risk_fraction: float = .02
    industry_open_risk_fraction: float = .006
    same_day_new_risk_fraction: float = .0075
    max_symbol_weight: float = .08
    max_gross_exposure: float = .8
    max_amount_participation: float = .05
    max_stress_loss_fraction: float = .06


class AnnualUpliftTests(unittest.TestCase):
    def test_sizing_does_not_relax_hard_caps_or_mutate_baseline(self):
        risk = Budget()
        original = asdict(risk)
        scaled = scaled_risk(risk, 2.)
        self.assertEqual(asdict(risk), original)
        self.assertEqual(scaled.single_trade_risk_fraction, .005)
        for field in ('max_symbol_weight', 'max_gross_exposure',
                      'max_amount_participation', 'max_stress_loss_fraction'):
            self.assertEqual(getattr(scaled, field), original[field])
        with self.assertRaises(ValueError):
            scaled_risk(risk, 1.75)

    def test_cagr_uses_elapsed_calendar_time_and_reconciles_cash(self):
        frame = pd.DataFrame(dict(date=[20230101, 20240101, 20250101],
                                  capital=[1e6, 1.1e6, 1.21e6],
                                  cash=[1e6, 1.1e6, 1.21e6], stocks=[0, 0, 0],
                                  exposure=[0., 0., 0.]))
        metrics = curve_metrics(frame)
        self.assertAlmostEqual(metrics['cagr_pct'], (1.21 ** (365.25 / 731) - 1) * 100)
        self.assertAlmostEqual(metrics['return_pct'], 21.)
        frame.loc[1, 'cash'] += 10
        with self.assertRaises(ValueError):
            curve_metrics(frame)

    def test_identical_paths_have_zero_paired_uncertainty(self):
        nav = 1e6 * np.exp(np.cumsum(np.r_[0., np.linspace(-.01, .012, 100)]))
        result = paired_cagr_interval(nav, nav, 1., replicates=100)
        self.assertEqual(result['ci95_low_pp'], 0.)
        self.assertEqual(result['ci95_high_pp'], 0.)

    def test_constant_growth_has_known_annualized_delta(self):
        nav = 1e6 * np.exp(np.arange(101) * np.log(1.04) / 100)
        result = paired_cagr_interval(np.repeat(1e6, 101), nav, 2., replicates=100)
        expected = (1.04 ** .5 - 1) * 100
        self.assertAlmostEqual(result['ci95_low_pp'], expected)
        self.assertAlmostEqual(result['ci95_high_pp'], expected)

    def test_label_boundaries_and_row_provenance_fail_closed(self):
        fold = dict(fold=0, train_end=20230101, train_label_end=20230201,
                    validation_start=20230202, validation_label_end=20230301,
                    test_start=20230302, test_end=20230303)
        rows = pd.DataFrame(dict(fold=[0], train_end=[20230101],
                                 signal_asof=[20230302], symbol=['sz000001']))
        validate_folds([fold], rows)
        for bad in (dict(fold, validation_label_end=20230302),
                    dict(fold, train_label_end=20230202)):
            with self.assertRaises(ValueError):
                validate_folds([bad], rows)
        with self.assertRaises(ValueError):
            validate_folds([fold], pd.concat([rows, rows]))
        rows.loc[0, 'train_end'] = 20230303
        with self.assertRaises(ValueError):
            validate_folds([fold], rows)


if __name__ == '__main__':
    unittest.main()
