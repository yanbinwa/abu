"""Enterprise WeChat paper-fill message tests."""
from __future__ import annotations

import unittest

import pandas as pd

from scripts.notify_wecom_paper_trades import build_trade_message


class WeComPaperNotificationTest(unittest.TestCase):

    def test_message_contains_filled_trade_reason_and_account(self):
        fills = pd.DataFrame([
            {"order_id": "b1", "date": 20261009, "symbol": "sz000001",
             "side": "buy", "status": "filled", "quantity": 1000,
             "fill_price_raw": 10.025, "commission": 5.0,
             "transfer_fee": 0.1, "stamp_tax": 0.0, "slippage_cost": 25.0},
            {"order_id": "s1", "date": 20261020, "symbol": "sz000001",
             "side": "sell", "status": "filled", "quantity": 1000,
             "fill_price_raw": 11.0, "commission": 5.0,
             "transfer_fee": 0.1, "stamp_tax": 5.5, "slippage_cost": 27.5},
        ])
        exits = pd.DataFrame([{
            "date": 20261019, "symbol": "sz000001", "reason": "TRAILING_STOP",
        }])
        summary = {"capital": 1_010_000, "return_pct": 1.0,
                   "exposure_pct": 0.0, "positions": 0}
        message = build_trade_message(
            fills, summary, {"sz000001": "平安银行"}, exits)
        self.assertIn("买入 sz000001 平安银行", message)
        self.assertIn("卖出 sz000001 平安银行", message)
        self.assertIn("移动止损", message)
        self.assertIn("账户资产：¥1,010,000.00", message)
        self.assertIn("研究模拟盘", message)


if __name__ == "__main__":
    unittest.main()
