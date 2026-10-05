# -*- encoding: utf-8 -*-
"""Immutable records for the v2 intent-to-fill lifecycle."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping


VALID_POSITION_EFFECTS = ("OPEN", "INCREASE", "REDUCE", "CLOSE")
VALID_INTRADAY_STATES = (
    "CREATED", "ACTIVE", "CANDIDATE", "FILLED", "CANCELLED", "EXPIRED",
)


def validate_side_effect(side: str, position_effect: str) -> None:
    """Validate an explicit side/effect pair while accepting legacy blanks."""
    if not position_effect:
        return
    allowed = {
        "buy": ("OPEN", "INCREASE"),
        "sell": ("REDUCE", "CLOSE"),
    }
    if position_effect not in VALID_POSITION_EFFECTS:
        raise ValueError("unknown position_effect: {}".format(position_effect))
    if position_effect not in allowed.get(side, ()):
        raise ValueError("illegal side/position_effect combination")


def make_record_id(prefix: str, *parts) -> str:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    return "{}-{}".format(prefix, hashlib.sha256(encoded).hexdigest()[:20])


@dataclass(frozen=True)
class TradeIntent:
    intent_id: str
    strategy_id: str
    strategy_version: str
    signal_asof: int
    symbol: str
    side: str = "buy"
    score: float = 0.0
    signal_price_adjusted: float | None = None
    signal_price_raw: float | None = None
    adjustment_factor_signal: float | None = None
    initial_stop_adjusted: float | None = None
    initial_stop_raw: float | None = None
    industry_asof: int = -1
    required_fields: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    max_gap_atr: float = 1.0
    valid_for_sessions: int = 1
    r_definition_version: str = "none"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    trade_id: str = ""
    allocation_id: str = "GLOBAL"
    position_effect: str = ""
    source_policy_id: str = ""
    source_policy_version: str = ""
    policy_evaluation_id: str = ""
    proposal_id: str = ""
    logical_order_id: str = ""
    schema_version: str = "position_lineage_v1"
    account_id: str = ""
    strategy_instance_id: str = ""
    actor_activation_id: str = ""
    source_snapshot_id: str = ""

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if self.valid_for_sessions <= 0:
            raise ValueError("valid_for_sessions must be positive")
        validate_side_effect(self.side, self.position_effect)


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    intent_id: str
    reserved_cash: float
    reserved_risk: float
    reserved_industry_risk: float
    reserved_same_day_risk: float
    reserved_stress_loss: float
    expires_on: int
    decision: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApprovedOrder:
    order_id: str
    intent_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    side: str
    quantity: int
    created_asof: int
    valid_session: int
    max_buy_price_raw: float | None = None
    initial_stop_adjusted: float | None = None
    initial_stop_raw: float | None = None
    planned_initial_r_per_share_raw: float = 0.0
    planned_initial_r_cash: float = 0.0
    reason: str = ""
    target_trade_id: str = ""
    allocation_id: str = "GLOBAL"
    position_effect: str = ""
    source_policy_id: str = ""
    source_policy_version: str = ""
    policy_evaluation_id: str = ""
    proposal_id: str = ""
    logical_order_id: str = ""
    physical_order_id: str = ""
    schema_version: str = "position_lineage_v1"
    signal_price_adjusted: float | None = None
    adjustment_factor_signal: float | None = None
    portfolio_equity_asof: float = 0.0
    account_id: str = ""
    strategy_instance_id: str = ""
    actor_activation_id: str = ""
    source_snapshot_id: str = ""

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        # A-share purchases use board lots.  A position may acquire an odd-lot
        # remainder through a corporate action; exchanges allow that remainder
        # to be sold as one order, so sell orders must retain the exact shares.
        if self.side == "buy" and self.quantity % 100:
            raise ValueError("buy quantity must be a positive board lot")
        if self.side == "buy" and not self.max_buy_price_raw:
            raise ValueError("buy order requires max_buy_price_raw")
        validate_side_effect(self.side, self.position_effect)


@dataclass(frozen=True)
class IntradayExecutionInstruction:
    instruction_id: str
    order_id: str
    symbol: str
    quantity: int
    trading_date: int
    policy_id: str
    max_buy_price_raw: float
    trigger_bar_end: str = "09:35:00"
    last_decision_at: str = "10:29:00"
    last_candidate_start: str = "10:30:00"
    created_at: str = ""
    reservation_id: str = ""
    schema_version: str = "intraday_instruction_v1"

    def __post_init__(self):
        if self.quantity <= 0 or self.quantity % 100:
            raise ValueError("intraday quantity must be a positive board lot")
        if self.policy_id not in ("M1", "M2"):
            raise ValueError("policy_id must be M1 or M2")
        if self.max_buy_price_raw <= 0:
            raise ValueError("max_buy_price_raw must be positive")


@dataclass(frozen=True)
class OrderEvent:
    event_id: str
    instruction_id: str
    order_id: str
    sequence: int
    event_type: str
    state: str
    event_at: str
    reason_code: str = ""
    reference_bar_end: str = ""
    candidate_bar_start: str = ""
    input_source: str = ""
    input_revision: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = "intraday_order_event_v1"

    def __post_init__(self):
        if self.sequence <= 0:
            raise ValueError("sequence must be positive")
        if self.state not in VALID_INTRADAY_STATES:
            raise ValueError("invalid intraday state")
        if self.input_revision < 0:
            raise ValueError("input_revision must be non-negative")


@dataclass(frozen=True)
class Fill:
    order_id: str
    intent_id: str
    date: int
    symbol: str
    side: str
    status: str
    quantity: int
    reference_price: float = 0.0
    fill_price_raw: float = 0.0
    commission: float = 0.0
    transfer_fee: float = 0.0
    stamp_tax: float = 0.0
    slippage_cost: float = 0.0
    actual_initial_r_per_share_raw: float = 0.0
    actual_initial_r_cash: float = 0.0
    reason_code: str = ""
    limit_rule_id: str = ""
    execution_limit_model_version: str = "legacy_v1"
    limit_reference_quality: str = ""
    limit_reason_codes: tuple[str, ...] = ()
    fill_id: str = ""
    target_trade_id: str = ""
    allocation_id: str = "GLOBAL"
    position_effect: str = ""
    source_policy_id: str = ""
    source_policy_version: str = ""
    policy_evaluation_id: str = ""
    proposal_id: str = ""
    logical_order_id: str = ""
    physical_order_id: str = ""
    schema_version: str = "position_lineage_v1"
    execution_policy_id: str = "D0"
    decision_at: str = ""
    trigger_bar_end: str = ""
    candidate_bar_start: str = ""
    capacity_reference_bar_end: str = ""
    data_source: str = ""
    data_revision: int = 0
    latency_model: str = ""
    available_at: str = ""
    account_id: str = ""
    strategy_instance_id: str = ""
    actor_activation_id: str = ""
    source_snapshot_id: str = ""

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if self.data_revision < 0:
            raise ValueError("data_revision must be non-negative")
        validate_side_effect(self.side, self.position_effect)


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: int
    entry_date: int
    entry_price_raw: float
    total_cost: float
    strategy_id: str
    strategy_version: str
    initial_stop_raw: float | None = None
    current_stop_raw: float | None = None
    initial_r_per_share_raw: float = 0.0
    initial_r_cash_frozen: float = 0.0


@dataclass(frozen=True)
class PositionEvent:
    date: int
    symbol: str
    event_type: str
    quantity_delta: int = 0
    cash_delta: float = 0.0
    reason: str = ""
