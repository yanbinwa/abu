# -*- encoding: utf-8 -*-
"""Causal all-or-none M1/M2 minute-entry execution policies."""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import pandas as pd

from ..MarketBu.ABuRealtimeMarket import MinuteBarEvent, _as_shanghai_timestamp
from .ABuTradeIntent import (
    ApprovedOrder, IntradayExecutionInstruction, OrderEvent, make_record_id,
)


TERMINAL_STATES = frozenset(("FILLED", "CANCELLED", "EXPIRED"))


@dataclass(frozen=True)
class IntradayExecutionConfig:
    trigger_bar_end: str = "09:35:00"
    last_decision_at: str = "10:29:00"
    last_candidate_start: str = "10:30:00"
    max_volume_participation: float = 0.05
    slippage_bps: float = 25.0
    decision_latency_ms: int = 0
    order_latency_ms: int = 0
    limit_up_mode: str = "base"

    def __post_init__(self):
        if not 0 < self.max_volume_participation <= 1:
            raise ValueError("max_volume_participation must be in (0, 1]")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        if self.decision_latency_ms < 0 or self.order_latency_ms < 0:
            raise ValueError("latencies must be non-negative")
        if self.limit_up_mode not in ("base", "pessimistic"):
            raise ValueError("limit_up_mode must be base or pessimistic")
        for value in (self.trigger_bar_end, self.last_decision_at,
                      self.last_candidate_start):
            pd.Timestamp("2000-01-01T{}+08:00".format(value))


@dataclass(frozen=True)
class IntradayExecutionOutcome:
    instruction: IntradayExecutionInstruction
    state: str
    reason_code: str
    events: tuple[OrderEvent, ...]
    reference_price: float = 0.0
    fill_price_raw: float = 0.0
    decision_at: str = ""
    trigger_bar_end: str = ""
    candidate_bar_start: str = ""
    capacity_reference_bar_end: str = ""
    data_source: str = ""
    data_revision: int = 0
    available_at: str = ""


class IntradayTradability(object):
    """Minute-only tradability checks; never accesses a daily panel."""

    @staticmethod
    def candidate(event, order, config, upper_limit_raw=None):
        if not event.is_complete:
            return False, "BAR_INCOMPLETE", 0.0
        opening = float(event.open_raw)
        if opening <= 0:
            return False, "INVALID_OPEN", 0.0
        if opening > float(order.max_buy_price_raw) + 1e-12:
            return False, "ABOVE_MAX_BUY_PRICE", 0.0
        if upper_limit_raw is not None and opening >= upper_limit_raw - 1e-12:
            return False, "OPEN_AT_LIMIT_UP", 0.0
        if (config.limit_up_mode == "pessimistic" and
                upper_limit_raw is not None and
                event.high_raw >= upper_limit_raw - 1e-12):
            return False, "CANDIDATE_TOUCHED_LIMIT_UP", 0.0
        price = opening * (1 + config.slippage_bps / 10000.0)
        if price > float(order.max_buy_price_raw) + 1e-12:
            return False, "SLIPPAGE_EXCEEDS_MAX_PRICE", 0.0
        if upper_limit_raw is not None and price >= upper_limit_raw - 1e-12:
            return False, "SLIPPAGE_EXCEEDS_LIMIT", 0.0
        if order.initial_stop_raw is not None and \
                price <= float(order.initial_stop_raw) + 1e-12:
            return False, "STOP_INVALIDATED", 0.0
        return True, "ELIGIBLE", price


def instruction_from_order(order, trading_date, policy_id, config=None,
                           created_at="", reservation_id=""):
    config = config or IntradayExecutionConfig()
    return IntradayExecutionInstruction(
        instruction_id=make_record_id(
            "intraday", order.order_id, trading_date, policy_id),
        order_id=order.order_id, symbol=order.symbol,
        quantity=order.quantity, trading_date=int(trading_date),
        policy_id=policy_id,
        max_buy_price_raw=float(order.max_buy_price_raw),
        trigger_bar_end=config.trigger_bar_end,
        last_decision_at=config.last_decision_at,
        last_candidate_start=config.last_candidate_start,
        created_at=created_at, reservation_id=reservation_id,
    )


