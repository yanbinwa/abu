# -*- encoding: utf-8 -*-
"""Execution-aware Alpha158 event-exit labels for isolated research only."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np
import pandas as pd

from .ABuAlpha158Lite import Alpha158LiteConfig, Alpha158LiteFeatureEngine
from .ABuPriceLimit import (
    can_sell_at_open, limit_prices, price_limit_rule,
)


@dataclass(frozen=True)
class Alpha158PolicyLabelConfig:
    label_version: str = "alpha158_event_exit_r60_v1"
    maximum_holding_sessions: int = 60
    entry_slippage_bps: float = 25.0
    exit_slippage_bps: float = 25.0
    target_kind: str = "gross_price_r"
    maturity_policy: str = "fixed_signal_plus_61_sessions"

    def __post_init__(self):
        if self.label_version != "alpha158_event_exit_r60_v1":
            raise ValueError("unsupported policy label version")
        if self.maximum_holding_sessions != 60:
            raise ValueError("v1 is frozen to 60 holding sessions")
        if min(self.entry_slippage_bps, self.exit_slippage_bps) < 0:
            raise ValueError("label slippage cannot be negative")
        if self.target_kind != "gross_price_r":
            raise ValueError("v1 target kind must remain gross_price_r")
        if self.maturity_policy != "fixed_signal_plus_61_sessions":
            raise ValueError("v1 maturity policy is frozen")

    @property
    def sha256(self):
        return hashlib.sha256(json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()


def load_alpha158_policy_label_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(Alpha158PolicyLabelConfig)}
    if set(payload) != expected:
        raise ValueError("Alpha158PolicyLabelConfig fields mismatch")
    return Alpha158PolicyLabelConfig(**payload)


class Alpha158PolicyLabelBuilder(object):
    """Build full-candidate labels matching the frozen event-exit sequence.

    The target intentionally excludes quantity-dependent commissions.  It is a
    per-share gross price R label, not an account-PnL label.  Account evaluation
    remains the final economic test.
    """

    def __init__(self, panel, strategy_config=None, label_config=None):
        self.panel = panel
        self.strategy = strategy_config or Alpha158LiteConfig()
        self.config = label_config or Alpha158PolicyLabelConfig()
        self.features = Alpha158LiteFeatureEngine(panel, self.strategy)
        semantics = {
            "label": asdict(self.config),
            "strategy": {
                key: getattr(self.strategy, key) for key in (
                    "atr_stop_multiple", "trailing_atr_multiple",
                    "trailing_activation_r", "stagnation_sessions",
                    "stagnation_mfe_r", "max_gap_atr")},
            "entry": "next_open_with_stop_invalidated_fail_closed",
            "exit": "close_signal_then_next_executable_open",
            "corporate_action_scale": "daily_raw_to_adjusted_open_factor",
        }
        self.config_sha256 = hashlib.sha256(json.dumps(
            semantics, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def _previous_raw_close(self, day, column):
        values = np.asarray(self.panel.exec_close[:day, column], dtype=float)
        valid = values[np.isfinite(values) & (values > 0)]
        return float(valid[-1]) if len(valid) else np.nan

    def _sell_fill(self, day, column):
        if day >= len(self.panel.dates) or not bool(
                self.panel.sell_tradable_mask[day, column]):
            return None
        opening = float(self.panel.exec_open[day, column])
        adjusted_open = float(self.panel.open[day, column])
        previous = self._previous_raw_close(day, column)
        if not all(np.isfinite(item) and item > 0 for item in (
                opening, adjusted_open, previous)):
            return None
        rule = price_limit_rule(
            str(self.panel.board[column]), int(self.panel.dates[day]),
            bool(self.panel.st_status[day, column]), 6,
            bool(self.panel.st_status_known[day, column]))
        lower, _ = limit_prices(previous, rule)
        if not can_sell_at_open(opening, lower):
            return None
        price_raw = opening*(1-self.config.exit_slippage_bps/10000.0)
        if lower is not None and price_raw < lower-1e-12:
            return None
        factor = opening/adjusted_open
        return price_raw/factor

    def _event_path(self, entry_day, column, entry_adjusted,
                    initial_stop_adjusted, initial_r):
        peak = float(entry_adjusted)
        current_stop = float(initial_stop_adjusted)
        trailing = False
        last_decision = entry_day+self.config.maximum_holding_sessions-1
        fixed_maturity_day = entry_day+self.config.maximum_holding_sessions
        for day in range(entry_day, last_decision+1):
            close = float(self.panel.close[day, column])
            if not np.isfinite(close):
                continue
            peak = max(peak, close)
            mfe = peak-entry_adjusted
            if mfe >= self.strategy.trailing_activation_r*initial_r:
                trailing = True
            atr = float(self.panel.atr21[day, column])
            if trailing and np.isfinite(atr):
                current_stop = max(
                    current_stop,
                    peak-self.strategy.trailing_atr_multiple*atr)
            held = day-entry_day+1
            reason = None
            if close <= initial_stop_adjusted:
                reason = "INITIAL_STOP"
            elif trailing and close <= current_stop:
                reason = "TRAILING_STOP"
            elif (held >= self.strategy.stagnation_sessions and
                  mfe < self.strategy.stagnation_mfe_r*initial_r):
                reason = "STAGNATION"
            if reason is None:
                continue
            for exit_day in range(day+1, fixed_maturity_day+1):
                exit_adjusted = self._sell_fill(exit_day, column)
                if exit_adjusted is None:
                    continue
                return {
                    "event_path_r_60d": (
                        exit_adjusted-entry_adjusted)/initial_r,
                    "event_path_reason_60d": reason,
                    "event_path_end_date_60d": int(self.panel.dates[exit_day]),
                    "event_path_holding_sessions_60d": int(
                        exit_day-entry_day+1),
                }
            return {
                "event_path_r_60d": np.nan,
                "event_path_reason_60d": "EXIT_PENDING_AT_HORIZON",
                "event_path_end_date_60d": np.nan,
                "event_path_holding_sessions_60d": np.nan,
            }
        terminal = float(self.panel.close[last_decision, column])
        if not np.isfinite(terminal):
            return {
                "event_path_r_60d": np.nan,
                "event_path_reason_60d": "MISSING_TERMINAL_MARK",
                "event_path_end_date_60d": np.nan,
                "event_path_holding_sessions_60d": np.nan,
            }
        return {
            "event_path_r_60d": (terminal-entry_adjusted)/initial_r,
            "event_path_reason_60d": "TIME_MARK_60",
            "event_path_end_date_60d": int(
                self.panel.dates[last_decision]),
            "event_path_holding_sessions_60d": 60,
        }

    def build_day(self, day, columns=None):
        day = int(day)
        entry_day = day+1
        maturity_day = day+61
        if maturity_day >= len(self.panel.dates):
            raise ValueError("policy labels are not fully mature")
        if columns is None:
            columns = np.flatnonzero(self.features.eligible(day))
        columns = np.asarray(columns, dtype=int)
        rows = []
        if not len(columns):
            return pd.DataFrame(rows)
        adjusted_close = np.asarray(self.panel.close[day, columns], float)
        raw_close = np.asarray(self.panel.exec_close[day, columns], float)
        atr = np.asarray(self.panel.atr21[day, columns], float)
        factor = raw_close/adjusted_close
        stop_adjusted = adjusted_close-self.strategy.atr_stop_multiple*atr
        stop_raw = stop_adjusted*factor
        max_buy_raw = raw_close+self.strategy.max_gap_atr*atr*factor
        base_executable = self.features._entry_executable(
            day, columns, max_buy_raw)
        raw_open = np.asarray(self.panel.exec_open[entry_day, columns], float)
        fill_raw = raw_open*(1+self.config.entry_slippage_bps/10000.0)
        fill_adjusted = fill_raw/factor
        initial_r = fill_adjusted-stop_adjusted
        executable = (
            base_executable & np.isfinite(stop_adjusted) &
            (stop_adjusted > 0) & np.isfinite(initial_r) & (initial_r > 0) &
            (fill_raw > stop_raw+1e-12))
        for position, column in enumerate(columns):
            reason = "ELIGIBLE"
            if not base_executable[position]:
                reason = "ENTRY_NOT_EXECUTABLE"
            elif not executable[position]:
                reason = "STOP_INVALIDATED"
            row = {
                "signal_asof": int(self.panel.dates[day]),
                "symbol": str(self.panel.symbols[column]),
                "column": int(column),
                "entry_date": int(self.panel.dates[entry_day]),
                "entry_executable": bool(executable[position]),
                "entry_reason": reason,
                "entry_fill_raw": (float(fill_raw[position])
                                   if executable[position] else np.nan),
                "initial_stop_raw": (float(stop_raw[position])
                                     if executable[position] else np.nan),
                "initial_r_adjusted": (float(initial_r[position])
                                       if executable[position] else np.nan),
                "event_path_r_60d": np.nan,
                "event_path_reason_60d": None,
                "event_path_end_date_60d": np.nan,
                "event_path_holding_sessions_60d": np.nan,
                "label_fully_mature_date": int(
                    self.panel.dates[maturity_day]),
                "label_version": self.config.label_version,
                "label_config_sha256": self.config_sha256,
            }
            if executable[position]:
                row.update(self._event_path(
                    entry_day, int(column), float(fill_adjusted[position]),
                    float(stop_adjusted[position]), float(initial_r[position])))
            rows.append(row)
        return pd.DataFrame(rows).sort_values(
            ["signal_asof", "symbol"], kind="mergesort").reset_index(drop=True)
