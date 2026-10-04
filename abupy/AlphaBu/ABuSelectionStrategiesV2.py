# -*- encoding: utf-8 -*-
"""Frozen legacy strategy adapters and separated B1/B2 experiments."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

import numpy as np
import pandas as pd

from .ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor
from .ABuPortfolioRisk import PortfolioRiskEngine, RiskConfig
from .ABuTradeIntent import Reservation, TradeIntent, make_record_id


LEGACY_STRATEGIES = ("residual_momentum", "trend_reversal", "trend_breakout")
EXPERIMENT_MODES = ("a1_legacy_compat", "a2_pit_corrected", "b1", "b2")


@dataclass(frozen=True)
class B1Config:
    constraint_version: str = "b1_constraints_v1"
    max_symbol_weight: float = 0.08
    max_gross_exposure: float = 0.80
    max_industry_weight: float = 0.25
    max_same_day_notional_fraction: float = 0.20
    max_amount_participation: float = 0.05
    max_stress_loss_fraction: float = 0.06
    missing_beta: float = 1.50

    def __post_init__(self):
        values = [self.max_symbol_weight, self.max_gross_exposure,
                  self.max_industry_weight, self.max_same_day_notional_fraction,
                  self.max_amount_participation, self.max_stress_loss_fraction]
        if any(value <= 0 or value > 1 for value in values):
            raise ValueError("B1 fractions must be in (0, 1]")

    @property
    def sha256(self):
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def load_b1_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(B1Config)}
    if set(payload) != expected:
        raise ValueError("B1 config fields mismatch")
    return B1Config(**payload)


@dataclass(frozen=True)
class B1Decision:
    intent_id: str
    requested_quantity: int
    final_quantity: int
    decision: str
    reason_codes: tuple[str, ...]
    quantities: dict
    stress_losses: dict
    config_sha256: str


def _lots(value):
    return max(0, int(np.floor(value / 100.0)) * 100) if np.isfinite(value) else 0


class B1ConstraintEngine(object):
    """Market-value constraints only; it never invents a stop or an R value."""

    def __init__(self, panel, config=None):
        self.panel = panel
        self.config = config or B1Config()
        risk_config = RiskConfig(
            max_stress_loss_fraction=self.config.max_stress_loss_fraction,
            missing_beta=self.config.missing_beta,
        )
        self.stress = PortfolioRiskEngine(panel, risk_config)
        self.decisions = []

    def evaluate(self, executor, intent, day, requested_quantity):
        if intent.initial_stop_raw is not None or intent.r_definition_version != "none":
            raise ValueError("B1 intents must not contain a stop or R definition")
        column = self.panel.symbol_index[intent.symbol]
        max_price = float(intent.metadata["max_buy_price_raw"])
        rows = self.stress._portfolio(executor, day)
        position_value = sum(row["market_value"] for row in rows
                             if not row.get("pending", False))
        equity = executor.cash + position_value
        gross = sum(row["market_value"] for row in rows)
        industry = self.stress._industry(day, column)
        industry_value = sum(row["market_value"] for row in rows
                             if row["industry"] == industry)
        same_day = sum(
            order.quantity * float(order.max_buy_price_raw)
            for order in executor.orders
            if order.side == "buy" and
            order.created_asof == int(self.panel.dates[day])
        )
        start = max(0, day - 19)
        amounts = self.panel.amount[start:day + 1, column].astype(float)
        average_amount = float(np.nanmean(amounts)) if np.isfinite(amounts).any() else np.nan
        requested = _lots(requested_quantity)
        quantities = {
            "requested": requested,
            "symbol": _lots(equity * self.config.max_symbol_weight / max_price),
            "gross": _lots(max(0.0, equity * self.config.max_gross_exposure - gross) /
                           max_price),
            "industry": _lots(max(0.0, equity * self.config.max_industry_weight -
                                   industry_value) / max_price),
            "same_day": _lots(max(0.0, equity *
                                   self.config.max_same_day_notional_fraction -
                                   same_day) / max_price),
            "capacity": (_lots(average_amount * self.config.max_amount_participation /
                               max_price) if np.isfinite(average_amount) else 0),
            "cash": _lots(executor.available_cash / max_price),
        }
        while quantities["cash"] > 0:
            fees = sum(executor._fees(quantities["cash"], max_price, "buy"))
            if quantities["cash"] * max_price + fees <= executor.available_cash + 1e-9:
                break
            quantities["cash"] -= 100
        pre_stress = min(quantities.values())

        def losses(quantity):
            candidate = {
                "symbol": intent.symbol, "quantity": quantity,
                "market_value": quantity * max_price, "industry": industry,
                "beta": self.stress.beta_for(day, column), "open_risk": 0.0,
                "initial_r_cash": 0.0, "has_stop": False,
                "limit_fraction": self.stress._limit_fraction(day, column),
                "pending": True,
            }
            return self.stress.stress_losses(executor, day, candidate)

        budget = equity * self.config.max_stress_loss_fraction
        lo, hi = 0, pre_stress // 100
        keys = ("MARKET_7", "INDUSTRY_10",
                "MARKET_5_PLUS_INDUSTRY_5", "GAP_2R")
        while lo < hi:
            middle = (lo + hi + 1) // 2
            scenario = losses(middle * 100)
            if max(scenario[key] for key in keys) <= budget + 1e-9:
                lo = middle
            else:
                hi = middle - 1
        quantities["stress"] = lo * 100
        final = min(quantities.values())
        reasons = tuple(name.upper() for name, value in quantities.items()
                        if name != "requested" and value == final and value < requested)
        if not np.isfinite(average_amount):
            reasons = ("MISSING_AMOUNT",) + reasons
        decision = B1Decision(
            intent_id=intent.intent_id, requested_quantity=requested,
            final_quantity=final,
            decision=("rejected" if final < 100 else
                      "reduced" if final < requested else "approved"),
            reason_codes=reasons, quantities=quantities,
            stress_losses=losses(final), config_sha256=self.config.sha256,
        )
        self.decisions.append(decision)
        return decision

    def approve(self, executor, intent, day, entry_day, requested_quantity):
        decision = self.evaluate(executor, intent, day, requested_quantity)
        if decision.final_quantity < 100:
            return None, Reservation(
                reservation_id=make_record_id("b1-rejection", intent.intent_id),
                intent_id=intent.intent_id, reserved_cash=0, reserved_risk=0,
                reserved_industry_risk=0, reserved_same_day_risk=0,
                reserved_stress_loss=0, expires_on=int(self.panel.dates[entry_day]),
                decision="rejected", reason_codes=decision.reason_codes,
            ), decision
        order, reservation = executor.approve_order(
            intent, decision.final_quantity, int(self.panel.dates[entry_day]),
            max_buy_price_raw=float(intent.metadata["max_buy_price_raw"]),
            planned_risk_per_share=0.0,
        )
        return order, reservation, decision


class LegacyStrategyIntentAdapter(object):

    def __init__(self, panel, strategy, mode):
        if strategy not in LEGACY_STRATEGIES or mode not in EXPERIMENT_MODES:
            raise ValueError("unknown strategy or experiment mode")
        self.panel = panel
        self.strategy = strategy
        self.mode = mode
        self.position_state = {}

    @property
    def strategy_id(self):
        return (self.strategy + "_rstop_v2" if self.mode == "b2"
                else self.strategy + "_v1")

    def _score(self, day, column):
        if self.strategy == "trend_reversal":
            return float((self.panel.ma10[day, column] - self.panel.close[day, column]) /
                         self.panel.atr21[day, column])
        if self.strategy == "trend_breakout":
            return float((self.panel.close[day, column] -
                          self.panel.prior60_high[day, column]) /
                         self.panel.atr21[day, column])
        return 0.0

    def _intent(self, day, column):
        adjusted = float(self.panel.close[day, column])
        raw = float(self.panel.exec_close[day, column])
        factor = raw / adjusted
        atr = float(self.panel.atr21[day, column])
        stop_adjusted = None
        if self.mode == "b2":
            stop_adjusted = adjusted - 2 * atr
            if not np.isfinite(stop_adjusted) or stop_adjusted <= 0 or stop_adjusted >= adjusted:
                return None
        raw_atr = atr * factor
        max_price = raw + min(raw_atr, raw * 0.03)
        target_notional = 40_000 if self.strategy == "residual_momentum" else 80_000
        return TradeIntent(
            intent_id=make_record_id(self.strategy_id, int(self.panel.dates[day]),
                                     self.panel.symbols[column]),
            strategy_id=self.strategy_id,
            strategy_version=("2" if self.mode == "b2" else "1"),
            signal_asof=int(self.panel.dates[day]),
            symbol=self.panel.symbols[column], score=self._score(day, column),
            signal_price_adjusted=adjusted, signal_price_raw=raw,
            adjustment_factor_signal=factor,
            initial_stop_adjusted=stop_adjusted,
            initial_stop_raw=(stop_adjusted * factor
                              if stop_adjusted is not None else None),
            r_definition_version=("atr2_v1" if self.mode == "b2" else "none"),
            metadata={"max_buy_price_raw": max_price,
                      "target_notional": target_notional,
                      "source_strategy": self.strategy},
        )

    def signals(self, day, executor):
        held_symbols = set(executor.positions)
        held_columns = {self.panel.symbol_index[symbol] for symbol in held_symbols}
        exits = []
        if self.mode == "b2":
            for symbol in held_symbols:
                state = self.position_state.get(symbol, {})
                column = self.panel.symbol_index[symbol]
                close = self.panel.close[day, column]
                stop = state.get("initial_stop_adjusted")
                if stop is not None and np.isfinite(close) and close <= stop:
                    exits.append(symbol)
        if self.strategy == "residual_momentum":
            if day + 1 >= len(self.panel.dates) or \
                    self.panel.dates[day] // 100 == self.panel.dates[day + 1] // 100:
                return sorted(set(exits)), []
            desired = self.panel.momentum_picks(day)
            exits.extend(self.panel.symbols[column]
                         for column in held_columns if column not in desired)
            picks = [column for column in desired if column not in held_columns]
        elif self.strategy == "trend_reversal":
            for symbol in held_symbols:
                column = self.panel.symbol_index[symbol]
                state = self.position_state.get(symbol, {})
                price = self.panel.close[day, column]
                if (not self.panel.benchmark_close[day] > self.panel.market_ma120[day] or
                        (np.isfinite(price) and
                         (price >= self.panel.ma10[day, column] or
                          price < state.get("entry_signal_price", price) -
                          2 * state.get("entry_atr", 0))) or
                        day - state.get("entry_day", day) >= 10):
                    exits.append(symbol)
            picks = self.panel.reversal_picks(day, held_columns)
        else:
            for symbol in held_symbols:
                column = self.panel.symbol_index[symbol]
                state = self.position_state.setdefault(symbol, {})
                price = self.panel.close[day, column]
                if np.isfinite(price):
                    state["peak"] = max(state.get("peak", float(price)), float(price))
                atr = self.panel.atr21[day, column]
                trail = state.get("peak", -np.inf) - 3 * atr if np.isfinite(atr) else -np.inf
                if (not self.panel.benchmark_close[day] > self.panel.market_ma200[day] or
                        (np.isfinite(price) and
                         price < max(trail, self.panel.ma60[day, column]))):
                    exits.append(symbol)
            picks = self.panel.breakout_picks(day, held_columns)

        min_history = 252 if self.strategy == "residual_momentum" else 120
        if self.mode != "a1_legacy_compat":
            eligible = self.panel.signal_eligible(min_history, unknown_st_policy="exclude")[day]
            picks = [column for column in picks if eligible[column]]
        intents = [self._intent(day, column) for column in picks]
        return sorted(set(exits)), [item for item in intents if item is not None]

    def register_fills(self, fills, intent_lookup, day):
        for fill in fills:
            if fill.status != "filled":
                continue
            if fill.side == "sell":
                self.position_state.pop(fill.symbol, None)
                continue
            intent = intent_lookup[fill.intent_id]
            column = self.panel.symbol_index[fill.symbol]
            self.position_state[fill.symbol] = {
                "entry_signal_price": float(intent.signal_price_adjusted),
                "entry_atr": float(self.panel.atr21[day - 1, column]),
                "entry_day": day,
                "peak": float(intent.signal_price_adjusted),
                "initial_stop_adjusted": intent.initial_stop_adjusted,
            }


def rebuild_legacy_placebo_intent(original, symbol, day, panel):
    """Recalculate legacy replacement prices without adding a B1 shadow stop."""
    column = panel.symbol_index[symbol]
    adjusted = float(panel.close[day, column])
    raw = float(panel.exec_close[day, column])
    factor = raw / adjusted
    atr = float(panel.atr21[day, column])
    if original.r_definition_version == "none":
        stop_adjusted = stop_raw = None
    elif original.r_definition_version == "atr2_v1":
        stop_adjusted = adjusted - 2 * atr
        stop_raw = stop_adjusted * factor
    else:
        raise ValueError("unsupported legacy R definition: {}".format(
            original.r_definition_version))
    max_price = raw + min(atr*factor, raw*0.03)
    return replace(
        original,
        intent_id=make_record_id("placebo-legacy", original.intent_id, symbol),
        symbol=symbol, signal_price_adjusted=adjusted, signal_price_raw=raw,
        adjustment_factor_signal=factor,
        initial_stop_adjusted=stop_adjusted, initial_stop_raw=stop_raw,
        industry_asof=int(panel.industry[day, column]),
        metadata={**dict(original.metadata), "placebo_target": original.symbol,
                  "max_buy_price_raw": max_price},
    )


def run_legacy_v2_backtest(panel, strategy, year, mode="a2_pit_corrected",
                           slippage_bps=25.0, risk_config=None, b1_config=None,
                           start_date=None, end_date=None):
    """Run one frozen legacy rule under one explicitly named experiment."""
    if start_date is not None or end_date is not None:
        if start_date is None or end_date is None:
            raise ValueError("start_date and end_date must be provided together")
        indices = np.flatnonzero((panel.dates >= int(start_date)) &
                                (panel.dates <= int(end_date)))
    else:
        indices = np.flatnonzero(panel.dates // 10000 == int(year))
    if len(indices) == 0 or indices[0] == 0:
        raise ValueError("no backtest dates or missing prior signal date")
    first, last = int(indices[0]), int(indices[-1])
    target_positions = 20 if strategy == "residual_momentum" else 10
    executor = PortfolioExecutor(panel, ExecutionConfig(
        slippage_bps=slippage_bps,
        mode="legacy_compat" if mode == "a1_legacy_compat" else "pit_corrected",
        max_positions=target_positions,
    ))
    previous = pd.DataFrame(panel.exec_close[:first]).ffill()
    if not previous.empty:
        executor.last_close = previous.iloc[-1].to_numpy(dtype=float)
    adapter = LegacyStrategyIntentAdapter(panel, strategy, mode)
    risk = PortfolioRiskEngine(panel, risk_config or RiskConfig()) if mode == "b2" else None
    b1 = B1ConstraintEngine(panel, b1_config or B1Config()) if mode == "b1" else None
    intent_lookup = {}
    rejection_rows = []

    exits, intents = adapter.signals(first - 1, executor)
    pending_signals = (exits, intents, first - 1)
    for day in range(first, last + 1):
        exits, intents, signal_day = pending_signals
        for symbol in exits:
            position = executor.positions.get(symbol)
            if position is None:
                continue
            sell = TradeIntent(
                intent_id=make_record_id("exit", adapter.strategy_id,
                                         int(panel.dates[signal_day]), symbol),
                strategy_id=adapter.strategy_id,
                strategy_version="2" if mode == "b2" else "1",
                signal_asof=int(panel.dates[signal_day]), symbol=symbol, side="sell",
            )
            executor.approve_order(sell, position.quantity, int(panel.dates[day]))
        possible_slots = max(0, target_positions - len(executor.positions) +
                             sum(symbol in executor.positions for symbol in exits))
        ordered = sorted(intents, key=lambda item: (-item.score, item.symbol))[:possible_slots]
        if mode == "b2":
            results = risk.approve_batch(executor, ordered, signal_day, day)
            rejection_rows.extend(item[2] for item in results)
        else:
            for intent in ordered:
                max_price = float(intent.metadata["max_buy_price_raw"])
                requested = _lots(float(intent.metadata["target_notional"]) / max_price)
                if mode == "b1":
                    _, _, decision = b1.approve(
                        executor, intent, signal_day, day, requested)
                    rejection_rows.append(decision)
                elif requested >= 100:
                    executor.approve_order(intent, requested, int(panel.dates[day]),
                                           max_price, 0.0)
                intent_lookup[intent.intent_id] = intent
        for intent in ordered:
            intent_lookup[intent.intent_id] = intent
        fills = executor.process_open(day)
        adapter.register_fills(fills, intent_lookup, day)
        executor.process_close(day)
        pending_signals = ([], [], day)
        if day < last:
            pending_signals = (*adapter.signals(day, executor), day)

    curve = executor.curve_frame()
    fills = executor.fills_frame()
    initial = executor.config.initial_cash
    capital = np.r_[initial, curve.capital.to_numpy()]
    daily_returns = capital[1:] / capital[:-1] - 1
    cutoff = np.quantile(daily_returns, 0.05) if len(daily_returns) else np.nan
    expected_shortfall = (float(daily_returns[daily_returns <= cutoff].mean())
                          if len(daily_returns) else np.nan)
    traded_notional = (float((fills.quantity * fills.fill_price_raw).sum())
                       if not fills.empty else 0.0)
    ending_industry = {}
    for symbol, position in executor.positions.items():
        column = panel.symbol_index[symbol]
        bucket = (str(int(panel.industry[last, column]))
                  if panel.industry[last, column] >= 0 else "UNKNOWN")
        mark = float(panel.exec_close[last, column])
        value = position.quantity * (mark if np.isfinite(mark) else
                                     executor.last_close[column])
        ending_industry[bucket] = ending_industry.get(bucket, 0.0) + value
    result = {
        "strategy": adapter.strategy_id, "source_strategy": strategy,
        "experiment": mode, "year": int(year) if year is not None else None,
        "start": int(panel.dates[first]), "end": int(panel.dates[last]),
        "return_pct": (capital[-1] / initial - 1) * 100,
        "max_drawdown_pct": (capital / np.maximum.accumulate(capital) - 1).min() * 100,
        "filled_buys": int(((fills.side == "buy") & (fills.status == "filled")).sum())
        if not fills.empty else 0,
        "filled_sells": int(((fills.side == "sell") & (fills.status == "filled")).sum())
        if not fills.empty else 0,
        "open_positions": len(executor.positions),
        "fees": float(fills[["commission", "transfer_fee", "stamp_tax"]].sum().sum())
        if not fills.empty else 0.0,
        "slippage_cost": float(fills.slippage_cost.sum()) if not fills.empty else 0.0,
        "turnover_initial_cash_multiple": traded_notional / initial,
        "daily_expected_shortfall_95_pct": expected_shortfall * 100,
        "ending_max_industry_weight": (max(ending_industry.values()) / capital[-1]
                                       if ending_industry and capital[-1] > 0 else 0.0),
        "unable_exit_positions": sum(1 for order in executor.orders
                                     if order.side == "sell"),
        "risk_decisions": len(rejection_rows),
        "risk_rejected": sum(item.decision == "rejected" for item in rejection_rows),
        "risk_reduced": sum(item.decision == "reduced" for item in rejection_rows),
    }
    return result, curve, fills, rejection_rows