class IntradayOrderMachine(object):
    """Deterministic state machine driven only by available completed bars."""

    def __init__(self, instruction, order, config=None, upper_limit_raw=None,
                 corporate_action=False):
        if instruction.order_id != order.order_id:
            raise ValueError("instruction/order mismatch")
        self.instruction = instruction
        self.order = order
        self.config = config or IntradayExecutionConfig()
        self.upper_limit_raw = upper_limit_raw
        self.state = "CREATED"
        self.reason_code = ""
        self.events = []
        self._seen = set()
        self._candidate_after = None
        self._reference = None
        self._decision_at = None
        self._candidate = None
        self._fill_price = 0.0
        if corporate_action:
            self._transition(
                "CANCELLED", "CORPORATE_ACTION_REAPPROVAL_REQUIRED", None)
        else:
            self._transition("ACTIVE", "INSTRUCTION_ACTIVATED", None)

    def _date_time(self, clock):
        date = str(self.instruction.trading_date)
        return pd.Timestamp(
            "{}-{}-{}T{}+08:00".format(
                date[:4], date[4:6], date[6:], clock))

    def _transition(self, state, reason, event, details=None):
        if self.state in TERMINAL_STATES:
            return
        self.state = state
        self.reason_code = reason
        event_at = (
            event.available_at if event is not None else
            self.instruction.created_at or
            self._date_time("09:15:00").isoformat())
        record = OrderEvent(
            event_id=make_record_id(
                "order-event", self.instruction.instruction_id,
                len(self.events) + 1, state, reason),
            instruction_id=self.instruction.instruction_id,
            order_id=self.order.order_id, sequence=len(self.events) + 1,
            event_type=reason, state=state, event_at=event_at,
            reason_code=reason,
            reference_bar_end=(self._reference.bar_end
                               if self._reference is not None else ""),
            candidate_bar_start=(event.bar_start if event is not None and
                                 state == "FILLED" else ""),
            input_source=(event.source if event is not None else ""),
            input_revision=(event.revision if event is not None else 0),
            details=details or {},
        )
        self.events.append(record)

    def _within_decision_window(self, event):
        trigger = self._date_time(self.config.trigger_bar_end)
        last = self._date_time(self.config.last_decision_at)
        bar_end = _as_shanghai_timestamp(event.bar_end)
        decision = _as_shanghai_timestamp(event.available_at) + pd.Timedelta(
            milliseconds=self.config.decision_latency_ms)
        return (bar_end >= trigger and bar_end.floor("min") <= last and
                decision.floor("min") <= last)

    def _arm_candidate(self, event):
        decision_at = _as_shanghai_timestamp(event.available_at) + pd.Timedelta(
            milliseconds=self.config.decision_latency_ms)
        eligible = decision_at + pd.Timedelta(
            milliseconds=self.config.order_latency_ms)
        self._reference = event
        self._decision_at = decision_at
        self._candidate_after = eligible
        self._transition("CANDIDATE", "CANDIDATE_ARMED", event, {
            "execution_eligible_at": eligible.isoformat(),
            "capacity_reference_bar_end": event.bar_end,
        })

    def _reference_bar(self, event):
        if not event.is_complete or event.interval_minutes != 1:
            return
        bar_end = _as_shanghai_timestamp(event.bar_end)
        trigger = self._date_time(self.config.trigger_bar_end)
        if self.instruction.policy_id == "M1" and bar_end != trigger:
            return
        if not self._within_decision_window(event):
            return
        if self.instruction.policy_id == "M2":
            capacity = math.floor(
                event.volume_shares *
                self.config.max_volume_participation / 100.0) * 100
            if capacity < self.order.quantity:
                self._reference = event
                self._transition("ACTIVE", "WAITING_CAPACITY", event, {
                    "capacity_shares": capacity,
                    "required_shares": self.order.quantity,
                })
                return
        self._arm_candidate(event)

    def _candidate_bar(self, event):
        start = _as_shanghai_timestamp(event.bar_start)
        last = self._date_time(self.config.last_candidate_start)
        if start > last:
            self._transition("EXPIRED", "EXECUTION_WINDOW_EXPIRED", event)
            return True
        if start < self._candidate_after:
            return False
        eligible, reason, price = IntradayTradability.candidate(
            event, self.order, self.config, self.upper_limit_raw)
        if eligible:
            self._candidate = event
            self._fill_price = price
            self._transition("FILLED", "FILLED", event)
            return True
        if self.instruction.policy_id == "M1":
            self._transition("EXPIRED", reason, event)
            return True
        self._transition("ACTIVE", reason, event)
        self._candidate_after = None
        return False

    def on_bar(self, event):
        if self.state in TERMINAL_STATES:
            return self.state
        if event.symbol != self.instruction.symbol:
            return self.state
        key = (event.source, event.bar_end, event.revision)
        if key in self._seen:
            return self.state
        self._seen.add(key)
        if not event.is_complete:
            return self.state
        if self.state == "CANDIDATE" and self._candidate_after is not None:
            if self._candidate_bar(event):
                return self.state
        if self.state not in TERMINAL_STATES and self.state != "CANDIDATE":
            self._reference_bar(event)
        return self.state

    def finalize(self):
        if self.state not in TERMINAL_STATES:
            self._transition("EXPIRED", "EXECUTION_WINDOW_EXPIRED", None)
        return self.outcome()

    def outcome(self):
        candidate = self._candidate
        return IntradayExecutionOutcome(
            instruction=self.instruction, state=self.state,
            reason_code=self.reason_code, events=tuple(self.events),
            reference_price=(candidate.open_raw if candidate else 0.0),
            fill_price_raw=self._fill_price,
            decision_at=(self._decision_at.isoformat()
                         if self._decision_at is not None else ""),
            trigger_bar_end=(self._reference.bar_end
                             if self._reference is not None else ""),
            candidate_bar_start=(candidate.bar_start if candidate else ""),
            capacity_reference_bar_end=(self._reference.bar_end
                                        if self._reference is not None else ""),
            data_source=(candidate.source if candidate else
                         self._reference.source if self._reference else ""),
            data_revision=(candidate.revision if candidate else
                           self._reference.revision if self._reference else 0),
            available_at=(candidate.available_at if candidate else
                          self._reference.available_at if self._reference else ""),
        )

    def export_state(self):
        """Return the complete deterministic state needed for incremental resume."""
        return {
            "schema_version": "intraday_order_machine_state_v2",
            "instruction": asdict(self.instruction),
            "order": asdict(self.order),
            "config": asdict(self.config),
            "upper_limit_raw": self.upper_limit_raw,
            "state": self.state,
            "reason_code": self.reason_code,
            "events": [asdict(item) for item in self.events],
            "seen": [list(item) for item in sorted(self._seen)],
            "candidate_after": (
                None if self._candidate_after is None else
                self._candidate_after.isoformat()),
            "reference": (
                None if self._reference is None else asdict(self._reference)),
            "decision_at": (
                None if self._decision_at is None else
                self._decision_at.isoformat()),
            "candidate": (
                None if self._candidate is None else asdict(self._candidate)),
            "fill_price": self._fill_price,
        }

    @classmethod
    def restore(cls, instruction, order, state, config=None,
                upper_limit_raw=None):
        """Restore without replaying already consumed minute bars."""
        if state.get("schema_version") not in (
                None, "intraday_order_machine_state_v1",
                "intraday_order_machine_state_v2"):
            raise ValueError("intraday machine state version mismatch")
        if instruction.order_id != order.order_id:
            raise ValueError("instruction/order mismatch")
        if state.get("schema_version") == "intraday_order_machine_state_v2":
            if state.get("instruction") != asdict(instruction):
                raise ValueError("persisted intraday instruction mismatch")
            if state.get("order") != asdict(order):
                raise ValueError("persisted approved order mismatch")
            frozen_config = IntradayExecutionConfig(**state["config"])
            if config is not None and config != frozen_config:
                raise ValueError("persisted intraday config mismatch")
            config = frozen_config
            frozen_limit = state.get("upper_limit_raw")
            if (upper_limit_raw is not None and
                    upper_limit_raw != frozen_limit):
                raise ValueError("persisted upper limit mismatch")
            upper_limit_raw = frozen_limit
        valid_states = frozenset((
            "CREATED", "ACTIVE", "CANDIDATE", "FILLED", "CANCELLED",
            "EXPIRED"))
        if state.get("state") not in valid_states:
            raise ValueError("invalid persisted intraday state")

        def minute_event(payload):
            if payload is None:
                return None
            value = dict(payload)
            value["quality_codes"] = tuple(value.get("quality_codes", ()))
            return MinuteBarEvent(**value)

        machine = cls.__new__(cls)
        machine.instruction = instruction
        machine.order = order
        machine.config = config or IntradayExecutionConfig()
        machine.upper_limit_raw = upper_limit_raw
        machine.state = state["state"]
        machine.reason_code = state.get("reason_code", "")
        machine.events = [OrderEvent(**item) for item in state.get("events", ())]
        if any(item.sequence != index + 1
               for index, item in enumerate(machine.events)):
            raise ValueError("persisted order event sequence is not contiguous")
        machine._seen = {tuple(item) for item in state.get("seen", ())}
        machine._candidate_after = (
            None if state.get("candidate_after") is None else
            pd.Timestamp(state["candidate_after"]))
        machine._reference = minute_event(state.get("reference"))
        machine._decision_at = (
            None if state.get("decision_at") is None else
            pd.Timestamp(state["decision_at"]))
        machine._candidate = minute_event(state.get("candidate"))
        machine._fill_price = float(state.get("fill_price", 0.0))
        if machine.events and machine.events[-1].state != machine.state:
            raise ValueError("persisted machine state does not match last event")
        return machine

    @classmethod
    def restore_exported(cls, state):
        """Restore a v2 state using only its frozen event payload."""
        if state.get("schema_version") != "intraday_order_machine_state_v2":
            raise ValueError("self-contained intraday state v2 is required")
        instruction = IntradayExecutionInstruction(**state["instruction"])
        order = ApprovedOrder(**state["order"])
        config = IntradayExecutionConfig(**state["config"])
        return cls.restore(
            instruction, order, state, config=config,
            upper_limit_raw=state.get("upper_limit_raw"))


