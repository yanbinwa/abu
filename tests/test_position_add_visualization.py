"""Position-add report presentation tests."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.visualize_position_add_trades import write_html


class PositionAddVisualizationTest(unittest.TestCase):

    def test_index_exposes_filters_and_complete_operation_reasons(self):
        records = [{
            "tradeNo": 1, "tradeId": "trade-1", "symbol": "sz000001",
            "stockName": "平安银行", "firstDate": 20250102,
            "lastDate": 20250120, "openCount": 1, "addCount": 1,
            "realizedPnl": 123.45, "searchText": "移动止损 protected_winner",
            "image": "charts/trade-1.png",
            "operations": [
                {"no": 1, "date": 20250102, "type": "OPEN",
                 "action": "基础买入", "quantity": 100, "price": 10.0,
                 "reason": "VCP 条件满足", "detail": "组合风险审批通过",
                 "policy": "", "policyVersion": "", "fees": 5.0,
                 "realizedPnl": 0.0},
                {"no": 2, "date": 20250108, "type": "INCREASE",
                 "action": "加仓", "quantity": 100, "price": 10.8,
                 "reason": "保护止损已抬至盈亏平衡线以上",
                 "detail": "信号日：20250107", "policy": "protected_winner",
                 "policyVersion": "protected_winner_v1", "fees": 5.0,
                 "realizedPnl": 0.0},
                {"no": 3, "date": 20250120, "type": "CLOSE",
                 "action": "完全退出", "quantity": 200, "price": 11.0,
                 "reason": "移动止损", "detail": "收盘价触及移动止损",
                 "policy": "", "policyVersion": "", "fees": 6.0,
                 "realizedPnl": 123.45},
            ],
        }]
        with TemporaryDirectory() as directory:
            page = write_html(records, Path(directory), "加仓复盘").read_text(
                encoding="utf-8")
        self.assertIn('id="search"', page)
        self.assertIn('id="action"', page)
        self.assertIn('id="select"', page)
        self.assertIn("protected_winner", page)
        self.assertIn("保护止损已抬至盈亏平衡线以上", page)
        self.assertNotIn('"nan"', page.lower())


if __name__ == "__main__":
    unittest.main()
