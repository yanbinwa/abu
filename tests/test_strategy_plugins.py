import unittest
from dataclasses import replace
from types import SimpleNamespace

from abupy.AlphaBu.ABuAlpha158Lite import Alpha158LiteFeatureEngine
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor
from abupy.AlphaBu.ABuPortfolioRisk import PortfolioRiskEngine, RiskConfig
from abupy.AlphaBu.ABuStrategyPlugin import (
    Alpha158StrategyPlugin, DailyStrategySnapshot, PortfolioDomainCore,
    StrategyBinding, VCPStrategyPlugin,
)
from abupy.AlphaBu.ABuVCPStrategy import VCPStrategy
from abupy.AlphaBu.ABuTradeIntent import TradeIntent
from tests.test_portfolio_executor import make_executor
from tests.test_alpha158_lite import config as alpha_config
from tests.test_vcp_strategy import make_vcp_panel


BINDING = StrategyBinding("account-a", "instance-a", "activation-a")
EMPTY_ACCOUNT = SimpleNamespace(logical_trades=())


def without_namespace(intent):
    return replace(
        intent, account_id="", strategy_instance_id="",
        actor_activation_id="", source_snapshot_id="")


class StrategyPluginTest(unittest.TestCase):

    def test_account_namespace_flows_from_intent_to_order_and_fill(self):
        executor = make_executor()
        namespaced = TradeIntent(
            intent_id="namespaced", strategy_id="test", strategy_version="1",
            signal_asof=20250102, symbol="sz000001", initial_stop_raw=8.0,
            account_id="account-a", strategy_instance_id="instance-a",
            actor_activation_id="activation-a", source_snapshot_id="daily-a")
        order, _ = executor.approve_order(
            namespaced, 100, 20250103, 10.5, 2.0)
        fill = executor.process_open(1)[0]
        for record in (order, fill):
            self.assertEqual("account-a", record.account_id)
            self.assertEqual("instance-a", record.strategy_instance_id)
            self.assertEqual("activation-a", record.actor_activation_id)
            self.assertEqual("daily-a", record.source_snapshot_id)

    def test_vcp_adapter_preserves_golden_intents_and_adds_only_namespace(self):
        panel, day = make_vcp_panel()
        strategy = VCPStrategy(panel)
        direct = strategy.generate_intents(day, "core")
        snapshot = DailyStrategySnapshot(
            "daily-fixture", int(panel.dates[day]), panel, day)
        plugin = VCPStrategyPlugin(strategy, "core", BINDING)
        adapted = plugin.on_daily_close(snapshot, EMPTY_ACCOUNT)
        self.assertEqual(direct, [without_namespace(item) for item in adapted])
        self.assertTrue(adapted)
        for item in adapted:
            self.assertEqual("account-a", item.account_id)
            self.assertEqual("instance-a", item.strategy_instance_id)
            self.assertEqual("activation-a", item.actor_activation_id)
            self.assertEqual("daily-fixture", item.source_snapshot_id)
        self.assertIn("raw_ohlc", plugin.data_requirements().required)
        self.assertEqual("VCP_ENTRY", plugin.explain(adapted[0]).reason_code)

    def test_alpha158_adapter_preserves_engine_intents_and_provider_order(self):
        panel, day = make_vcp_panel()
        engine = Alpha158LiteFeatureEngine(panel, alpha_config())
        ranked = ((1, 0.9), (0, 0.8))
        direct = [engine.make_intent(day, column, score)
                  for column, score in ranked]
        snapshot = DailyStrategySnapshot(
            "daily-fixture", int(panel.dates[day]), panel, day)
        plugin = Alpha158StrategyPlugin(
            engine, lambda supplied: ranked, BINDING,
            strategy_version="event_exit_only")
        adapted = plugin.on_daily_close(snapshot, EMPTY_ACCOUNT)
        self.assertEqual(direct, [without_namespace(item) for item in adapted])
        self.assertEqual([item.symbol for item in direct],
                         [item.symbol for item in adapted])
        self.assertEqual("ALPHA158_ENTRY",
                         plugin.explain(adapted[0]).reason_code)

    def test_watchlist_uses_snapshot_candidates_and_account_trades_only(self):
        panel, day = make_vcp_panel()
        snapshot = DailyStrategySnapshot(
            "daily-fixture", int(panel.dates[day]), panel, day,
            payload={"candidate_symbols": ("sz000002",)})
        account = SimpleNamespace(logical_trades=(
            {"symbol": "sz000001", "status": "OPEN"},
            {"symbol": "sz000003", "status": "CLOSED"},
        ))
        request = VCPStrategyPlugin(
            VCPStrategy(panel), "core", BINDING).build_watchlist(snapshot, account)
        self.assertEqual(("sz000001", "sz000002"), request.symbols)

    def test_domain_core_matches_direct_executor_and_fails_cross_account(self):
        panel, day = make_vcp_panel()
        raw = VCPStrategy(panel).generate_intents(day, "core")[0]
        intent = BINDING.bind(raw, "daily-fixture")
        max_price = intent.metadata["max_buy_price_raw"]
        direct_executor = PortfolioExecutor(
            panel, ExecutionConfig(initial_cash=100_000))
        wrapped_executor = PortfolioExecutor(
            panel, ExecutionConfig(initial_cash=100_000))
        direct = direct_executor.approve_order(
            intent, 100, int(panel.dates[day + 1]), max_price, 1.0)
        core = PortfolioDomainCore(wrapped_executor, BINDING)
        wrapped = core.approve_order(
            intent, 100, int(panel.dates[day + 1]), max_price, 1.0)
        self.assertEqual(direct, wrapped)
        self.assertEqual(direct_executor.cash, wrapped_executor.cash)
        self.assertEqual(direct_executor.reserved_cash,
                         wrapped_executor.reserved_cash)

        risk = PortfolioRiskEngine(panel, RiskConfig())
        decision = core.evaluate_risk(
            risk, intent, day, day + 1, requested_quantity=100, shadow=True)
        self.assertEqual("account-a", decision.account_id)
        with self.assertRaisesRegex(ValueError, "namespace"):
            core.approve_order(
                replace(intent, account_id="other"), 100,
                int(panel.dates[day + 1]), max_price, 1.0)

    def test_legacy_exit_builder_is_delegated_without_rewriting(self):
        panel, day = make_vcp_panel()
        exit_intent = replace(
            VCPStrategy(panel).generate_intents(day, "core")[0],
            intent_id="exit", side="sell", initial_stop_raw=None)
        snapshot = DailyStrategySnapshot(
            "daily-fixture", int(panel.dates[day]), panel, day)
        plugin = VCPStrategyPlugin(
            SimpleNamespace(generate_intents=lambda *args, **kwargs: []),
            "core", BINDING,
            legacy_exit_builder=lambda supplied, account: [exit_intent])
        result = plugin.on_daily_close(snapshot, EMPTY_ACCOUNT)
        self.assertEqual(exit_intent, without_namespace(result[0]))
        self.assertEqual("VCP_EXIT", plugin.explain(result[0]).reason_code)


if __name__ == "__main__":
    unittest.main()