def simulate_intraday_order(instruction, order, bars, config=None,
                            upper_limit_raw=None, corporate_action=False,
                            finalize=True):
    machine = IntradayOrderMachine(
        instruction, order, config=config, upper_limit_raw=upper_limit_raw,
        corporate_action=corporate_action)
    for event in sorted(bars, key=lambda item: (
            _as_shanghai_timestamp(item.available_at),
            _as_shanghai_timestamp(item.bar_end), item.revision)):
        machine.on_bar(event)
    return machine.finalize() if finalize else machine.outcome()


def apply_intraday_outcome(executor, order, day, outcome):
    """Commit a simulated/shadow outcome through the shared account ledger."""
    if outcome.state == "FILLED":
        return executor.apply_buy_fill(
            order, day, reference_price=outcome.reference_price,
            fill_price_raw=outcome.fill_price_raw,
            execution_policy_id=outcome.instruction.policy_id,
            decision_at=outcome.decision_at,
            trigger_bar_end=outcome.trigger_bar_end,
            candidate_bar_start=outcome.candidate_bar_start,
            capacity_reference_bar_end=outcome.capacity_reference_bar_end,
            data_source=outcome.data_source,
            data_revision=outcome.data_revision,
            available_at=outcome.available_at)
    status = "cancelled" if outcome.state == "CANCELLED" else "expired"
    return executor.cancel_buy_order(
        order, day, outcome.reason_code, status=status,
        execution_policy_id=outcome.instruction.policy_id,
        decision_at=outcome.decision_at,
        trigger_bar_end=outcome.trigger_bar_end,
        candidate_bar_start=outcome.candidate_bar_start,
        data_source=outcome.data_source,
        data_revision=outcome.data_revision,
        available_at=outcome.available_at)
