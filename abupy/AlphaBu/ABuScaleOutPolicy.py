# -*- encoding: utf-8 -*-
"""Deterministic R-multiple scale-out policy for research experiments."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class ScaleOutConfig:
    policy_id: str = "scale_out_1r_2r_v1"
    trigger_r_multiples: tuple[float, ...] = (1.0, 2.0)
    cumulative_exit_fractions: tuple[float, ...] = (0.25, 0.50)
    lot_size: int = 100

    def __post_init__(self):
        if len(self.trigger_r_multiples) != len(self.cumulative_exit_fractions):
            raise ValueError("scale-out trigger/fraction lengths must match")
        if any(value <= 0 for value in self.trigger_r_multiples):
            raise ValueError("scale-out R triggers must be positive")
        if tuple(sorted(self.trigger_r_multiples)) != self.trigger_r_multiples:
            raise ValueError("scale-out R triggers must be increasing")
        if any(not 0 < value < 1 for value in self.cumulative_exit_fractions):
            raise ValueError("cumulative scale-out fractions must be within (0, 1)")
        if tuple(sorted(self.cumulative_exit_fractions)) != \
                self.cumulative_exit_fractions:
            raise ValueError("cumulative scale-out fractions must be increasing")
        if self.lot_size <= 0:
            raise ValueError("lot size must be positive")


@dataclass
class ScaleOutState:
    reference_quantity: int
    reduced_quantity: int = 0
    completed_stages: set[int] = field(default_factory=set)
    pending_stage: int | None = None


class RMultipleScaleOutPolicy(object):
    """Sell cumulative 25%/50% after close reaches +1R/+2R.

    Signals are evaluated on the close and executed by the caller at the next
    eligible open.  The reference quantity includes later filled ADD lots.  A
    board lot is always retained for the trailing-stop remainder.
    """

    def __init__(self, config=None):
        self.config = config or ScaleOutConfig()
        self.states = {}

    @property
    def reason_codes(self):
        return tuple(
            "TAKE_PROFIT_{}R".format(
                int(value) if float(value).is_integer() else str(value).replace(".", "_"))
            for value in self.config.trigger_r_multiples)

    def register_entry(self, symbol, quantity):
        self.states[str(symbol)] = ScaleOutState(int(quantity))

    def register_add(self, symbol, quantity):
        state = self.states.get(str(symbol))
        if state is not None:
            state.reference_quantity += int(quantity)

    def remove(self, symbol):
        self.states.pop(str(symbol), None)

    def evaluate(self, symbol, close_adjusted, exit_state, current_quantity):
        state = self.states.get(str(symbol))
        if state is None or state.pending_stage is not None:
            return None
        initial_r = float(exit_state.initial_r_adjusted)
        entry = float(exit_state.initial_stop_adjusted) + initial_r
        close = float(close_adjusted)
        if not np.isfinite(close) or initial_r <= 0:
            return None
        for stage, (trigger_r, cumulative_fraction) in enumerate(zip(
                self.config.trigger_r_multiples,
                self.config.cumulative_exit_fractions)):
            if stage in state.completed_stages:
                continue
            if close < entry + trigger_r * initial_r:
                return None
            target = int(
                state.reference_quantity * cumulative_fraction /
                self.config.lot_size) * self.config.lot_size
            quantity = min(
                max(0, target - state.reduced_quantity),
                max(0, int(current_quantity) - self.config.lot_size),
            )
            quantity = int(quantity / self.config.lot_size) * self.config.lot_size
            if quantity < self.config.lot_size:
                state.completed_stages.add(stage)
                continue
            state.pending_stage = stage
            return {
                "reason": self.reason_codes[stage],
                "quantity": quantity,
                "position_effect": "REDUCE",
                "stage": stage + 1,
                "trigger_r": float(trigger_r),
                "cumulative_fraction": float(cumulative_fraction),
            }
        return None

    def record_fill(self, symbol, reason, quantity):
        state = self.states.get(str(symbol))
        if state is None or reason not in self.reason_codes:
            return
        stage = self.reason_codes.index(reason)
        state.completed_stages.add(stage)
        state.pending_stage = None
        state.reduced_quantity += int(quantity)

    def record_cancel(self, symbol):
        state = self.states.get(str(symbol))
        if state is not None:
            state.pending_stage = None
