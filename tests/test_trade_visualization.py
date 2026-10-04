"""Round-trip attribution tests for the trade chart report."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from abupy.AlphaBu.ABuTradeVisualization import (
    build_round_trips, load_or_rebuild_entry_intents, write_html_index,
)


class TradeVisualizationTest(unittest.TestCase):

    def test_pairs_fills_and_attributes_costs_dividend_and_reason(self):
        fills = pd.DataFrame([
            {"intent_id": "buy-1", "date": 20240103, "symbol": "sz000001",
             "side": "buy", "status": "filled", "quantity": 100,
             "fill_price_raw": 10.0, "commission": 5.0,
             "transfer_fee": 0.1, "stamp_tax": 0.0, "slippage_cost": 2.0,
             "actual_initial_r_cash": 100.0},
            {"intent_id": "sell-1", "date": 20240110, "symbol": "sz000001",
             "side": "sell", "status": "filled", "quantity": 100,
             "fill_price_raw": 11.0, "commission": 5.0,
             "transfer_fee": 0.1, "stamp_tax": 0.55, "slippage_cost": 2.5,
             "actual_initial_r_cash": 0.0},
        ])
        intents = pd.DataFrame([{
            "intent_id": "buy-1", "strategy_id": "vcp_residual_v2",
            "signal_asof": 20240102, "score": 0.8,
            "adjustment_factor_signal": 1.1, "initial_stop_raw": 9.0,
            "metadata": "{'residual_momentum': 0.12, 'breakout_level': 9.5, "
                        "'max_buy_price_raw': 10.2}",
        }])
        exits = pd.DataFrame([{
            "date": 20240109, "symbol": "sz000001", "reason": "TRAILING_STOP",
        }])
        events = pd.DataFrame([{
            "date": 20240108, "symbol": "sz000001",
            "event_type": "CASH_DIVIDEND", "cash_delta": 10.0, "reason": "dividend",
        }])
        trades = build_round_trips(fills, intents, exits, events)
        self.assertEqual(len(trades), 1)
        trade = trades.iloc[0]
        self.assertEqual(trade.exit_reason, "TRAILING_STOP")
        self.assertEqual(trade.exit_reason_cn, "移动止损")
        self.assertEqual(trade.signal_date, 20240102)
        self.assertEqual(trade.sell_signal_date, 20240109)
        self.assertAlmostEqual(trade.net_pnl, 99.25)
        self.assertAlmostEqual(trade.r_multiple, 0.9925)
        self.assertAlmostEqual(trade.cash_dividend, 10.0)

    def test_interactive_index_embeds_trade_selector_and_chart_path(self):
        trades = pd.DataFrame([{
            "trade_no": 1, "symbol": "sz000001", "stock_name": "平安银行",
            "signal_date": 20240102, "buy_date": 20240103,
            "sell_signal_date": 20240109, "sell_date": 20240110,
            "buy_quantity": 100, "sell_quantity": 100,
            "buy_price_raw": 10.0, "sell_price_raw": 11.0,
            "score": 0.8, "residual_momentum": 0.12,
            "initial_stop_raw": 9.0, "max_buy_price_raw": 10.2,
            "exit_reason": "TRAILING_STOP", "exit_reason_cn": "移动止损",
            "cash_dividend": 10.0, "buy_fees": 5.1, "sell_fees": 5.65,
            "slippage_cost": 4.5, "net_pnl": 99.25,
            "return_pct": 9.875, "r_multiple": 0.9925,
        }])
        with TemporaryDirectory() as directory:
            path = write_html_index(trades, Path(directory), "逐笔复盘")
            page = path.read_text(encoding="utf-8")
        self.assertIn('id="tradeSelect"', page)
        self.assertIn('id="reasonFilter"', page)
        self.assertIn('id="strategyFilter"', page)
        self.assertIn("0001_sz000001_20240103_20240110.png", page)
        self.assertIn("平安银行", page)

    def test_rebuilds_alpha158_intent_and_ignores_open_position(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pd.DataFrame([{
                "intent_id": "alpha-buy", "strategy_id":
                "alpha158_lite_low_turnover_v3", "strategy_version": 1,
                "symbol": "sz000001", "side": "buy", "quantity": 100,
                "created_asof": 20240102, "valid_session": 20240103,
                "max_buy_price_raw": 10.5, "initial_stop_adjusted": 8.0,
                "initial_stop_raw": 8.8, "planned_initial_r_per_share_raw": 1.2,
                "planned_initial_r_cash": 120.0, "reason": "",
            }]).to_csv(root / "orders.csv", index=False)
            pd.DataFrame([{
                "signal_asof": 20240102, "symbol": "sz000001",
                "score": 0.25, "daily_rank": 8,
                "risk_decision": "approved", "order_created": True,
            }]).to_csv(root / "selection_decisions.csv", index=False)
            intents = load_or_rebuild_entry_intents(root)
        self.assertEqual(intents.iloc[0].signal_asof, 20240102)
        self.assertAlmostEqual(intents.iloc[0].adjustment_factor_signal, 1.1)
        self.assertIn("daily_rank", intents.iloc[0].metadata)

        fills = pd.DataFrame([{
            "intent_id": "alpha-buy", "date": 20240103,
            "symbol": "sz000001", "side": "buy", "status": "filled",
            "quantity": 100, "fill_price_raw": 10.0, "commission": 5.0,
            "transfer_fee": 0.1, "stamp_tax": 0.0, "slippage_cost": 2.0,
            "actual_initial_r_cash": 100.0,
        }])
        trades = build_round_trips(
            fills, intents,
            pd.DataFrame(columns=["date", "symbol", "reason"]),
            require_all_closed=False)
        self.assertTrue(trades.empty)


if __name__ == "__main__":
    unittest.main()
