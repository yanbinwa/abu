# -*- encoding: utf-8 -*-
"""Causal market/industry absolute-state exits for Alpha158 research."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .ABuAlpha158Lite import Alpha158LiteExitEngine


@dataclass(frozen=True)
class MarketIndustryExitConfig:
    policy_id: str = "alpha158_market_industry_exit_v1"
    variant: str = "combined"
    percentile_lookback_sessions: int = 252
    percentile_min_periods: int = 126
    amount_control_sessions: int = 20
    minimum_history_sessions: int = 120
    minimum_industry_members: int = 10
    minimum_member_coverage: float = 0.80
    market_hot_quantile: float = 0.95
    market_reversal_quantile: float = 0.05
    market_retreat_quantile: float = 0.05
    market_breadth_quantile: float = 0.10
    market_amount_hot_quantile: float = 0.95
    market_amount_retreat_quantile: float = 0.75
    industry_hot_quantile: float = 0.90
    industry_reversal_quantile: float = 0.10
    industry_retreat_quantile: float = 0.10
    industry_breadth_quantile: float = 0.10
    industry_amount_hot_quantile: float = 0.90
    industry_amount_retreat_quantile: float = 0.75
    require_trailing_enabled: bool = True
    research_only: bool = True

    def __post_init__(self):
        if self.variant not in ("market", "industry", "combined"):
            raise ValueError("variant must be market, industry or combined")
        positive = (
            self.percentile_lookback_sessions, self.percentile_min_periods,
            self.amount_control_sessions, self.minimum_history_sessions,
            self.minimum_industry_members,
        )
        if any(int(value) <= 0 for value in positive):
            raise ValueError("lookbacks and member counts must be positive")
        if self.percentile_min_periods > self.percentile_lookback_sessions:
            raise ValueError("minimum periods exceeds percentile lookback")
        quantiles = (
            self.market_hot_quantile, self.market_reversal_quantile,
            self.market_retreat_quantile, self.market_breadth_quantile,
            self.market_amount_hot_quantile,
            self.market_amount_retreat_quantile,
            self.industry_hot_quantile, self.industry_reversal_quantile,
            self.industry_retreat_quantile, self.industry_breadth_quantile,
            self.industry_amount_hot_quantile,
            self.industry_amount_retreat_quantile,
        )
        if any(not 0 < float(value) < 1 for value in quantiles):
            raise ValueError("quantiles must be in (0, 1)")
        if not 0 < self.minimum_member_coverage <= 1:
            raise ValueError("minimum member coverage must be in (0, 1]")
        if not self.research_only:
            raise ValueError("v1 is research only")

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


@dataclass(frozen=True)
class MarketIndustryPartialRiskConfig:
    """Frozen execution rules for the partial market/industry risk overlay."""

    policy_id: str = "alpha158_market_industry_partial_risk_v1"
    block_entries_on_market_event: bool = True
    reduce_fraction: float = 0.50
    require_trailing_enabled: bool = True
    max_reductions_per_position: int = 1
    board_lot: int = 100
    minimum_remaining_quantity: int = 100
    market_priority: bool = True
    signal_at_close_execute_next_open: bool = True
    research_only: bool = True

    def __post_init__(self):
        if not 0 < float(self.reduce_fraction) < 1:
            raise ValueError("reduce_fraction must be in (0, 1)")
        if int(self.max_reductions_per_position) != 1:
            raise ValueError("v1 freezes one context reduction per position")
        if int(self.board_lot) <= 0:
            raise ValueError("board_lot must be positive")
        if int(self.minimum_remaining_quantity) < int(self.board_lot):
            raise ValueError("minimum remaining quantity must preserve one lot")
        if not self.market_priority:
            raise ValueError("v1 freezes market-priority event attribution")
        if not self.signal_at_close_execute_next_open:
            raise ValueError("v1 requires next-open causal execution")
        if not self.research_only:
            raise ValueError("v1 is research only")

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


def load_market_industry_exit_config(path, variant=None):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(MarketIndustryExitConfig)}
    if set(payload) != expected:
        raise ValueError("MarketIndustryExitConfig fields mismatch")
    if variant is not None:
        payload["variant"] = str(variant)
    return MarketIndustryExitConfig(**payload)


def load_market_industry_partial_risk_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(MarketIndustryPartialRiskConfig)}
    if set(payload) != expected:
        raise ValueError("MarketIndustryPartialRiskConfig fields mismatch")
    return MarketIndustryPartialRiskConfig(**payload)


def _rolling_prior_quantile(values, lookback, minimum, quantile):
    frame = pd.DataFrame(np.asarray(values, dtype=float))
    return frame.rolling(
        int(lookback), min_periods=int(minimum)
    ).quantile(float(quantile)).shift(1).to_numpy(dtype=float)


def _prior_median(values, window):
    frame = pd.DataFrame(np.asarray(values, dtype=float))
    return frame.rolling(
        int(window), min_periods=max(5, int(window) // 2)
    ).median().shift(1).to_numpy(dtype=float)


def _ratio(numerator, denominator):
    return np.divide(
        numerator, denominator,
        out=np.full(np.asarray(numerator).shape, np.nan, dtype=float),
        where=np.isfinite(denominator) & (denominator > 0),
    )


class MarketIndustryAbsoluteState(object):
    """Precompute causal absolute market and industry event states."""

    def __init__(self, panel, config):
        self.panel = panel
        self.config = config
        self.market_features, self.market_reason = self._market_states()
        (self.industry_features, self.industry_reason,
         self.industry_count) = self._industry_states()

    def _market_inputs(self):
        panel = self.panel
        close = np.asarray(panel.close, dtype=float)
        amount = np.asarray(panel.amount, dtype=float)
        denominator = panel.breadth_denominator(
            min_history=self.config.minimum_history_sessions,
            unknown_st_policy="include")
        observed = denominator & np.isfinite(close) & (close > 0)
        amount_valid = observed & np.isfinite(amount) & (amount > 0)
        market_amount = np.nansum(np.where(amount_valid, amount, np.nan), axis=1)
        market_amount[np.sum(amount_valid, axis=1) == 0] = np.nan
        amount_ratio = _ratio(
            market_amount, _prior_median(
                market_amount[:, None], self.config.amount_control_sessions)[:, 0])
        advance = np.full(len(panel.dates), np.nan)
        for day in range(1, len(panel.dates)):
            valid = observed[day] & np.isfinite(panel.returns[day])
            if valid.any():
                advance[day] = float(np.mean(panel.returns[day, valid] > 0))
        benchmark_close = np.asarray(panel.benchmark_close, dtype=float)
        benchmark_open = np.asarray(panel.benchmark_open, dtype=float)
        benchmark_5d = np.full(len(panel.dates), np.nan)
        benchmark_5d[5:] = benchmark_close[5:] / benchmark_close[:-5] - 1.0
        benchmark_intraday = _ratio(benchmark_close, benchmark_open) - 1.0
        return benchmark_5d, benchmark_intraday, amount_ratio, advance

    def _market_states(self):
        config = self.config
        ret5, intraday, amount_ratio, advance = self._market_inputs()
        lookback, minimum = (
            config.percentile_lookback_sessions,
            config.percentile_min_periods,
        )
        ret_hot = _rolling_prior_quantile(
            ret5[:, None], lookback, minimum,
            config.market_hot_quantile)[:, 0]
        intraday_low = _rolling_prior_quantile(
            intraday[:, None], lookback, minimum,
            config.market_reversal_quantile)[:, 0]
        ret_low = _rolling_prior_quantile(
            ret5[:, None], lookback, minimum,
            config.market_retreat_quantile)[:, 0]
        advance_low = _rolling_prior_quantile(
            advance[:, None], lookback, minimum,
            config.market_breadth_quantile)[:, 0]
        amount_hot = _rolling_prior_quantile(
            amount_ratio[:, None], lookback, minimum,
            config.market_amount_hot_quantile)[:, 0]
        amount_retreat = _rolling_prior_quantile(
            amount_ratio[:, None], lookback, minimum,
            config.market_amount_retreat_quantile)[:, 0]
        exhaustion = (
            (ret5 >= ret_hot) & (intraday <= intraday_low) &
            (amount_ratio >= amount_hot))
        retreat = (
            (ret5 <= ret_low) & (advance <= advance_low) &
            (amount_ratio >= amount_retreat))
        reason = np.full(len(ret5), "", dtype=object)
        reason[retreat] = "MARKET_RETREAT_EXIT"
        reason[exhaustion] = "MARKET_EXHAUSTION_EXIT"
        features = pd.DataFrame({
            "date": self.panel.dates.astype(int),
            "return_5d": ret5,
            "intraday_return": intraday,
            "amount_ratio_20": amount_ratio,
            "advance_ratio": advance,
            "return_hot_threshold": ret_hot,
            "intraday_reversal_threshold": intraday_low,
            "return_retreat_threshold": ret_low,
            "advance_retreat_threshold": advance_low,
            "amount_hot_threshold": amount_hot,
            "amount_retreat_threshold": amount_retreat,
            "exhaustion": exhaustion,
            "retreat": retreat,
            "reason": reason,
        })
        return features, reason

    def _industry_inputs(self):
        panel, config = self.panel, self.config
        industries = sorted(int(value) for value in np.unique(panel.industry)
                            if int(value) >= 0)
        count = max(industries) + 1 if industries else 0
        shape = (len(panel.dates), count)
        ret5 = np.full(shape, np.nan)
        intraday = np.full(shape, np.nan)
        amount_total = np.full(shape, np.nan)
        advance = np.full(shape, np.nan)
        member_count = np.zeros(shape, dtype=np.int16)
        denominator = panel.breadth_denominator(
            min_history=config.minimum_history_sessions,
            unknown_st_policy="include")
        close = np.asarray(panel.close, dtype=float)
        opening = np.asarray(panel.open, dtype=float)
        amount = np.asarray(panel.amount, dtype=float)
        stock5 = np.full(close.shape, np.nan)
        stock5[5:] = _ratio(close[5:], close[:-5]) - 1.0
        stock_intraday = _ratio(close, opening) - 1.0
        for day in range(len(panel.dates)):
            membership = np.asarray(panel.industry[day], dtype=int)
            for industry in np.unique(membership[denominator[day]]):
                industry = int(industry)
                if industry < 0 or industry >= count:
                    continue
                members = denominator[day] & (membership == industry)
                total = int(members.sum())
                member_count[day, industry] = total
                if total < config.minimum_industry_members:
                    continue
                for target, values in ((ret5, stock5),
                                       (intraday, stock_intraday)):
                    valid = members & np.isfinite(values[day])
                    if valid.sum() / total >= config.minimum_member_coverage:
                        target[day, industry] = float(np.mean(values[day, valid]))
                valid_return = members & np.isfinite(panel.returns[day])
                if valid_return.sum() / total >= config.minimum_member_coverage:
                    advance[day, industry] = float(np.mean(
                        panel.returns[day, valid_return] > 0))
                valid_amount = members & np.isfinite(amount[day]) & (amount[day] > 0)
                if valid_amount.sum() / total >= config.minimum_member_coverage:
                    amount_total[day, industry] = float(
                        np.sum(amount[day, valid_amount]))
        amount_ratio = _ratio(
            amount_total,
            _prior_median(amount_total, config.amount_control_sessions))
        return ret5, intraday, amount_ratio, advance, member_count

    def _industry_states(self):
        config = self.config
        ret5, intraday, amount_ratio, advance, member_count = \
            self._industry_inputs()
        lookback, minimum = (
            config.percentile_lookback_sessions,
            config.percentile_min_periods,
        )
        ret_hot = _rolling_prior_quantile(
            ret5, lookback, minimum, config.industry_hot_quantile)
        intraday_low = _rolling_prior_quantile(
            intraday, lookback, minimum, config.industry_reversal_quantile)
        ret_low = _rolling_prior_quantile(
            ret5, lookback, minimum, config.industry_retreat_quantile)
        advance_low = _rolling_prior_quantile(
            advance, lookback, minimum, config.industry_breadth_quantile)
        amount_hot = _rolling_prior_quantile(
            amount_ratio, lookback, minimum,
            config.industry_amount_hot_quantile)
        amount_retreat = _rolling_prior_quantile(
            amount_ratio, lookback, minimum,
            config.industry_amount_retreat_quantile)
        exhaustion = (
            (ret5 >= ret_hot) & (intraday <= intraday_low) &
            (amount_ratio >= amount_hot))
        retreat = (
            (ret5 <= ret_low) & (advance <= advance_low) &
            (amount_ratio >= amount_retreat))
        reason = np.full(ret5.shape, "", dtype=object)
        reason[retreat] = "INDUSTRY_RETREAT_EXIT"
        reason[exhaustion] = "INDUSTRY_EXHAUSTION_EXIT"
        features = {
            "return_5d": ret5,
            "intraday_return": intraday,
            "amount_ratio_20": amount_ratio,
            "advance_ratio": advance,
            "return_hot_threshold": ret_hot,
            "intraday_reversal_threshold": intraday_low,
            "return_retreat_threshold": ret_low,
            "advance_retreat_threshold": advance_low,
            "amount_hot_threshold": amount_hot,
            "amount_retreat_threshold": amount_retreat,
            "exhaustion": exhaustion,
            "retreat": retreat,
        }
        return features, reason, member_count

    def market_signal(self, day):
        reason = str(self.market_reason[int(day)])
        return reason or None

    def industry_signal(self, day, symbol):
        column = self.panel.symbol_index[symbol]
        industry = int(self.panel.industry[int(day), column])
        if industry < 0 or industry >= self.industry_reason.shape[1]:
            return None, industry
        reason = str(self.industry_reason[int(day), industry])
        return (reason or None), industry


class MarketIndustryExitEngine(Alpha158LiteExitEngine):
    """Preserve base exits and add absolute market/industry event exits."""

    def __init__(self, panel, strategy_config, overlay_config, context=None):
        super().__init__(panel, strategy_config)
        self.overlay = overlay_config
        if context is not None and context.panel is not panel:
            raise ValueError("market/industry context belongs to another panel")
        self.context = context or MarketIndustryAbsoluteState(
            panel, overlay_config)
        self.trigger_log = []

    def signal(self, day, symbol):
        base_reason = super().signal(day, symbol)
        if base_reason:
            return base_reason
        state = self.states[symbol]
        if self.overlay.require_trailing_enabled and not state.trailing_enabled:
            return None
        reason = None
        industry = -1
        if self.overlay.variant in ("market", "combined"):
            reason = self.context.market_signal(day)
        if reason is None and self.overlay.variant in ("industry", "combined"):
            reason, industry = self.context.industry_signal(day, symbol)
        if reason:
            self.trigger_log.append({
                "date": int(self.panel.dates[int(day)]),
                "symbol": symbol,
                "reason": reason,
                "variant": self.overlay.variant,
                "industry_id": int(industry),
                "trailing_enabled": bool(state.trailing_enabled),
                "peak_close_adjusted": float(state.peak_close_adjusted),
                "current_stop_adjusted": float(state.current_stop_adjusted),
            })
            return reason
        return None


class MarketIndustryPartialRiskOverlay(object):
    """One-shot partial reduction after causal absolute-state events."""

    def __init__(self, panel, state_config, execution_config, context=None,
                 enable_entry_block=True, enable_partial_reduction=True):
        self.panel = panel
        self.state_config = state_config
        self.config = execution_config
        self.context = context or MarketIndustryAbsoluteState(panel, state_config)
        if self.context.panel is not panel:
            raise ValueError("market/industry context belongs to another panel")
        self.enable_entry_block = bool(enable_entry_block)
        self.enable_partial_reduction = bool(enable_partial_reduction)
        self.pending = {}
        self.reduction_count = {}
        self.actions = []

    def register_entry(self, symbol, quantity):
        self.pending.pop(symbol, None)
        self.reduction_count[symbol] = 0
        self.actions.append({
            "action": "REGISTER_ENTRY", "symbol": symbol,
            "quantity": int(quantity),
        })

    def remove(self, symbol):
        self.pending.pop(symbol, None)
        self.reduction_count.pop(symbol, None)

    def entry_block_reason(self, day):
        if not (self.enable_entry_block and
                self.config.block_entries_on_market_event):
            return None
        return self.context.market_signal(day)

    def evaluate(self, day, symbol, position_quantity, exit_state):
        if not self.enable_partial_reduction or symbol in self.pending:
            return None
        if self.reduction_count.get(symbol, 0) >= \
                self.config.max_reductions_per_position:
            return None
        if (self.config.require_trailing_enabled and
                not bool(exit_state.trailing_enabled)):
            return None
        reason = self.context.market_signal(day)
        industry = -1
        if reason is None:
            reason, industry = self.context.industry_signal(day, symbol)
        if reason is None:
            return None
        quantity = int(position_quantity)
        lot = int(self.config.board_lot)
        reduce_quantity = int(quantity * self.config.reduce_fraction) // lot * lot
        if (reduce_quantity < lot or
                quantity - reduce_quantity < self.config.minimum_remaining_quantity):
            return None
        decision = {
            "reason": reason.replace("_EXIT", "_REDUCE_50"),
            "context_reason": reason,
            "quantity": int(reduce_quantity),
            "position_effect": "REDUCE",
            "industry_id": int(industry),
        }
        self.pending[symbol] = dict(decision)
        self.actions.append({
            "action": "PROPOSE_REDUCTION",
            "date": int(self.panel.dates[int(day)]), "symbol": symbol,
            **decision,
        })
        return decision

    def record_cancel(self, symbol):
        decision = self.pending.pop(symbol, None)
        if decision is not None:
            self.actions.append({
                "action": "CANCEL_REDUCTION", "symbol": symbol,
                **decision,
            })

    def record_fill(self, symbol, reason, quantity):
        decision = self.pending.pop(symbol, None)
        if decision is None:
            return
        self.reduction_count[symbol] = self.reduction_count.get(symbol, 0) + 1
        self.actions.append({
            "action": "FILL_REDUCTION", "symbol": symbol,
            "reason": str(reason), "quantity": int(quantity),
            "reduction_count": int(self.reduction_count[symbol]),
        })


__all__ = [
    "MarketIndustryAbsoluteState", "MarketIndustryExitConfig",
    "MarketIndustryExitEngine", "MarketIndustryPartialRiskConfig",
    "MarketIndustryPartialRiskOverlay", "load_market_industry_exit_config",
    "load_market_industry_partial_risk_config",
]
