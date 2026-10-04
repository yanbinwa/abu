# -*- encoding: utf-8 -*-
"""Auditable position-add policy protocol and frozen v1 policies."""
from __future__ import annotations

import hashlib
import json
from abc import ABCMeta, abstractmethod
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .ABuPositionLedger import (
    LogicalTrade, PhysicalPosition, PositionLot, evaluation_id,
    logical_add_order_id, proposal_id,
)


def _config_sha256(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class PositionAddContext:
    signal_asof: int
    valid_session: int
    trade_snapshot: LogicalTrade
    physical_position_snapshot: PhysicalPosition
    lot_snapshots: tuple[PositionLot, ...]
    pending_order_snapshots: tuple[Any, ...] = ()
    adjusted_market_window: Mapping[str, Any] = field(default_factory=dict)
    raw_execution_snapshot: Mapping[str, Any] = field(default_factory=dict)
    portfolio_risk_snapshot: Mapping[str, Any] = field(default_factory=dict)
    base_signal_status: str = "HOLD"
    field_coverage: Mapping[str, bool] = field(default_factory=dict)
    data_version: str = ""


@dataclass(frozen=True)
class AddProposal:
    proposal_id: str
    evaluation_id: str
    trade_id: str
    allocation_id: str
    symbol: str
    signal_asof: int
    add_sequence: int
    trigger_code: str
    risk_budget_cash_cap: float
    notional_cash_cap: float
    current_stop_raw_snapshot: float
    max_buy_price_raw: float
    valid_session: int
    priority: int
    quantity_cap_optional: int | None = None
    reason_codes: tuple[str, ...] = ()
    logical_order_id: str = ""
    schema_version: str = "position_add_policy_v1"

    def __post_init__(self):
        if self.valid_session <= self.signal_asof:
            raise ValueError("ADD is valid only on the next session")
        if self.add_sequence <= 0:
            raise ValueError("add_sequence must be positive")
        if self.risk_budget_cash_cap < 0 or self.notional_cash_cap < 0:
            raise ValueError("proposal caps must be non-negative")


@dataclass(frozen=True)
class PolicyEvaluation:
    evaluation_id: str
    policy_id: str
    policy_version: str
    trade_id: str
    signal_asof: int
    triggered: bool
    evaluated_inputs: Mapping[str, Any]
    reason_codes: tuple[str, ...]
    missing_fields: tuple[str, ...]
    config_sha256: str
    data_version: str
    proposal: AddProposal | None = None
    schema_version: str = "position_add_policy_v1"


class PositionAddPolicy(metaclass=ABCMeta):
    policy_id = "abstract"
    policy_version = "0"

    @property
    @abstractmethod
    def config_sha256(self):
        raise NotImplementedError

    @abstractmethod
    def evaluate(self, context: PositionAddContext) -> PolicyEvaluation:
        raise NotImplementedError


@dataclass(frozen=True)
class NoAddConfig:
    policy_id: str = "no_add"
    policy_version: str = "no_add_v1"

    @property
    def sha256(self):
        return _config_sha256(asdict(self))


class NoAddPolicy(PositionAddPolicy):
    def __init__(self, config=None):
        self.config = config or NoAddConfig()
        self.policy_id = self.config.policy_id
        self.policy_version = self.config.policy_version

    @property
    def config_sha256(self):
        return self.config.sha256

    def evaluate(self, context):
        identity = evaluation_id(
            context.trade_snapshot.trade_id, context.signal_asof,
            self.policy_id, self.policy_version)
        return PolicyEvaluation(
            evaluation_id=identity, policy_id=self.policy_id,
            policy_version=self.policy_version,
            trade_id=context.trade_snapshot.trade_id,
            signal_asof=context.signal_asof, triggered=False,
            evaluated_inputs={},
            reason_codes=("POLICY_DISABLED_BY_DEFINITION",),
            missing_fields=(), config_sha256=self.config_sha256,
            data_version=context.data_version, proposal=None,
        )


def load_no_add_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = set(NoAddConfig.__dataclass_fields__)
    if set(payload) != expected:
        raise ValueError("NoAdd config fields mismatch")
    return NoAddConfig(**payload)


class PositionAddPolicyRunner(object):
    """Idempotent evaluator; order creation remains an external concern."""

    def __init__(self, policy):
        self.policy = policy
        self.evaluations = {}
        self.proposals = {}

    def evaluate(self, context):
        key = evaluation_id(
            context.trade_snapshot.trade_id, context.signal_asof,
            self.policy.policy_id, self.policy.policy_version)
        if key in self.evaluations:
            return self.evaluations[key]
        result = self.policy.evaluate(context)
        if result.evaluation_id != key:
            raise ValueError("policy returned a non-deterministic evaluation_id")
        self.evaluations[key] = result
        if result.proposal is not None:
            existing = self.proposals.get(result.proposal.logical_order_id)
            if existing is not None and existing != result.proposal:
                raise ValueError("DUPLICATE_ADD_REQUEST")
            self.proposals[result.proposal.logical_order_id] = result.proposal
        return result


@dataclass(frozen=True)
class ProtectedWinnerConfig:
    policy_id: str = "protected_winner"
    policy_version: str = "protected_winner_v1"
    min_holding_sessions: int = 5
    min_sessions_since_last_fill: int = 5
    min_profit_r: float = 1.0
    min_move_atr21: float = 0.5
    add_risk_budget_fraction: float = 0.00125
    max_add_notional_fraction: float = 0.02
    max_add_count: int = 1
    max_gap_fraction: float = 0.03
    broker_rate: float = 0.00025
    min_commission: float = 5.0
    transfer_rate: float = 0.00001
    sell_stamp_rate: float = 0.0005
    exit_slippage_bps: float = 25.0
    priority: int = 100

    @property
    def sha256(self):
        return _config_sha256(asdict(self))


def breakeven_price_raw_including_costs(quantity, book_cost_cash, config):
    """Raw quote whose modeled liquidation proceeds cover active book cost."""
    if quantity <= 0 or book_cost_cash <= 0:
        raise ValueError("positive quantity and book cost are required")

    def proceeds(quote):
        execution = quote * (1-config.exit_slippage_bps/10000.0)
        gross = quantity * execution
        commission = max(gross*config.broker_rate, config.min_commission)
        return gross-commission-gross*config.transfer_rate-gross*config.sell_stamp_rate

    low = book_cost_cash/quantity
    high = low*2.0+1.0
    while proceeds(high) < book_cost_cash:
        high *= 2.0
    for _ in range(80):
        middle = (low+high)/2.0
        if proceeds(middle) >= book_cost_cash:
            high = middle
        else:
            low = middle
    return high


class ProtectedWinnerPolicy(PositionAddPolicy):
    REQUIRED_MARKET_FIELDS = (
        "close_adjusted", "atr21_adjusted", "last_fill_price_adjusted")
    REQUIRED_RAW_FIELDS = ("close_raw",)
    REQUIRED_RISK_FIELDS = ("portfolio_equity_asof",)

    def __init__(self, config=None):
        self.config = config or ProtectedWinnerConfig()
        self.policy_id = self.config.policy_id
        self.policy_version = self.config.policy_version

    @property
    def config_sha256(self):
        return self.config.sha256

    def evaluate(self, context):
        trade = context.trade_snapshot
        identity = evaluation_id(
            trade.trade_id, context.signal_asof,
            self.policy_id, self.policy_version)
        missing = []
        for name in self.REQUIRED_MARKET_FIELDS:
            if context.adjusted_market_window.get(name) is None:
                missing.append(name)
        for name in self.REQUIRED_RAW_FIELDS:
            if context.raw_execution_snapshot.get(name) is None:
                missing.append(name)
        for name in self.REQUIRED_RISK_FIELDS:
            if context.portfolio_risk_snapshot.get(name) is None:
                missing.append(name)
        required_trade = {
            "initial_entry_price_adjusted": trade.initial_entry_price_adjusted,
            "initial_r_per_share_adjusted": trade.initial_r_per_share_adjusted,
            "current_stop_raw": trade.current_stop_raw,
            "entry_session_index": trade.entry_session_index,
            "last_buy_fill_session_index": trade.last_buy_fill_session_index,
        }
        missing.extend(name for name, value in required_trade.items()
                       if value is None)
        inputs = {
            **required_trade,
            **dict(context.adjusted_market_window),
            **dict(context.raw_execution_snapshot),
            **dict(context.portfolio_risk_snapshot),
            "base_signal_status": context.base_signal_status,
            "add_count": trade.add_count,
            "status": trade.status,
            "pending_add_count": len(trade.pending_add_order_ids),
            "pending_exit_count": len(trade.pending_exit_order_ids),
        }
        if missing:
            return PolicyEvaluation(
                identity, self.policy_id, self.policy_version, trade.trade_id,
                context.signal_asof, False, inputs, ("MISSING_REQUIRED_FIELD",),
                tuple(sorted(set(missing))), self.config_sha256,
                context.data_version)
        current_index = int(context.portfolio_risk_snapshot.get(
            "session_index", trade.last_buy_fill_session_index))
        holding = current_index-int(trade.entry_session_index)+1
        since_fill = current_index-int(trade.last_buy_fill_session_index)
        close_adjusted = float(context.adjusted_market_window["close_adjusted"])
        atr = float(context.adjusted_market_window["atr21_adjusted"])
        last_fill_adjusted = float(
            context.adjusted_market_window["last_fill_price_adjusted"])
        quantity = context.physical_position_snapshot.total_quantity
        breakeven = breakeven_price_raw_including_costs(
            quantity, context.physical_position_snapshot.remaining_book_cost_cash,
            self.config)
        inputs.update({"holding_sessions": holding,
                       "sessions_since_last_fill": since_fill,
                       "breakeven_price_raw_including_costs": breakeven})
        failures = []
        checks = (
            (trade.status == "ACTIVE", "TRADE_NOT_ACTIVE"),
            (trade.add_count < min(trade.max_add_count,
                                   self.config.max_add_count), "ADD_LIMIT_REACHED"),
            (holding >= self.config.min_holding_sessions, "HOLDING_TOO_SHORT"),
            (since_fill >= self.config.min_sessions_since_last_fill,
             "LAST_FILL_TOO_RECENT"),
            (context.base_signal_status == "HOLD", "BASE_SIGNAL_NOT_HOLD"),
            (close_adjusted >= float(trade.initial_entry_price_adjusted) +
             self.config.min_profit_r*float(trade.initial_r_per_share_adjusted),
             "PROFIT_BELOW_1R"),
            (close_adjusted >= last_fill_adjusted +
             self.config.min_move_atr21*atr, "MOVE_BELOW_ATR_THRESHOLD"),
            (float(trade.current_stop_raw) >= breakeven,
             "STOP_BELOW_BREAKEVEN"),
            (not trade.pending_add_order_ids, "PENDING_ADD_ORDER"),
            (not trade.pending_exit_order_ids, "PENDING_EXIT_ORDER"),
        )
        failures.extend(code for passed, code in checks if not passed)
        if failures:
            return PolicyEvaluation(
                identity, self.policy_id, self.policy_version, trade.trade_id,
                context.signal_asof, False, inputs, tuple(failures), (),
                self.config_sha256, context.data_version)
        sequence = trade.add_count+1
        equity = float(context.portfolio_risk_snapshot["portfolio_equity_asof"])
        entry_equity = float(trade.entry_equity_cash or equity)
        max_price = float(context.raw_execution_snapshot["close_raw"])*(
            1+self.config.max_gap_fraction)
        proposal = AddProposal(
            proposal_id=proposal_id(identity, sequence),
            evaluation_id=identity, trade_id=trade.trade_id,
            allocation_id=trade.allocation_id, symbol=trade.symbol,
            signal_asof=context.signal_asof, add_sequence=sequence,
            trigger_code="STOP_LEVEL_AT_BREAKEVEN",
            risk_budget_cash_cap=entry_equity*self.config.add_risk_budget_fraction,
            notional_cash_cap=equity*self.config.max_add_notional_fraction,
            current_stop_raw_snapshot=float(trade.current_stop_raw),
            max_buy_price_raw=max_price, valid_session=context.valid_session,
            priority=self.config.priority,
            reason_codes=("STOP_LEVEL_AT_BREAKEVEN",),
            logical_order_id=logical_add_order_id(
                trade.trade_id, context.signal_asof, sequence),
        )
        return PolicyEvaluation(
            identity, self.policy_id, self.policy_version, trade.trade_id,
            context.signal_asof, True, inputs,
            ("STOP_LEVEL_AT_BREAKEVEN",), (), self.config_sha256,
            context.data_version, proposal)


def load_protected_winner_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = set(ProtectedWinnerConfig.__dataclass_fields__)
    if set(payload) != expected:
        raise ValueError("ProtectedWinner config fields mismatch")
    return ProtectedWinnerConfig(**payload)


@dataclass(frozen=True)
class RebreakoutConfig:
    policy_id: str = "rebreakout"
    policy_version: str = "rebreakout_v1"
    lookback_sessions: int = 20
    add_risk_budget_fraction: float = 0.00125
    max_add_notional_fraction: float = 0.02
    max_add_count: int = 1
    max_gap_fraction: float = 0.03
    priority: int = 80

    @property
    def sha256(self):
        return _config_sha256(asdict(self))


@dataclass(frozen=True)
class TurtleAtrConfig:
    policy_id: str = "turtle_atr"
    policy_version: str = "turtle_atr_add_v1"
    atr_multiple: float = 0.5
    add_risk_budget_fraction: float = 0.00125
    max_add_notional_fraction: float = 0.02
    max_add_count: int = 1
    max_gap_fraction: float = 0.03
    priority: int = 70

    @property
    def sha256(self):
        return _config_sha256(asdict(self))


def _simple_add_evaluation(policy, context, triggered, trigger_code,
                           extra_inputs=(), failure_code="CONDITION_NOT_MET"):
    trade = context.trade_snapshot
    identity = evaluation_id(
        trade.trade_id, context.signal_asof,
        policy.policy_id, policy.policy_version)
    inputs = dict(extra_inputs)
    required = {
        "close_raw": context.raw_execution_snapshot.get("close_raw"),
        "portfolio_equity_asof": context.portfolio_risk_snapshot.get(
            "portfolio_equity_asof"),
        "current_stop_raw": trade.current_stop_raw,
    }
    missing = tuple(sorted(name for name, value in required.items()
                           if value is None or not np.isfinite(value)))
    blockers = []
    if trade.status != "ACTIVE":
        blockers.append("TRADE_NOT_ACTIVE")
    if trade.add_count >= min(trade.max_add_count, policy.config.max_add_count):
        blockers.append("ADD_LIMIT_REACHED")
    if trade.pending_add_order_ids:
        blockers.append("PENDING_ADD_ORDER")
    if trade.pending_exit_order_ids:
        blockers.append("PENDING_EXIT_ORDER")
    if context.base_signal_status != "HOLD":
        blockers.append("BASE_SIGNAL_NOT_HOLD")
    if missing:
        blockers.append("MISSING_REQUIRED_FIELD")
    if not triggered:
        blockers.append(failure_code)
    if blockers:
        return PolicyEvaluation(
            identity, policy.policy_id, policy.policy_version, trade.trade_id,
            context.signal_asof, False, {**inputs, **required},
            tuple(blockers), missing, policy.config_sha256,
            context.data_version)
    sequence = trade.add_count+1
    equity = float(required["portfolio_equity_asof"])
    entry_equity = float(trade.entry_equity_cash or equity)
    max_price = float(required["close_raw"])*(1+policy.config.max_gap_fraction)
    proposal = AddProposal(
        proposal_id=proposal_id(identity, sequence), evaluation_id=identity,
        trade_id=trade.trade_id, allocation_id=trade.allocation_id,
        symbol=trade.symbol, signal_asof=context.signal_asof,
        add_sequence=sequence, trigger_code=trigger_code,
        risk_budget_cash_cap=entry_equity*policy.config.add_risk_budget_fraction,
        notional_cash_cap=equity*policy.config.max_add_notional_fraction,
        current_stop_raw_snapshot=float(required["current_stop_raw"]),
        max_buy_price_raw=max_price, valid_session=context.valid_session,
        priority=policy.config.priority, reason_codes=(trigger_code,),
        logical_order_id=logical_add_order_id(
            trade.trade_id, context.signal_asof, sequence))
    return PolicyEvaluation(
        identity, policy.policy_id, policy.policy_version, trade.trade_id,
        context.signal_asof, True, {**inputs, **required}, (trigger_code,), (),
        policy.config_sha256, context.data_version, proposal)


class RebreakoutPolicy(PositionAddPolicy):
    def __init__(self, config=None):
        self.config = config or RebreakoutConfig()
        self.policy_id = self.config.policy_id
        self.policy_version = self.config.policy_version

    @property
    def config_sha256(self):
        return self.config.sha256

    def evaluate(self, context):
        close = context.adjusted_market_window.get("close_adjusted")
        prior = context.adjusted_market_window.get("prior20_high_adjusted")
        valid = (close is not None and prior is not None and
                 np.isfinite(close) and np.isfinite(prior))
        return _simple_add_evaluation(
            self, context, bool(valid and float(close) > float(prior)),
            "REBROKE_PRIOR_20_HIGH",
            (("close_adjusted", close), ("prior20_high_adjusted", prior),
             ("window_excludes_signal_day", True)),
            "NO_REBREAKOUT")


class TurtleAtrPolicy(PositionAddPolicy):
    def __init__(self, config=None):
        self.config = config or TurtleAtrConfig()
        self.policy_id = self.config.policy_id
        self.policy_version = self.config.policy_version

    @property
    def config_sha256(self):
        return self.config.sha256

    def evaluate(self, context):
        close = context.adjusted_market_window.get("close_adjusted")
        last_fill = context.adjusted_market_window.get("last_fill_price_adjusted")
        atr = context.adjusted_market_window.get("atr21_adjusted")
        valid = all(value is not None and np.isfinite(value)
                    for value in (close, last_fill, atr))
        threshold = (float(last_fill)+self.config.atr_multiple*float(atr)
                     if valid else None)
        return _simple_add_evaluation(
            self, context, bool(valid and float(close) >= threshold),
            "TURTLE_ATR_ADVANCE",
            (("close_adjusted", close),
             ("last_fill_price_adjusted", last_fill),
             ("atr21_adjusted", atr), ("trigger_threshold", threshold)),
            "ATR_ADVANCE_NOT_MET")


class CompositePositionAddPolicy(PositionAddPolicy):
    """Deterministic ALL_OF/ANY_OF/PRIORITY proposal arbiter."""

    def __init__(self, policies, mode="ALL_OF", policy_id="composite_v1"):
        if mode not in ("ALL_OF", "ANY_OF", "PRIORITY"):
            raise ValueError("unknown composite mode")
        if not policies:
            raise ValueError("composite requires policies")
        self.policies = tuple(policies)
        self.mode = mode
        self.policy_id = policy_id
        self.policy_version = "{}_{}".format(policy_id, mode.lower())
        self.member_evaluations = []

    @property
    def config_sha256(self):
        return _config_sha256({
            "mode": self.mode,
            "members": [(item.policy_id, item.policy_version,
                         item.config_sha256) for item in self.policies]})

    def evaluate(self, context):
        members = [policy.evaluate(context) for policy in self.policies]
        self.member_evaluations.extend(members)
        triggered = [item for item in members if item.triggered and item.proposal]
        accepted = None
        if self.mode == "ALL_OF" and len(triggered) == len(members):
            accepted = triggered
        elif self.mode == "ANY_OF" and triggered:
            accepted = [sorted(triggered,
                               key=lambda item: (-item.proposal.priority,
                                                 item.policy_id))[0]]
        elif self.mode == "PRIORITY":
            accepted = next(([item] for item in members
                             if item.triggered and item.proposal), None)
        identity = evaluation_id(
            context.trade_snapshot.trade_id, context.signal_asof,
            self.policy_id, self.policy_version)
        if not accepted:
            reasons = tuple("{}:{}".format(item.policy_id, code)
                            for item in members for code in item.reason_codes)
            return PolicyEvaluation(
                identity, self.policy_id, self.policy_version,
                context.trade_snapshot.trade_id, context.signal_asof, False,
                {"member_evaluation_ids": tuple(item.evaluation_id
                                                 for item in members)},
                reasons or ("NO_MEMBER_TRIGGERED",), (), self.config_sha256,
                context.data_version)
        proposals = [item.proposal for item in accepted]
        sequence = context.trade_snapshot.add_count+1
        quantity_caps = [item.quantity_cap_optional for item in proposals
                         if item.quantity_cap_optional is not None]
        merged = AddProposal(
            proposal_id=proposal_id(identity, sequence), evaluation_id=identity,
            trade_id=context.trade_snapshot.trade_id,
            allocation_id=context.trade_snapshot.allocation_id,
            symbol=context.trade_snapshot.symbol,
            signal_asof=context.signal_asof, add_sequence=sequence,
            trigger_code="COMPOSITE_{}".format(self.mode),
            risk_budget_cash_cap=min(item.risk_budget_cash_cap for item in proposals),
            notional_cash_cap=min(item.notional_cash_cap for item in proposals),
            current_stop_raw_snapshot=min(
                item.current_stop_raw_snapshot for item in proposals),
            max_buy_price_raw=min(item.max_buy_price_raw for item in proposals),
            valid_session=context.valid_session,
            priority=max(item.priority for item in proposals),
            quantity_cap_optional=min(quantity_caps) if quantity_caps else None,
            reason_codes=tuple(item.trigger_code for item in proposals),
            logical_order_id=logical_add_order_id(
                context.trade_snapshot.trade_id, context.signal_asof, sequence))
        return PolicyEvaluation(
            identity, self.policy_id, self.policy_version,
            context.trade_snapshot.trade_id, context.signal_asof, True,
            {"member_evaluation_ids": tuple(item.evaluation_id for item in members)},
            merged.reason_codes, (), self.config_sha256,
            context.data_version, merged)


def load_rebreakout_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(payload) != set(RebreakoutConfig.__dataclass_fields__):
        raise ValueError("Rebreakout config fields mismatch")
    return RebreakoutConfig(**payload)


def load_turtle_atr_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(payload) != set(TurtleAtrConfig.__dataclass_fields__):
        raise ValueError("TurtleATR config fields mismatch")
    return TurtleAtrConfig(**payload)
