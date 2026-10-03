# -*- encoding: utf-8 -*-
"""Unambiguous VCP core, attention ablations and event-driven exits."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

import numpy as np

from .ABuPriceLimit import limit_prices, price_limit_rule
from .ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor
from .ABuPortfolioRisk import PortfolioRiskEngine, RiskConfig
from .ABuTradeIntent import TradeIntent, make_record_id


@dataclass(frozen=True)
class VCPCoreConfig:
    strategy_version: str = "vcp_core_v1"
    min_history: int = 252
    contraction_sessions: int = 20
    control_sessions: int = 60
    contraction_ratio_max: float = 0.65
    atr_quantile: float = 0.30
    slope_sessions: int = 20
    structure_sessions: int = 20
    atr_stop_multiple: float = 2.0
    max_planned_risk_fraction: float = 0.08
    max_gap_atr: float = 1.0
    fixed_hold_sessions: int = 20

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


@dataclass(frozen=True)
class VCPAttentionConfig:
    strategy_version: str = "vcp_attention_v1"
    amount_dry_max: float = 0.70
    turnover_dry_max: float = 0.80
    amplitude_dry_max: float = 0.75
    amount_expansion_min: float = 1.50
    turnover_expansion_min: float = 1.25
    amplitude_expansion_min: float = 1.20
    breadth_ma120_min: float = 0.55
    minimum_field_coverage: float = 0.90

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


def _load_config(path, cls):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(cls)}
    if set(payload) != expected:
        raise ValueError("{} fields mismatch".format(cls.__name__))
    return cls(**payload)


def load_vcp_core_config(path):
    return _load_config(path, VCPCoreConfig)


def load_vcp_attention_config(path):
    return _load_config(path, VCPAttentionConfig)


def _rank(values, columns):
    """Zero-to-one rank with symbol-column tie break."""
    result = np.zeros(len(columns), dtype=float)
    if len(columns) <= 1:
        result[:] = 1.0
        return result
    order = np.lexsort((columns, values))
    result[order] = np.arange(len(columns), dtype=float) / (len(columns) - 1)
    return result


def _median_ratio(short, long, minimum_coverage):
    short_valid = np.isfinite(short)
    long_valid = np.isfinite(long)
    valid = ((short_valid.mean(axis=0) >= minimum_coverage) &
             (long_valid.mean(axis=0) >= minimum_coverage))
    short_median = np.nanmedian(short, axis=0)
    long_median = np.nanmedian(long, axis=0)
    valid &= np.isfinite(short_median) & np.isfinite(long_median) & (long_median > 0)
    ratio = np.divide(short_median, long_median,
                      out=np.full(short_median.shape, np.nan), where=valid)
    return ratio, valid


class VCPStrategy(object):
    VARIANTS = ("core", "core_common", "amount_common", "attention_common")

    def __init__(self, panel, core=None, attention=None):
        self.panel = panel
        self.core = core or VCPCoreConfig()
        self.attention = attention or VCPAttentionConfig()
        self._breadth_denominator = None

    def _base_components(self, day):
        c = self.core
        count = len(self.panel.symbols)
        if day < c.min_history or day + 1 >= len(self.panel.dates):
            return np.zeros(count, dtype=bool), {}
        eligible = self.panel.signal_eligible(
            c.min_history, unknown_st_policy="exclude")[day].copy()
        eligible &= self.panel.benchmark_close[day] > self.panel.market_ma200[day]
        eligible &= self.panel.close[day] > self.panel.ma120[day]
        eligible &= self.panel.ma60[day] > self.panel.ma120[day]

        slope_window = self.panel.ma120[day-c.slope_sessions+1:day+1].astype(float)
        slope_valid = np.isfinite(slope_window).all(axis=0) & (slope_window > 0).all(axis=0)
        x = np.arange(c.slope_sessions, dtype=float)
        centered = x - x.mean()
        slopes = np.full(count, np.nan)
        slopes[slope_valid] = np.sum(
            (np.log(slope_window[:, slope_valid]) -
             np.log(slope_window[:, slope_valid]).mean(axis=0)) * centered[:, None],
            axis=0,
        ) / np.sum(centered ** 2)
        eligible &= slopes > 0

        contraction = self.panel.high[day-c.contraction_sessions:day].astype(float)
        contraction_low = self.panel.low[day-c.contraction_sessions:day].astype(float)
        control_start = day - c.contraction_sessions - c.control_sessions
        control_high = self.panel.high[control_start:day-c.contraction_sessions].astype(float)
        control_low = self.panel.low[control_start:day-c.contraction_sessions].astype(float)
        complete = (np.isfinite(contraction).all(axis=0) &
                    np.isfinite(contraction_low).all(axis=0) &
                    np.isfinite(control_high).all(axis=0) &
                    np.isfinite(control_low).all(axis=0))
        contraction_range = (np.nanmax(contraction, axis=0) /
                             np.nanmin(contraction_low, axis=0) - 1)
        control_range = (np.nanmax(control_high, axis=0) /
                         np.nanmin(control_low, axis=0) - 1)
        eligible &= complete & (control_range > 0)
        eligible &= contraction_range <= c.contraction_ratio_max * control_range

        atr_fraction = self.panel.atr21 / self.panel.close
        history = atr_fraction[day-c.min_history:day].astype(float)
        history_valid = np.isfinite(history).mean(axis=0) >= 0.90
        threshold = np.nanquantile(history, c.atr_quantile, axis=0, method="linear")
        eligible &= history_valid & np.isfinite(atr_fraction[day-1])
        eligible &= atr_fraction[day-1] <= threshold

        breakout_level = np.nanmax(
            self.panel.high[day-c.structure_sessions:day].astype(float), axis=0)
        eligible &= self.panel.close[day] > breakout_level
        strength = ((self.panel.close[day] - breakout_level) /
                    self.panel.atr21[day])
        tightness = 1 - contraction_range / control_range
        eligible &= np.isfinite(strength) & np.isfinite(tightness)
        return eligible, {
            "slopes": slopes, "contraction_range": contraction_range,
            "control_range": control_range, "breakout_level": breakout_level,
            "breakout_strength": strength, "tightness": tightness,
        }

    def _attention_components(self, day):
        cfg = self.attention
        amount = self.panel.amount.astype(float)
        turnover = self.panel.turnover.astype(float)
        previous = np.roll(self.panel.close.astype(float), 1, axis=0)
        previous[0] = np.nan
        amplitude = (self.panel.high - self.panel.low) / previous
        result = {}
        for name, values in (("amount", amount), ("turnover", turnover),
                             ("amplitude", amplitude)):
            dry, dry_valid = _median_ratio(
                values[day-5:day], values[day-20:day],
                cfg.minimum_field_coverage,
            )
            baseline = np.nanmedian(values[day-20:day], axis=0)
            expansion_valid = (np.isfinite(values[day]) & np.isfinite(baseline) &
                               (baseline > 0) &
                               (np.isfinite(values[day-20:day]).mean(axis=0) >=
                                cfg.minimum_field_coverage))
            expansion = np.divide(values[day], baseline,
                                  out=np.full(baseline.shape, np.nan),
                                  where=expansion_valid)
            result[name + "_dry"] = dry
            result[name + "_dry_valid"] = dry_valid
            result[name + "_expansion"] = expansion
            result[name + "_expansion_valid"] = expansion_valid
        if self._breadth_denominator is None:
            self._breadth_denominator = self.panel.breadth_denominator(
                min_history=120, unknown_st_policy="exclude")
        denominator = self._breadth_denominator[day]
        breadth_count = int(denominator.sum())
        breadth = (float(((self.panel.close[day] > self.panel.ma120[day]) &
                          denominator).sum()) / breadth_count
                   if breadth_count else np.nan)
        result["breadth_ma120"] = breadth
        return result

    def generate_intents(self, day, variant="core"):
        if variant not in self.VARIANTS:
            raise ValueError("unknown VCP variant")
        eligible, data = self._base_components(day)
        attention = None
        if variant != "core":
            attention = self._attention_components(day)
            common = (attention["amount_dry_valid"] &
                      attention["turnover_dry_valid"] &
                      attention["amplitude_dry_valid"] &
                      attention["amount_expansion_valid"] &
                      attention["turnover_expansion_valid"] &
                      attention["amplitude_expansion_valid"])
            eligible &= common
        if variant in ("amount_common", "attention_common"):
            eligible &= attention["amount_dry"] <= self.attention.amount_dry_max
            eligible &= (attention["amount_expansion"] >=
                         self.attention.amount_expansion_min)
        if variant == "attention_common":
            eligible &= attention["turnover_dry"] <= self.attention.turnover_dry_max
            eligible &= attention["amplitude_dry"] <= self.attention.amplitude_dry_max
            eligible &= (attention["turnover_expansion"] >=
                         self.attention.turnover_expansion_min)
            eligible &= (attention["amplitude_expansion"] >=
                         self.attention.amplitude_expansion_min)
            eligible &= attention["breadth_ma120"] >= self.attention.breadth_ma120_min
        columns = np.flatnonzero(eligible)
        if not len(columns):
            return []
        if variant == "attention_common":
            score = (0.40 * _rank(attention["amount_expansion"][columns], columns) +
                     0.25 * _rank(attention["turnover_expansion"][columns], columns) +
                     0.20 * _rank(attention["amplitude_expansion"][columns], columns) +
                     0.15 * _rank(data["breakout_strength"][columns], columns))
            version = self.attention.strategy_version
        else:
            score = (0.60 * _rank(data["breakout_strength"][columns], columns) +
                     0.40 * _rank(data["tightness"][columns], columns))
            version = self.core.strategy_version + ("_" + variant if variant != "core" else "")
        intents = []
        for position, column in enumerate(columns):
            adjusted = float(self.panel.close[day, column])
            raw = float(self.panel.exec_close[day, column])
            factor = raw / adjusted
            structure_low = float(np.min(
                self.panel.low[day-self.core.structure_sessions:day, column]))
            stop_adjusted = max(
                structure_low,
                adjusted - self.core.atr_stop_multiple * float(self.panel.atr21[day, column]),
            )
            if stop_adjusted >= adjusted:
                continue
            raw_atr = float(self.panel.atr21[day, column]) * factor
            next_day = day + 1
            listing_session = (6 if self.panel.list_date[column] < int(self.panel.dates[0])
                               else int(self.panel.universe_mask[:next_day+1, column].sum()))
            rule = price_limit_rule(
                str(self.panel.board[column]), int(self.panel.dates[next_day]),
                bool(self.panel.st_status[next_day, column]), listing_session,
                bool(self.panel.st_status_known[next_day, column]),
            )
            _, upper = limit_prices(raw, rule)
            max_price = raw + self.core.max_gap_atr * raw_atr
            if upper is not None:
                max_price = min(max_price, upper)
            stop_raw = stop_adjusted * factor
            if max_price - stop_raw > raw * self.core.max_planned_risk_fraction:
                continue
            intents.append(TradeIntent(
                intent_id=make_record_id(version, int(self.panel.dates[day]),
                                         self.panel.symbols[column]),
                strategy_id=version, strategy_version="1",
                signal_asof=int(self.panel.dates[day]),
                symbol=self.panel.symbols[column], score=float(score[position]),
                signal_price_adjusted=adjusted, signal_price_raw=raw,
                adjustment_factor_signal=factor,
                initial_stop_adjusted=stop_adjusted, initial_stop_raw=stop_raw,
                industry_asof=int(self.panel.industry[day, column]),
                required_fields=("amount", "turnover") if variant != "core" else (),
                max_gap_atr=self.core.max_gap_atr,
                r_definition_version="vcp_structure_or_2atr_v1",
                metadata={
                    "max_buy_price_raw": max_price,
                    "breakout_level": float(data["breakout_level"][column]),
                    "variant": variant,
                    "hold_sessions": self.core.fixed_hold_sessions,
                    "stop_fraction": 1 - stop_adjusted / adjusted,
                },
            ))
        return sorted(intents, key=lambda item: (-item.score, item.symbol))

    def rebuild_placebo_intent(self, original, symbol, day, panel=None):
        """Recalculate replacement-specific VCP prices, structure and stop."""
        panel = panel or self.panel
        column = panel.symbol_index[symbol]
        adjusted = float(panel.close[day, column])
        raw = float(panel.exec_close[day, column])
        factor = raw / adjusted
        atr = float(panel.atr21[day, column])
        breakout = float(np.nanmax(
            panel.high[day-self.core.structure_sessions:day, column]))
        structure_low = float(np.nanmin(
            panel.low[day-self.core.structure_sessions:day, column]))
        stop_adjusted = max(structure_low,
                            adjusted-self.core.atr_stop_multiple*atr)
        next_day = min(day+1, len(panel.dates)-1)
        listing_session = (6 if panel.list_date[column] < int(panel.dates[0])
                           else int(panel.universe_mask[:next_day+1, column].sum()))
        rule = price_limit_rule(
            str(panel.board[column]), int(panel.dates[next_day]),
            bool(panel.st_status[next_day, column]), listing_session,
            bool(panel.st_status_known[next_day, column]),
        )
        _, upper = limit_prices(raw, rule)
        max_price = raw + self.core.max_gap_atr * atr * factor
        if upper is not None:
            max_price = min(max_price, upper)
        return replace(
            original,
            intent_id=make_record_id("placebo-vcp", original.intent_id, symbol),
            symbol=symbol, signal_price_adjusted=adjusted,
            signal_price_raw=raw, adjustment_factor_signal=factor,
            initial_stop_adjusted=stop_adjusted,
            initial_stop_raw=stop_adjusted*factor,
            industry_asof=int(panel.industry[day, column]),
            metadata={**dict(original.metadata),
                      "placebo_target": original.symbol,
                      "max_buy_price_raw": max_price,
                      "breakout_level": breakout,
                      "stop_fraction": 1-stop_adjusted/adjusted},
        )


@dataclass
class VCPPositionState:
    symbol: str
    entry_day: int
    breakout_level: float
    initial_stop_adjusted: float
    initial_r_adjusted: float
    peak_close_adjusted: float
    current_stop_adjusted: float
    trailing_enabled: bool = False


class VCPExitEngine(object):
    PRIORITY = ("INITIAL_STOP", "BREAKOUT_FAILURE", "TRAILING_STOP",
                "MARKET_REGIME", "STAGNATION", "FIXED_HOLD")

    def __init__(self, panel):
        self.panel = panel
        self.states = {}

    def register_entry(self, intent, fill, day):
        adjusted_entry = fill.fill_price_raw / intent.adjustment_factor_signal
        initial_r = adjusted_entry - float(intent.initial_stop_adjusted)
        self.states[intent.symbol] = VCPPositionState(
            symbol=intent.symbol, entry_day=day,
            breakout_level=float(intent.metadata["breakout_level"]),
            initial_stop_adjusted=float(intent.initial_stop_adjusted),
            initial_r_adjusted=initial_r,
            peak_close_adjusted=adjusted_entry,
            current_stop_adjusted=float(intent.initial_stop_adjusted),
        )

    def signal(self, day, symbol, fixed_hold=False):
        state = self.states[symbol]
        column = self.panel.symbol_index[symbol]
        close = float(self.panel.close[day, column])
        if np.isfinite(close):
            state.peak_close_adjusted = max(state.peak_close_adjusted, close)
        mfe = state.peak_close_adjusted - (
            state.initial_stop_adjusted + state.initial_r_adjusted)
        if mfe >= state.initial_r_adjusted:
            state.trailing_enabled = True
        if state.trailing_enabled and np.isfinite(self.panel.atr21[day, column]):
            trailing = state.peak_close_adjusted - 3 * self.panel.atr21[day, column]
            state.current_stop_adjusted = max(state.current_stop_adjusted, trailing)
        held = day - state.entry_day + 1
        triggered = []
        if close <= state.initial_stop_adjusted:
            triggered.append("INITIAL_STOP")
        if held <= 5 and close <= state.breakout_level:
            triggered.append("BREAKOUT_FAILURE")
        if state.trailing_enabled and close <= state.current_stop_adjusted:
            triggered.append("TRAILING_STOP")
        if self.panel.benchmark_close[day] < self.panel.market_ma200[day]:
            triggered.append("MARKET_REGIME")
        if held >= 20 and mfe < 0.5 * state.initial_r_adjusted:
            triggered.append("STAGNATION")
        if fixed_hold and held >= 20:
            triggered.append("FIXED_HOLD")
        return next((reason for reason in self.PRIORITY if reason in triggered), None)

    def remove(self, symbol):
        self.states.pop(symbol, None)


VCP_EXPERIMENTS = {
    "c_core_fixed20": ("core", False, False),
    "d_core_r_fixed20": ("core", True, False),
    "e_core_r_event": ("core", True, True),
    "f_core_common_r_event": ("core_common", True, True),
    "f_amount_common_r_event": ("amount_common", True, True),
    "f_attention_common_r_event": ("attention_common", True, True),
}


def run_vcp_backtest(panel, year=None, experiment="e_core_r_event",
                     slippage_bps=25.0, core_config=None,
                     attention_config=None, risk_config=None,
                     start_date=None, end_date=None, audit=None):
    """Run a C/D/E/F VCP experiment through the common executor."""
    if experiment not in VCP_EXPERIMENTS:
        raise ValueError("unknown VCP experiment")
    variant, use_risk, event_exit = VCP_EXPERIMENTS[experiment]
    if start_date is not None or end_date is not None:
        if start_date is None or end_date is None:
            raise ValueError("start_date and end_date must be provided together")
        indices = np.flatnonzero((panel.dates >= int(start_date)) &
                                 (panel.dates <= int(end_date)))
        period_label = "{}-{}".format(start_date, end_date)
    else:
        indices = np.flatnonzero(panel.dates // 10000 == int(year))
        period_label = int(year)
    if len(indices) == 0 or indices[0] == 0:
        raise ValueError("no test dates or prior signal date")
    first, last = int(indices[0]), int(indices[-1])
    executor = PortfolioExecutor(panel, ExecutionConfig(
        slippage_bps=slippage_bps, mode="pit_corrected", max_positions=10,
    ))
    previous = np.asarray(panel.exec_close[:first], dtype=float)
    for column in range(previous.shape[1]):
        valid = previous[:, column][np.isfinite(previous[:, column]) &
                                    (previous[:, column] > 0)]
        if len(valid):
            executor.last_close[column] = valid[-1]
    strategy = VCPStrategy(panel, core_config, attention_config)
    risk = PortfolioRiskEngine(panel, risk_config or RiskConfig())
    exits = VCPExitEngine(panel)
    intent_lookup = {}
    entry_intent_by_symbol = {}
    decision_rows = []
    exit_reasons = []
    all_intents = []

    pending_intents = strategy.generate_intents(first - 1, variant)
    all_intents.extend(pending_intents)
    pending_exits = []
    for day in range(first, last + 1):
        signal_day = day - 1
        for symbol, reason in pending_exits:
            position = executor.positions.get(symbol)
            if position is None:
                continue
            if any(order.side == "sell" and order.symbol == symbol
                   for order in executor.orders):
                continue
            sell = TradeIntent(
                intent_id=make_record_id("vcp-exit", int(panel.dates[signal_day]),
                                         symbol, reason),
                strategy_id=entry_intent_by_symbol[symbol].strategy_id,
                strategy_version="1", signal_asof=int(panel.dates[signal_day]),
                symbol=symbol, side="sell", metadata={"exit_reason": reason},
            )
            executor.approve_order(sell, position.quantity, int(panel.dates[day]))
            exit_reasons.append({"date": int(panel.dates[signal_day]),
                                 "symbol": symbol, "reason": reason})

        held_or_ordered = set(executor.positions) | {
            order.symbol for order in executor.orders if order.side == "buy"
        }
        candidates = [intent for intent in pending_intents
                      if intent.symbol not in held_or_ordered]
        possible_slots = max(0, 10 - len(executor.positions) +
                             sum(symbol in executor.positions
                                 for symbol, _ in pending_exits))
        candidates = candidates[:possible_slots]
        if use_risk:
            results = risk.approve_batch(executor, candidates, signal_day, day)
            decision_rows.extend(item[2] for item in results)
        else:
            for intent in candidates:
                max_price = float(intent.metadata["max_buy_price_raw"])
                quantity = int(80_000 / max_price / 100) * 100
                if quantity >= 100:
                    executor.approve_order(
                        intent, quantity, int(panel.dates[day]), max_price,
                        max_price - float(intent.initial_stop_raw),
                    )
        for intent in candidates:
            intent_lookup[intent.intent_id] = intent
        fills = executor.process_open(day)
        for fill in fills:
            if fill.status != "filled":
                continue
            if fill.side == "sell":
                exits.remove(fill.symbol)
                entry_intent_by_symbol.pop(fill.symbol, None)
            else:
                intent = intent_lookup[fill.intent_id]
                exits.register_entry(intent, fill, day)
                entry_intent_by_symbol[fill.symbol] = intent
        executor.process_close(day)

        pending_exits = []
        if day < last:
            for symbol in sorted(executor.positions):
                if any(order.side == "sell" and order.symbol == symbol
                       for order in executor.orders):
                    continue
                state = exits.states.get(symbol)
                if state is None:
                    continue
                if event_exit:
                    reason = exits.signal(day, symbol, fixed_hold=False)
                else:
                    held = day - state.entry_day + 1
                    reason = "FIXED_HOLD" if held >= strategy.core.fixed_hold_sessions else None
                if reason:
                    pending_exits.append((symbol, reason))
            pending_intents = strategy.generate_intents(day, variant)
            all_intents.extend(pending_intents)

    curve = executor.curve_frame()
    fills = executor.fills_frame()
    initial = executor.config.initial_cash
    capital = np.r_[initial, curve.capital.to_numpy()]
    daily = capital[1:] / capital[:-1] - 1
    cutoff = np.quantile(daily, 0.05) if len(daily) else np.nan
    result = {
        "strategy": (strategy.attention.strategy_version
                     if variant == "attention_common" else strategy.core.strategy_version),
        "variant": variant, "experiment": experiment, "period": period_label,
        "start": int(panel.dates[first]), "end": int(panel.dates[last]),
        "return_pct": (capital[-1] / initial - 1) * 100,
        "max_drawdown_pct": (capital / np.maximum.accumulate(capital) - 1).min() * 100,
        "daily_expected_shortfall_95_pct": (
            float(daily[daily <= cutoff].mean() * 100) if len(daily) else np.nan),
        "filled_buys": int(((fills.side == "buy") &
                            (fills.status == "filled")).sum()) if not fills.empty else 0,
        "filled_sells": int(((fills.side == "sell") &
                             (fills.status == "filled")).sum()) if not fills.empty else 0,
        "open_positions": len(executor.positions),
        "risk_decisions": len(decision_rows),
        "risk_rejected": sum(item.decision == "rejected" for item in decision_rows),
        "risk_reduced": sum(item.decision == "reduced" for item in decision_rows),
        "core_config_sha256": strategy.core.sha256,
        "attention_config_sha256": strategy.attention.sha256,
        "risk_config_sha256": risk.config.sha256,
    }
    if audit is not None:
        audit.update({
            "intents": list(all_intents),
            "orders": list(executor.order_history),
            "reservations": list(executor.reservation_history),
            "position_events": list(executor.position_events),
            "risk_decisions": list(decision_rows),
            "unexit_positions": [
                {"symbol": symbol, **position.__dict__}
                for symbol, position in sorted(executor.positions.items())
            ],
        })
    return result, curve, fills, decision_rows, exit_reasons
