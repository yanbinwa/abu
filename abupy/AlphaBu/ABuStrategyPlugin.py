# -*- encoding: utf-8 -*-
"""M4 strategy-plugin boundary for committed daily snapshots.

Adapters delegate signal and exit calculations to the existing frozen strategy
objects.  They only add stable account ownership and never access market data
outside the supplied snapshot.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Sequence

from .ABuTradeIntent import TradeIntent


@dataclass(frozen=True)
class DataRequirements:
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()
    display_only: tuple[str, ...] = ()


@dataclass(frozen=True)
class DailyStrategySnapshot:
    snapshot_id: str
    trading_session: int
    panel: Any
    day: int
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyBinding:
    account_id: str
    strategy_instance_id: str
    activation_id: str

    def bind(self, intent: TradeIntent, snapshot_id: str) -> TradeIntent:
        return replace(
            intent, account_id=self.account_id,
            strategy_instance_id=self.strategy_instance_id,
            actor_activation_id=self.activation_id,
            source_snapshot_id=snapshot_id)


@dataclass(frozen=True)
class WatchlistRequest:
    symbols: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class DecisionExplanation:
    intent_id: str
    reason_code: str
    reason_text: str
    factor_values: Mapping[str, Any]
    source_snapshot_id: str


class StrategyPlugin(ABC):
    strategy_id = ""
    strategy_version = ""

    @abstractmethod
    def data_requirements(self) -> DataRequirements:
        raise NotImplementedError

    @abstractmethod
    def build_watchlist(self, daily_snapshot, account_view) -> WatchlistRequest:
        raise NotImplementedError

    @abstractmethod
    def on_daily_close(self, daily_snapshot, account_view) -> list[TradeIntent]:
        raise NotImplementedError

    @abstractmethod
    def explain(self, intent) -> DecisionExplanation:
        raise NotImplementedError


def _active_symbols(account_view):
    symbols = {
        row["symbol"] for row in account_view.logical_trades
        if row.get("status") in ("OPEN", "ACTIVE", "EXIT_REQUESTED", "EXIT_PENDING")
    }
    return symbols


class VCPStrategyPlugin(StrategyPlugin):
    strategy_id = "vcp"

    def __init__(self, strategy, variant, binding,
                 legacy_exit_builder: Callable | None = None):
        self.strategy = strategy
        self.variant = variant
        self.binding = binding
        self.legacy_exit_builder = legacy_exit_builder
        self.strategy_version = "{}:{}".format(
            getattr(strategy, "__class__", type(strategy)).__name__, variant)

    def data_requirements(self):
        common = (
            "adjusted_ohlc", "raw_ohlc", "amount", "atr21", "ma60", "ma120",
            "benchmark_ma200", "universe", "industry", "price_limit_reference")
        if self.variant in ("core_common", "amount_common", "attention_common"):
            common += ("turnover", "market_breadth")
        return DataRequirements(required=common)

    def build_watchlist(self, daily_snapshot, account_view):
        symbols = _active_symbols(account_view)
        symbols.update(daily_snapshot.payload.get("candidate_symbols", ()))
        return WatchlistRequest(
            symbols=tuple(sorted(symbols)),
            reason_codes=("ACTIVE_POSITION", "DAILY_CANDIDATE"))

    def on_daily_close(self, daily_snapshot, account_view):
        entries = self.strategy.generate_intents(
            int(daily_snapshot.day), self.variant,
            allow_terminal=bool(daily_snapshot.payload.get("allow_terminal", False)))
        exits = [] if self.legacy_exit_builder is None else list(
            self.legacy_exit_builder(daily_snapshot, account_view))
        return [self.binding.bind(item, daily_snapshot.snapshot_id)
                for item in list(entries) + exits]

    def explain(self, intent):
        reason = "VCP_EXIT" if intent.side == "sell" else "VCP_ENTRY"
        return DecisionExplanation(
            intent_id=intent.intent_id, reason_code=reason,
            reason_text="existing VCP rule emitted this intent",
            factor_values=dict(intent.metadata or {}),
            source_snapshot_id=intent.source_snapshot_id)


class Alpha158StrategyPlugin(StrategyPlugin):
    strategy_id = "alpha158"

    def __init__(self, feature_engine, score_provider: Callable,
                 binding: StrategyBinding,
                 legacy_exit_builder: Callable | None = None,
                 strategy_version="alpha158_lite"):
        self.feature_engine = feature_engine
        self.score_provider = score_provider
        self.binding = binding
        self.legacy_exit_builder = legacy_exit_builder
        self.strategy_version = strategy_version

    def data_requirements(self):
        return DataRequirements(required=(
            "adjusted_ohlc", "raw_ohlc", "amount", "atr21", "ma120",
            "universe", "industry", "price_limit_reference"))

    def build_watchlist(self, daily_snapshot, account_view):
        symbols = _active_symbols(account_view)
        symbols.update(daily_snapshot.payload.get("candidate_symbols", ()))
        return WatchlistRequest(
            symbols=tuple(sorted(symbols)),
            reason_codes=("ACTIVE_POSITION", "DAILY_CANDIDATE"))

    def on_daily_close(self, daily_snapshot, account_view):
        entries = []
        for column, score in self.score_provider(daily_snapshot):
            intent = self.feature_engine.make_intent(
                int(daily_snapshot.day), int(column), float(score))
            if intent is not None:
                entries.append(intent)
        exits = [] if self.legacy_exit_builder is None else list(
            self.legacy_exit_builder(daily_snapshot, account_view))
        return [self.binding.bind(item, daily_snapshot.snapshot_id)
                for item in entries + exits]

    def explain(self, intent):
        reason = "ALPHA158_EXIT" if intent.side == "sell" else "ALPHA158_ENTRY"
        return DecisionExplanation(
            intent_id=intent.intent_id, reason_code=reason,
            reason_text="existing Alpha158 rule emitted this intent",
            factor_values=dict(intent.metadata or {}),
            source_snapshot_id=intent.source_snapshot_id)


class PortfolioDomainCore(object):
    """IO-free namespace guard around the existing PortfolioExecutor."""

    def __init__(self, executor, binding: StrategyBinding):
        self.executor = executor
        self.binding = binding

    def validate_intent(self, intent):
        actual = (intent.account_id, intent.strategy_instance_id,
                  intent.actor_activation_id)
        expected = (self.binding.account_id, self.binding.strategy_instance_id,
                    self.binding.activation_id)
        if actual != expected:
            raise ValueError("intent account namespace does not match domain core")
        if not intent.source_snapshot_id:
            raise ValueError("intent requires a committed source snapshot")
        return intent

    def approve_order(self, intent, quantity, valid_session,
                      max_buy_price_raw=None, planned_risk_per_share=0.0,
                      portfolio_equity_asof=0.0):
        self.validate_intent(intent)
        return self.executor.approve_order(
            intent, quantity, valid_session,
            max_buy_price_raw=max_buy_price_raw,
            planned_risk_per_share=planned_risk_per_share,
            portfolio_equity_asof=portfolio_equity_asof)

    def evaluate_risk(self, risk_engine, intent, day, valid_session,
                      requested_quantity, shadow=False):
        self.validate_intent(intent)
        return risk_engine.evaluate(
            self.executor, intent, day, valid_session,
            requested_quantity=requested_quantity, shadow=shadow)
