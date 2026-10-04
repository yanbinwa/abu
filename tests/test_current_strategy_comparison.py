"""Fair-period and metric invariants for the descriptive strategy comparison."""
import unittest
from pathlib import Path
import numpy as np
import pandas as pd
from scripts.compare_current_strategies_v1 import normalize_curve,metrics,Comparison,SCALES,arm_list,STRESS,cache_frozen_signals
from abupy.AlphaBu.ABuVCPStrategy import VCPStrategy
from tests.test_vcp_strategy import make_vcp_panel
from tests.test_selection_strategies_v2 import make_panel
from abupy.AlphaBu.ABuSelectionStrategiesV2 import run_legacy_v2_backtest


class CurrentComparisonTest(unittest.TestCase):
    def test_memoized_signals_match_and_do_not_share_mutable_metadata(self):
        panel,day=make_vcp_panel()
        original=VCPStrategy(panel).generate_intents(day)
        restore=cache_frozen_signals(panel)
        try:
            strategy=VCPStrategy(panel)
            self.assertEqual(original,strategy.generate_intents(day))
            altered=strategy.generate_intents(day)
            self.assertTrue(altered)
            altered[0].metadata['modified_for_test']=True
            self.assertEqual(original,strategy.generate_intents(day))
        finally:restore()
    def test_cash_anchor_counts_first_day_loss(self):
        curve=pd.DataFrame({'date':[20240103,20250102],'capital':[900000.,990000.],
                            'cash':[900000.,990000.],'stocks':[0.,0.],'exposure':[0.,0.]})
        aligned=normalize_curve(curve,np.array([20240102,20240103,20250102]))
        result=metrics(aligned,pd.DataFrame())
        self.assertAlmostEqual(result['return_pct'],-1)
        self.assertAlmostEqual(result['max_drawdown_pct'],-10)
        with self.assertRaisesRegex(ValueError,'anchor'):
            normalize_curve(curve,np.array([20240102,20240103,20240104,20250102]))

    def test_continuous_legacy_preserves_annual_semantics(self):
        panel=make_panel();dates=panel.dates[panel.dates//10000==2024]
        _,expected,fills,_=run_legacy_v2_backtest(panel,'trend_reversal',2024,mode='b2')
        _,actual,actual_fills,_=run_legacy_v2_backtest(panel,'trend_reversal',None,mode='b2',
            start_date=int(dates[0]),end_date=int(dates[-1]))
        pd.testing.assert_frame_equal(expected,actual)
        pd.testing.assert_frame_equal(fills,actual_fills)
        result,curve,_,_=run_legacy_v2_backtest(panel,'trend_breakout',None,mode='b2',
            start_date=int(panel.dates[250]),end_date=int(panel.dates[300]))
        self.assertEqual(len(curve),51)
        self.assertEqual(result['start'],int(panel.dates[250]))
        with self.assertRaises(ValueError):
            run_legacy_v2_backtest(panel,'trend_breakout',None,start_date=20240101)

    def test_existing_scale_configs_and_declared_arms(self):
        comparison=Comparison.__new__(Comparison)
        comparison.c=Path(__file__).resolve().parents[1]/'configs/selection'
        for name in SCALES:
            config=comparison.scale(name)
            self.assertEqual(config.policy_id,name+'_v1')
        self.assertEqual(len(arm_list()),len(set(arm_list())))
        self.assertFalse(set(STRESS)-set(arm_list()))


if __name__=='__main__':unittest.main()
