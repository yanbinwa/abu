import unittest
import numpy as np
import pandas as pd
from types import SimpleNamespace
from scripts.analyze_alpha158_cases_2025_2026 import FEATURES, feature_check, cluster_interval, risk_multiple, terminal_market_value


class AlphaCaseStudyTest(unittest.TestCase):
    def test_terminal_mark_uses_ledger_quantity_and_only_past_prices(self):
        panel=SimpleNamespace(exec_close=np.array([[10.],[np.nan],[100.]]),
                              terminated_mask=np.array([[False],[False],[True]]))
        self.assertEqual(terminal_market_value(panel,1,0,200),2000)
        self.assertEqual(terminal_market_value(panel,2,0,200),0)

    def test_gap_through_stop_does_not_create_infinite_r(self):
        self.assertTrue(np.isnan(risk_multiple(100,0)))
        self.assertEqual(risk_multiple(-100,50),-2)

    def frame(self):
        rows=[]
        for year in (2025,2026):
            for i in range(40):
                value=i/39
                rows.append(dict(status='CLOSED',opened_at=year*10000+101+i//4,
                    closed_at=year*10000+601,net_return_pct=value*10*(1 if year==2025 else -1),
                    forward_20d_pct=value*8,**{f:value for f in FEATURES}))
        return pd.DataFrame(rows)

    def test_later_losses_do_not_flip_development_direction(self):
        frame=self.frame()
        result=feature_check(frame)
        self.assertTrue(result.direction.eq('>=').all())
        self.assertTrue(result.threshold.eq(.5).all())
        self.assertTrue(result.later_delta_pp.lt(0).all())
        # Neither an open winner nor a 2025 trade closed in 2026 can train thresholds.
        extra=frame.iloc[[0,1]].copy()
        extra[list(FEATURES)]=1000
        extra.loc[extra.index[0],'status']='ACTIVE'
        extra.loc[extra.index[1],'closed_at']=20260115
        after=feature_check(pd.concat([frame,extra],ignore_index=True))
        pd.testing.assert_series_equal(result.threshold,after.threshold)
        pd.testing.assert_series_equal(result.direction,after.direction)

    def test_entry_cluster_bootstrap_is_reproducible(self):
        frame=self.frame();frame=frame[frame.opened_at>=20260101]
        first=cluster_interval(frame,FEATURES[0],.5,1,100)
        second=cluster_interval(frame,FEATURES[0],.5,1,100)
        np.testing.assert_array_equal(first,second)
        self.assertLess(first[1],0)


if __name__=='__main__':unittest.main()
