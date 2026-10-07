import unittest

import pandas as pd

from scripts.analyze_alpha158_ml_fixed_path_repricing_v1 import (
    EXECUTION, reprice_frozen_path, validate_source_fees,
)


class MLFixedPathRepricingTest(unittest.TestCase):

    @staticmethod
    def fixtures():
        curve = pd.DataFrame({
            "date": [1, 2, 3], "capital": [1000.0, 1010.0, 1020.0]})
        fills = pd.DataFrame({
            "date": [2, 3], "side": ["buy", "sell"],
            "status": ["filled", "filled"], "quantity": [100, 100],
            "reference_price": [10.0, 10.2],
            "fill_price_raw": [10.025, 10.1745],
        })
        gross = fills.quantity*fills.fill_price_raw
        fills["commission"] = (gross*EXECUTION["broker_rate"]).clip(
            lower=EXECUTION["min_commission"])
        fills["transfer_fee"] = gross*EXECUTION["transfer_rate"]
        fills["stamp_tax"] = [0.0, gross.iloc[1]*EXECUTION["sell_stamp_rate"]]
        return curve, fills

    def test_source_cost_reproduces_curve_exactly(self):
        curve, fills = self.fixtures()
        audit = validate_source_fees(fills, EXECUTION)
        self.assertLess(max(audit.values()), 1e-12)
        result, metrics = reprice_frozen_path(
            curve, fills, 25.0, 25.0, EXECUTION)
        self.assertTrue(result.repriced_capital.equals(result.source_capital))
        self.assertEqual(metrics["source_reproduction_max_abs_error"], 0.0)

    def test_higher_cost_lowers_same_path_capital(self):
        curve, fills = self.fixtures()
        low, _ = reprice_frozen_path(curve, fills, 25.0, 40.0, EXECUTION)
        high, _ = reprice_frozen_path(curve, fills, 25.0, 60.0, EXECUTION)
        self.assertLess(high.repriced_capital.iloc[-1],
                        low.repriced_capital.iloc[-1])
        self.assertLess(high.cumulative_cash_delta.iloc[-1],
                        low.cumulative_cash_delta.iloc[-1])


if __name__ == "__main__":
    unittest.main()
