# -*- encoding: utf-8 -*-
"""Frozen research overlays for Alpha158 event exits."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json

import numpy as np

from .ABuAlpha158Lite import Alpha158LiteExitEngine


@dataclass(frozen=True)
class AlphaExitOverlayConfig:
    policy_id: str = "alpha_exit_overlay_v1"
    breakeven_floor_enabled: bool = False
    breakeven_activation_r: float = 1.0
    breakeven_cost_buffer_bps: float = 60.0
    rolling_stagnation_enabled: bool = False
    rolling_min_holding_sessions: int = 30
    rolling_no_new_peak_sessions: int = 15
    rolling_require_below_ma20: bool = True

    def __post_init__(self):
        if self.breakeven_activation_r <= 0:
            raise ValueError("breakeven activation must be positive")
        if self.breakeven_cost_buffer_bps < 0:
            raise ValueError("breakeven cost buffer cannot be negative")
        if self.rolling_min_holding_sessions <= 0:
            raise ValueError("rolling holding threshold must be positive")
        if self.rolling_no_new_peak_sessions <= 0:
            raise ValueError("rolling peak threshold must be positive")

    @property
    def sha256(self):
        payload = json.dumps(
            asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class Alpha158ExitOverlayEngine(Alpha158LiteExitEngine):
    """Preserve the base exits and add pre-registered protective rules.

    The cost floor is activated only after the position reaches the configured
    R multiple.  Rolling stagnation applies only before the trailing stop has
    activated, so it cannot cut an already established winner merely because
    its last peak is old.
    """

    def __init__(self, panel, config, overlay_config=None):
        super().__init__(panel, config)
        self.overlay = overlay_config or AlphaExitOverlayConfig()
        self.last_peak_day = {}

    def register_entry(self, intent, fill, day):
        super().register_entry(intent, fill, day)
        self.last_peak_day[intent.symbol] = int(day)

    def signal(self, day, symbol):
        state = self.states[symbol]
        column = self.panel.symbol_index[symbol]
        close = float(self.panel.close[day, column])
        if not np.isfinite(close):
            return None

        if close > state.peak_close_adjusted:
            state.peak_close_adjusted = close
            self.last_peak_day[symbol] = int(day)
        entry = state.initial_stop_adjusted + state.initial_r_adjusted
        mfe = state.peak_close_adjusted-entry
        if (state.initial_r_adjusted > 0 and
                mfe >= self.config.trailing_activation_r *
                state.initial_r_adjusted):
            state.trailing_enabled = True

        atr = float(self.panel.atr21[day, column])
        if state.trailing_enabled and np.isfinite(atr):
            state.current_stop_adjusted = max(
                state.current_stop_adjusted,
                state.peak_close_adjusted-
                self.config.trailing_atr_multiple*atr)
        if (self.overlay.breakeven_floor_enabled and
                state.initial_r_adjusted > 0 and
                mfe >= self.overlay.breakeven_activation_r *
                state.initial_r_adjusted):
            cost_floor = entry * (
                1+self.overlay.breakeven_cost_buffer_bps/10000.0)
            state.current_stop_adjusted = max(
                state.current_stop_adjusted, cost_floor)

        held = int(day)-state.entry_day+1
        if close <= state.initial_stop_adjusted:
            return "INITIAL_STOP"
        if state.trailing_enabled and close <= state.current_stop_adjusted:
            return "TRAILING_STOP"
        if (self.overlay.rolling_stagnation_enabled and
                not state.trailing_enabled and
                held >= self.overlay.rolling_min_holding_sessions and
                int(day)-self.last_peak_day.get(symbol, state.entry_day) >=
                self.overlay.rolling_no_new_peak_sessions):
            ma20 = float(self.panel.ma10[day, column])
            # The base panel exposes MA10/MA60.  Calculate a causal MA20 from
            # the already observed close window when the rule requires it.
            start = max(0, int(day)-19)
            window = np.asarray(self.panel.close[start:int(day)+1, column],
                                dtype=float)
            valid = window[np.isfinite(window)]
            if len(valid) == 20:
                ma20 = float(valid.mean())
            below = (close < ma20 if
                     self.overlay.rolling_require_below_ma20 else True)
            if below:
                return "ROLLING_STAGNATION"
        if (held >= self.config.stagnation_sessions and
                mfe < self.config.stagnation_mfe_r *
                state.initial_r_adjusted):
            return "STAGNATION"
        return None

    def remove(self, symbol):
        super().remove(symbol)
        self.last_peak_day.pop(symbol, None)
