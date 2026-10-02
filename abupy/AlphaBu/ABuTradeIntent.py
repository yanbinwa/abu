# -*- encoding: utf-8 -*-
"""Immutable records for the v2 intent-to-fill lifecycle."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping


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

    def __post_init__(self):
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if self.valid_for_sessions <= 0:
            raise ValueError("valid_for_sessions must be positive")


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
