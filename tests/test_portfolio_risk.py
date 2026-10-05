"""Portfolio risk sizing, stress and shadow-mode tests."""

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor
from abupy.AlphaBu.ABuPortfolioRisk import (
    PortfolioRiskEngine, RiskConfig, load_risk_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuSelectionStrategies import SelectionPanel
from abupy.AlphaBu.ABuTradeIntent import TradeIntent


def make_panel(amount=10_000_000, flat_market=False):
    dates = pd.bdate_range("2024-01-02", periods=140).strftime(
        "%Y%m%d").astype(int).to_numpy()
    t = np.arange(len(dates), dtype=float)
    market = np.full(len(dates), 100.0) if flat_market else 100 + 0.05 * t
    close = np.column_stack([
        10 + 0.005*t, 11 + 0.004*t, 12 + 0.003*t,
    ]).astype(np.float32)
    opening = close.copy(); high = close*1.01; low = close*0.99
    volume = np.full_like(close, 1_000_000)
    base = SelectionPanel(
        dates, ("sz000001", "sz000002", "sz000003"),
        opening, high, low, close, volume, market,
        execution={"open": opening, "high": high, "low": low,
                   "close": close, "volume": volume},
        amount=np.full_like(close, amount, dtype=np.float32),
        market_cap=np.full_like(close, 1e9, dtype=np.float64),
        industry=np.array([[0, 0, -1]] * len(dates), dtype=np.int16),
    )
    master = pd.DataFrame({
        "symbol": base.symbols, "list_date": ["2000-01-01"]*3,
        "delist_date": [None]*3, "status": ["listed"]*3,
    })
    return SelectionPanelV2(
        base, master, turnover=np.full_like(close, 0.5),
        st_status_known=np.ones_like(close, dtype=bool),
    )


def make_intent(panel, symbol="sz000001", suffix="a", score=1.0, stop_gap=1.0):
    day = 125; column = panel.symbol_index[symbol]
    raw = float(panel.exec_close[day, column])
    return TradeIntent(
        intent_id="intent-" + suffix, strategy_id="test", strategy_version="1",
        signal_asof=int(panel.dates[day]), symbol=symbol, score=score,
        signal_price_adjusted=raw, signal_price_raw=raw,
        adjustment_factor_signal=1.0,
        initial_stop_adjusted=raw-stop_gap, initial_stop_raw=raw-stop_gap,
        metadata={"max_buy_price_raw": raw + 0.3},
    )


def liberal(**changes):
    base = RiskConfig(
        single_trade_risk_fraction=1.0,
        portfolio_open_risk_fraction=1.0,
        industry_open_risk_fraction=1.0,
        same_day_new_risk_fraction=1.0,
        max_symbol_weight=1.0, max_gross_exposure=1.0,
        max_amount_participation=1.0, max_stress_loss_fraction=1.0,
    )
    return replace(base, **changes)


class PortfolioRiskTest(unittest.TestCase):

    def test_m4_account_namespace_flows_into_risk_decision(self):
        panel = make_panel()
        executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=100_000))
        source = replace(
            make_intent(panel), account_id="account-a",
            strategy_instance_id="instance-a",
            actor_activation_id="activation-a", source_snapshot_id="daily-a")
        decision = PortfolioRiskEngine(panel, liberal()).evaluate(
            executor, source, 125, 126, requested_quantity=100)
        self.assertEqual("account-a", decision.account_id)
        self.assertEqual("instance-a", decision.strategy_instance_id)
        self.assertEqual("activation-a", decision.actor_activation_id)
        self.assertEqual("daily-a", decision.source_snapshot_id)

    def test_config_is_strict_and_hash_is_stable(self):
        path = Path(__file__).parents[1] / "configs/selection/risk_v1.json"
        first = load_risk_config(path); second = load_risk_config(path)
        self.assertEqual(first.sha256, second.sha256)
        payload = json.loads(path.read_text())
        payload["surprise"] = 1
        with tempfile.NamedTemporaryFile("w", suffix=".json") as output:
            json.dump(payload, output); output.flush()
            with self.assertRaises(ValueError):
                load_risk_config(output.name)

    def test_each_primary_quantity_constraint_can_bind(self):
        cases = [
            (RiskConfig(), "SINGLE_TRADE_RISK"),
            (liberal(max_symbol_weight=0.01), "MAX_SYMBOL_WEIGHT"),
            (liberal(max_gross_exposure=0.01), "MAX_GROSS_EXPOSURE"),
            (liberal(max_amount_participation=0.0001), "CAPACITY"),
            (liberal(portfolio_open_risk_fraction=0.001), "PORTFOLIO_OPEN_RISK"),
            (liberal(industry_open_risk_fraction=0.001), "INDUSTRY_OPEN_RISK"),
            (liberal(same_day_new_risk_fraction=0.001), "SAME_DAY_NEW_RISK"),
            (liberal(max_stress_loss_fraction=0.001), "STRESS_LOSS"),
        ]
        for config, reason in cases:
            with self.subTest(reason=reason):
                panel = make_panel()
                executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=100_000))
                decision = PortfolioRiskEngine(panel, config).evaluate(
                    executor, make_intent(panel), 125, 126,
                    requested_quantity=9000,
                )
                self.assertIn(reason, decision.reason_codes)

    def test_one_lot_above_risk_budget_is_rejected(self):
        panel = make_panel()
        executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=1_000))
        decision = PortfolioRiskEngine(panel).evaluate(
            executor, make_intent(panel, stop_gap=5.0), 125, 126,
            requested_quantity=100,
        )
        self.assertEqual(decision.decision, "rejected")
        self.assertEqual(decision.final_quantity, 0)

    def test_requested_quantity_uses_explicit_fraction_and_ceiling(self):
        panel = make_panel()
        executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=100_000))
        engine = PortfolioRiskEngine(
            panel, liberal(single_trade_risk_fraction=0.005))
        intent = make_intent(panel)
        smaller = engine.requested_quantity_for_risk_fraction(
            executor, intent, 125, 0.0025)
        larger = engine.requested_quantity_for_risk_fraction(
            executor, intent, 125, 0.00375)
        self.assertGreater(larger, smaller)
        self.assertEqual(smaller % 100, 0)
        with self.assertRaises(ValueError):
            engine.requested_quantity_for_risk_fraction(
                executor, intent, 125, 0.006)

    def test_zero_pre_stress_capacity_does_not_mislabel_stress(self):
        panel = make_panel()
        executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=100_000))
        config = liberal(portfolio_open_risk_fraction=0.0001)
        decision = PortfolioRiskEngine(panel, config).evaluate(
            executor, make_intent(panel), 125, 126,
            requested_quantity=9000)
        self.assertEqual(decision.final_quantity, 0)
        self.assertIn("PORTFOLIO_OPEN_RISK", decision.reason_codes)
        self.assertNotIn("STRESS_LOSS", decision.reason_codes)

    def test_published_trailing_stop_releases_open_risk_capacity(self):
        panel = make_panel()
        executor = PortfolioExecutor(panel, ExecutionConfig(
            initial_cash=100_000, slippage_bps=0))
        first = make_intent(panel, "sz000001", "first")
        executor.approve_order(
            first, 1000, int(panel.dates[126]),
            first.metadata["max_buy_price_raw"], 1.3)
        self.assertEqual(executor.process_open(126)[0].status, "filled")
        engine = PortfolioRiskEngine(
            panel, liberal(portfolio_open_risk_fraction=0.02))
        before = engine.evaluate(
            executor, make_intent(panel, "sz000002", "before"), 126, 127,
            requested_quantity=1000)
        current_mark = float(panel.exec_close[126, 0])
        executor.update_position_stop("sz000001", current_mark-0.1)
        after = engine.evaluate(
            executor, make_intent(panel, "sz000002", "after"), 126, 127,
            requested_quantity=1000)
        self.assertGreater(after.quantity_portfolio_risk,
                           before.quantity_portfolio_risk)
        self.assertEqual(
            executor.positions["sz000001"].initial_stop_raw,
            first.initial_stop_raw)
        self.assertAlmostEqual(
            executor.positions["sz000001"].current_stop_raw,
            current_mark-0.1)

    def test_missing_beta_and_unknown_industry_are_conservative(self):
        panel = make_panel(flat_market=True)
        engine = PortfolioRiskEngine(panel)
        self.assertEqual(engine.beta_for(125, 2), 1.5)
        executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=100_000))
        intent = make_intent(panel, "sz000003")
        decision = engine.evaluate(executor, intent, 125, 126,
                                   requested_quantity=100)
        self.assertEqual(decision.decisive_scenario, "GAP_2R")
        self.assertTrue(np.isfinite(decision.stress_losses["INDUSTRY_10"]))

    def test_batch_order_is_deterministic(self):
        panel = make_panel()
        intents = [
            make_intent(panel, "sz000002", "b", score=1),
            make_intent(panel, "sz000001", "a", score=2),
        ]
        executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=100_000))
        engine = PortfolioRiskEngine(panel)
        results = engine.approve_batch(executor, list(reversed(intents)), 125, 126)
        self.assertEqual([item[2].symbol for item in results],
                         ["sz000001", "sz000002"])

    def test_shadow_does_not_change_requested_fill(self):
        panel = make_panel()
        direct = PortfolioExecutor(panel, ExecutionConfig(
            initial_cash=100_000, slippage_bps=0))
        shadow = PortfolioExecutor(panel, ExecutionConfig(
            initial_cash=100_000, slippage_bps=0))
        trade = make_intent(panel)
        direct.approve_order(trade, 1000, int(panel.dates[126]),
                             trade.metadata["max_buy_price_raw"], 1.3)
        _, _, decision = PortfolioRiskEngine(panel).approve(
            shadow, trade, 125, 126, requested_quantity=1000, shadow=True)
        self.assertLess(decision.final_quantity, 1000)
        first = direct.process_open(126)[0]
        second = shadow.process_open(126)[0]
        self.assertEqual((first.status, first.quantity, first.fill_price_raw),
                         (second.status, second.quantity, second.fill_price_raw))

    def test_cash_cap_includes_fees(self):
        panel = make_panel()
        executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=1_095))
        decision = PortfolioRiskEngine(panel, liberal()).evaluate(
            executor, make_intent(panel), 125, 126, requested_quantity=100)
        price = make_intent(panel).metadata["max_buy_price_raw"]
        gross_only = 100 * price
        self.assertLess(gross_only, executor.available_cash)
        self.assertEqual(decision.quantity_cash, 0)
        self.assertIn("CASH", decision.reason_codes)

    def test_post_fill_breach_creates_normal_sell_intent(self):
        panel = make_panel()
        executor = PortfolioExecutor(panel, ExecutionConfig(
            initial_cash=100_000, slippage_bps=0))
        trade = make_intent(panel)
        executor.approve_order(trade, 8000, int(panel.dates[126]),
                               trade.metadata["max_buy_price_raw"], 1.3)
        fill = executor.process_open(126)[0]
        self.assertEqual(fill.status, "filled")
        position = executor.positions[trade.symbol]
        frozen_initial_r = position.initial_r_per_share_raw
        # A share credit changes current open risk through quantity while the
        # historical per-share initial R stays frozen.
        executor.positions[trade.symbol] = type(position)(
            **{**position.__dict__, "quantity": 8800}
        )
        review = PortfolioRiskEngine(panel).post_fill_review(executor, 126)
        self.assertTrue(review.breach_codes)
        self.assertTrue(review.de_risk_intents)
        self.assertEqual(review.de_risk_intents[0].side, "sell")
        self.assertEqual(executor.positions[trade.symbol].initial_r_per_share_raw,
                         frozen_initial_r)

    def test_audit_export_contains_config_hash_and_decisions(self):
        panel = make_panel()
        executor = PortfolioExecutor(panel, ExecutionConfig(initial_cash=100_000))
        engine = PortfolioRiskEngine(panel)
        engine.evaluate(executor, make_intent(panel), 125, 126,
                        requested_quantity=100)
        with tempfile.TemporaryDirectory() as directory:
            paths = engine.export_audit(directory)
            config = json.loads(paths["config"].read_text())
            decisions = paths["decisions"].read_text().splitlines()
            self.assertEqual(config["config_sha256"], engine.config.sha256)
            self.assertEqual(len(decisions), 1)
            self.assertEqual(json.loads(decisions[0])["intent_id"], "intent-a")

    def test_executor_enforces_strategy_position_count_at_fill(self):
        panel = make_panel()
        executor = PortfolioExecutor(panel, ExecutionConfig(
            initial_cash=100_000, slippage_bps=0, max_positions=1))
        first = make_intent(panel, "sz000001", "first")
        second = make_intent(panel, "sz000002", "second")
        executor.approve_order(first, 100, int(panel.dates[126]),
                               first.metadata["max_buy_price_raw"], 1.3)
        executor.approve_order(second, 100, int(panel.dates[126]),
                               second.metadata["max_buy_price_raw"], 1.3)
        fills = executor.process_open(126)
        self.assertEqual([item.status for item in fills], ["filled", "rejected"])
        self.assertEqual(fills[1].reason_code, "MAX_POSITIONS")


if __name__ == "__main__":
    unittest.main()
