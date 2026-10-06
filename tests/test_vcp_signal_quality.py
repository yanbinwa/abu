import unittest

import numpy as np
import pandas as pd

from scripts.analyze_vcp_signal_quality import (
    bh_adjust, build_closed_trades, factor_ic_table, factor_quintiles,
    within_date_ic,
)


class VCPSignalQualityTest(unittest.TestCase):

    def test_factor_diagnostics_detect_monotonic_signal(self):
        rows = []
        for day in (20230101, 20230102, 20230103):
            for value in range(1, 11):
                rows.append({
                    "intent_id": "{}-{}".format(day, value),
                    "signal_asof": day,
                    "score": value,
                    "residual_momentum": value,
                    "ma120_slope": value,
                    "contraction_tightness": value,
                    "breakout_strength": value,
                    "return_5d": value / 100,
                    "return_20d": value / 50,
                    "return_40d": value / 25,
                    "hit_plus_1r_20d": float(value >= 6),
                    "hit_initial_stop_20d": float(value <= 5),
                })
        frame = pd.DataFrame(rows)
        values = within_date_ic(frame, "score", "return_20d")
        self.assertEqual(len(values), 3)
        self.assertTrue(np.allclose(values.ic, 1))
        table = factor_ic_table(
            frame, bootstrap_paths=100, permutation_paths=100, seed=7)
        primary = table.query(
            "factor == 'score' and outcome == 'return_20d'").iloc[0]
        self.assertEqual(primary.mean_spearman_ic, 1)
        quintiles = factor_quintiles(frame)
        score = quintiles[quintiles.factor.eq("score")].set_index("quintile")
        self.assertGreater(score.loc[5, "mean_return_20d"],
                           score.loc[1, "mean_return_20d"])

    def test_bh_adjust_and_closed_trade_pairing(self):
        adjusted = bh_adjust([0.01, 0.03, 0.2])
        self.assertTrue(np.allclose(adjusted, [0.03, 0.045, 0.2]))
        fills = pd.DataFrame([
            {"intent_id": "buy-a", "date": 20230102, "symbol": "sz000001",
             "side": "buy", "status": "filled", "quantity": 100,
             "fill_price_raw": 10.0, "commission": 5.0,
             "transfer_fee": 0.1, "stamp_tax": 0.0},
            {"intent_id": "sell-a", "date": 20230110, "symbol": "sz000001",
             "side": "sell", "status": "filled", "quantity": 100,
             "fill_price_raw": 11.0, "commission": 5.0,
             "transfer_fee": 0.1, "stamp_tax": 1.1},
        ])
        exits = pd.DataFrame([
            {"date": 20230109, "symbol": "sz000001",
             "reason": "TRAILING_STOP"},
        ])
        trades = build_closed_trades(fills, exits)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades.iloc[0].intent_id, "buy-a")
        self.assertEqual(trades.iloc[0].exit_reason, "TRAILING_STOP")
        self.assertGreater(trades.iloc[0].pnl_ex_dividend, 0)


if __name__ == "__main__":
    unittest.main()
