# -*- encoding: utf-8 -*-
"""Execution-aware research labels kept outside the live decision path."""
from __future__ import annotations

import ast
import hashlib
import json

import numpy as np
import pandas as pd


VCP_LABEL_VERSION = "vcp_execution_labels_v1"
VCP_LABEL_VERSION_V2 = "vcp_execution_labels_v2"


def _metadata(value):
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = ast.literal_eval(str(value))
        return parsed if isinstance(parsed, dict) else {}
    except (SyntaxError, ValueError, TypeError):
        return {}


class VCPLabelBuilder(object):
    """Build labels whose entry begins at the next executable open."""

    def __init__(self, panel, horizons=(5, 20),
                 label_version=VCP_LABEL_VERSION):
        self.panel = panel
        self.horizons = tuple(sorted(set(int(item) for item in horizons)))
        if not self.horizons or min(self.horizons) <= 0:
            raise ValueError("horizons must be positive")
        self.label_version = str(label_version)
        self._date_index = {int(value): position
                            for position, value in enumerate(panel.dates)}
        semantics = {
            "label_version": self.label_version,
            "horizons": self.horizons,
            "entry": "t_plus_1_adjusted_open_if_executable",
            "false_breakout": "close_below_frozen_breakout_before_plus_1r",
        }
        if self.label_version == VCP_LABEL_VERSION_V2:
            semantics["event_path"] = "stop_trailing_stagnation_or_mark_60"
        self.config_sha256 = hashlib.sha256(json.dumps(
            semantics, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def _row(self, intent):
        day = self._date_index.get(int(intent.signal_asof))
        column = self.panel.symbol_index.get(str(intent.symbol))
        if day is None or column is None or day + 1 >= len(self.panel.dates):
            return None
        entry_day = day + 1
        metadata = _metadata(intent.metadata)
        raw_open = float(self.panel.exec_open[entry_day, column])
        adjusted_open = float(self.panel.open[entry_day, column])
        max_buy = float(metadata.get("max_buy_price_raw", np.nan))
        executable = bool(
            self.panel.buy_tradable_mask[entry_day, column] and
            np.isfinite(raw_open) and raw_open > 0 and
            np.isfinite(adjusted_open) and adjusted_open > 0 and
            (not np.isfinite(max_buy) or raw_open <= max_buy)
        )
        row = {
            "intent_id": intent.intent_id,
            "signal_asof": int(intent.signal_asof),
            "symbol": str(intent.symbol),
            "entry_date": int(self.panel.dates[entry_day]),
            "entry_executable": executable,
            "label_version": self.label_version,
            "label_config_sha256": self.config_sha256,
        }
        for horizon in self.horizons:
            row["return_{}d".format(horizon)] = np.nan
            row["excess_return_{}d".format(horizon)] = np.nan
        row.update({
            "mfe_20_r": np.nan, "mae_20_r": np.nan,
            "hit_plus_1r_20d": np.nan, "false_breakout_5d": np.nan,
        })
        if self.label_version == VCP_LABEL_VERSION_V2:
            row.update({
                "event_path_r_60d": np.nan, "event_path_reason_60d": None,
                "event_path_end_date_60d": np.nan,
                "event_path_holding_sessions_60d": np.nan,
            })
        if not executable:
            return row
        for horizon in self.horizons:
            exit_day = day + horizon
            if exit_day >= len(self.panel.dates):
                continue
            exit_close = float(self.panel.close[exit_day, column])
            benchmark_entry = float(self.panel.benchmark_open[entry_day])
            benchmark_exit = float(self.panel.benchmark_close[exit_day])
            if np.isfinite(exit_close) and exit_close > 0:
                value = exit_close / adjusted_open - 1
                row["return_{}d".format(horizon)] = value
                if (np.isfinite(benchmark_entry) and benchmark_entry > 0 and
                        np.isfinite(benchmark_exit)):
                    row["excess_return_{}d".format(horizon)] = (
                        value - (benchmark_exit / benchmark_entry - 1))
        stop = float(intent.initial_stop_adjusted)
        initial_r = adjusted_open - stop
        end20 = min(day + 21, len(self.panel.dates))
        future_high = np.asarray(
            self.panel.high[entry_day:end20, column], dtype=float)
        future_low = np.asarray(
            self.panel.low[entry_day:end20, column], dtype=float)
        if initial_r > 0 and len(future_high):
            row["mfe_20_r"] = float(
                (np.nanmax(future_high)-adjusted_open) / initial_r)
            row["mae_20_r"] = float(
                (np.nanmin(future_low)-adjusted_open) / initial_r)
            row["hit_plus_1r_20d"] = bool(
                np.nanmax(future_high) >= adjusted_open + initial_r)
        first5_end = min(day + 6, len(self.panel.dates))
        breakout = float(metadata.get("breakout_level", np.nan))
        first5_close = np.asarray(
            self.panel.close[entry_day:first5_end, column], dtype=float)
        first5_high = np.asarray(
            self.panel.high[entry_day:first5_end, column], dtype=float)
        if len(first5_close) and np.isfinite(breakout) and initial_r > 0:
            hit_1r = bool(np.nanmax(first5_high) >= adjusted_open + initial_r)
            row["false_breakout_5d"] = bool(
                np.nanmin(first5_close) < breakout and not hit_1r)
        if self.label_version == VCP_LABEL_VERSION_V2:
            event = self._event_path_60d(entry_day, column, adjusted_open,
                                         stop, initial_r)
            row.update(event)
        return row

    def _event_path_60d(self, entry_day, column, entry, initial_stop,
                        initial_r):
        """Approximate the frozen stop/trailing/stagnation exit in R units."""
        empty = {
            "event_path_r_60d": np.nan, "event_path_reason_60d": None,
            "event_path_end_date_60d": np.nan,
            "event_path_holding_sessions_60d": np.nan,
        }
        end = entry_day + 59
        if initial_r <= 0:
            return empty
        peak = float(entry); current_stop = float(initial_stop)
        trailing = False
        for day in range(entry_day, min(end + 1, len(self.panel.dates)-1)):
            close = float(self.panel.close[day, column])
            atr = float(self.panel.atr21[day, column])
            if not np.isfinite(close):
                continue
            peak = max(peak, close)
            mfe = peak-entry
            if mfe >= initial_r:
                trailing = True
            if trailing and np.isfinite(atr):
                current_stop = max(current_stop, peak-3*atr)
            held = day-entry_day+1
            reason = None
            if close <= initial_stop:
                reason = "INITIAL_STOP"
            elif trailing and close <= current_stop:
                reason = "TRAILING_STOP"
            elif held >= 20 and mfe < .5*initial_r:
                reason = "STAGNATION"
            if reason is None:
                continue
            for exit_day in range(day+1, min(end+2, len(self.panel.dates))):
                if not self.panel.sell_tradable_mask[exit_day, column]:
                    continue
                exit_open = float(self.panel.open[exit_day, column])
                if np.isfinite(exit_open) and exit_open > 0:
                    return {
                        "event_path_r_60d": (exit_open-entry)/initial_r,
                        "event_path_reason_60d": reason,
                        "event_path_end_date_60d": int(
                            self.panel.dates[exit_day]),
                        "event_path_holding_sessions_60d": int(
                            exit_day-entry_day+1),
                    }
            return empty
        if end >= len(self.panel.dates):
            return empty
        terminal = float(self.panel.close[end, column])
        if not np.isfinite(terminal):
            return empty
        return {
            "event_path_r_60d": (terminal-entry)/initial_r,
            "event_path_reason_60d": "TIME_MARK_60",
            "event_path_end_date_60d": int(self.panel.dates[end]),
            "event_path_holding_sessions_60d": 60,
        }

    def build(self, intents):
        rows = [self._row(intent) for intent in intents]
        rows = [row for row in rows if row is not None]
        return pd.DataFrame(rows).sort_values(
            ["signal_asof", "symbol"], kind="mergesort").reset_index(drop=True)
