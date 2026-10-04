# -*- encoding: utf-8 -*-
"""Immutable position-lineage records and deterministic migration helpers.

The executable ledger is added in M2.  This module deliberately keeps M1 to
schema, validation and deterministic identity so legacy backtests remain
economically unchanged.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Iterable

from .ABuTradeIntent import Position, make_record_id, validate_side_effect


TRADE_STATUSES = (
    "PENDING_OPEN", "ACTIVE", "EXIT_REQUESTED", "EXIT_PENDING", "CLOSED",
    "CANCELLED",
)
LOT_STATUSES = ("ACTIVE", "CLOSED")


def _positive(value, name):
    if value < 0:
        raise ValueError("{} must be non-negative".format(name))


def evaluation_id(trade_id, signal_asof, policy_id, policy_version):
    return make_record_id(
        "evaluation", trade_id, int(signal_asof), policy_id, policy_version)


def proposal_id(evaluation_id_value, add_sequence):
    return make_record_id("proposal", evaluation_id_value, int(add_sequence))


def logical_add_order_id(trade_id, signal_asof, add_sequence):
    """Plugin-independent ADD idempotency key required by the v1 spec."""
    return make_record_id(
        "logical-add", trade_id, int(signal_asof), int(add_sequence), "INCREASE")


def physical_order_id(logical_order_id_value):
    return make_record_id("physical-order", logical_order_id_value)


def fill_id(physical_order_id_value, fill_date, sequence=1):
    return make_record_id(
        "fill", physical_order_id_value, int(fill_date), int(sequence))


def fill_allocation_id(fill_id_value, logical_order_id_value):
    return make_record_id("fill-allocation", fill_id_value, logical_order_id_value)


def lot_id(trade_id, fill_allocation_id_value):
    return make_record_id("lot", trade_id, fill_allocation_id_value)


def disposition_id(fill_allocation_id_value, lot_id_value):
    return make_record_id(
        "disposition", fill_allocation_id_value, lot_id_value)


@dataclass(frozen=True)
class PhysicalPosition:
    symbol: str
    total_quantity: int
    sellable_quantity: int
    reserved_sell_quantity: int
    remaining_book_cost_cash: float
    market_value_cash: float = 0.0
    logical_trade_ids: tuple[str, ...] = ()
    active_physical_order_ids: tuple[str, ...] = ()
    lifecycle_version: str = "position_lifecycle_v1"

    def __post_init__(self):
        for name in ("total_quantity", "sellable_quantity",
                     "reserved_sell_quantity"):
            _positive(getattr(self, name), name)
        _positive(self.remaining_book_cost_cash, "remaining_book_cost_cash")
        if self.sellable_quantity + self.reserved_sell_quantity > self.total_quantity:
            raise ValueError("sellable plus reserved quantity exceeds total")


@dataclass(frozen=True)
class LogicalTrade:
    trade_id: str
    allocation_id: str
    symbol: str
    selection_strategy_id: str
    selection_strategy_version: str
    entry_intent_id: str
    status: str
    opened_at: int | None = None
    closed_at: int | None = None
    lot_ids: tuple[str, ...] = ()
    pending_add_order_ids: tuple[str, ...] = ()
    pending_exit_order_ids: tuple[str, ...] = ()
    add_count: int = 0
    max_add_count: int = 1
    initial_entry_price_adjusted: float | None = None
    initial_entry_price_raw: float | None = None
    initial_stop_adjusted: float | None = None
    current_stop_adjusted: float | None = None
    current_stop_raw: float | None = None
    initial_r_per_share_adjusted: float = 0.0
    initial_r_cash_frozen: float = 0.0
    add_risk_budget_cash_frozen: float = 0.0
    filled_add_risk_cash_frozen: float = 0.0
    reserved_add_risk_cash: float = 0.0
    highest_close_adjusted: float | None = None
    entry_session_index: int | None = None
    last_buy_fill_session_index: int | None = None
    last_add_session_index: int | None = None
    schema_version: str = "position_lineage_v1"
    entry_equity_cash: float = 0.0

    def __post_init__(self):
        if not self.trade_id:
            raise ValueError("trade_id is required")
        if self.status not in TRADE_STATUSES:
            raise ValueError("invalid logical trade status")
        for name in ("add_count", "max_add_count"):
            _positive(getattr(self, name), name)
        if self.add_count > self.max_add_count:
            raise ValueError("add_count exceeds max_add_count")


@dataclass(frozen=True)
class PositionLot:
    lot_id: str
    trade_id: str
    allocation_id: str
    symbol: str
    position_effect: str
    source_policy_id: str
    source_policy_version: str
    order_id: str
    fill_id: str
    fill_date: int
    sellable_date: int
    original_quantity: int
    quantity_remaining: int
    fill_price_raw: float
    allocated_commission_cash: float
    allocated_transfer_fee_cash: float
    allocated_stamp_tax_cash: float
    allocated_slippage_cash: float
    remaining_book_cost_cash: float
    stop_raw_at_fill: float | None
    risk_per_share_raw_at_fill: float
    risk_cash_frozen: float
    adjustment_factor_at_fill: float | None
    status: str = "ACTIVE"
    schema_version: str = "position_lineage_v1"

    def __post_init__(self):
        validate_side_effect("buy", self.position_effect)
        if self.status not in LOT_STATUSES:
            raise ValueError("invalid lot status")
        for name in ("original_quantity", "quantity_remaining"):
            _positive(getattr(self, name), name)
        if self.quantity_remaining > self.original_quantity:
            raise ValueError("remaining quantity exceeds original quantity")
        _positive(self.remaining_book_cost_cash, "remaining_book_cost_cash")


@dataclass(frozen=True)
class FillAllocation:
    fill_allocation_id: str
    physical_fill_id: str
    physical_order_id: str
    logical_order_id: str
    trade_id: str
    allocation_id: str
    symbol: str
    side: str
    position_effect: str
    allocated_quantity: int
    allocated_fill_price_raw: float
    allocated_gross_cash: float
    allocated_commission_cash: float
    allocated_transfer_fee_cash: float
    allocated_stamp_tax_cash: float
    allocated_slippage_cash: float
    created_lot_id_optional: str = ""
    created_at: int = 0
    schema_version: str = "position_lineage_v1"

    def __post_init__(self):
        validate_side_effect(self.side, self.position_effect)
        _positive(self.allocated_quantity, "allocated_quantity")


@dataclass(frozen=True)
class LotDisposition:
    disposition_id: str
    physical_fill_id: str
    fill_allocation_id: str
    logical_order_id: str
    trade_id: str
    allocation_id: str
    lot_id: str
    symbol: str
    disposed_quantity: int
    allocated_gross_proceeds_cash: float
    allocated_sell_commission_cash: float
    allocated_sell_transfer_fee_cash: float
    allocated_stamp_tax_cash: float
    allocated_sell_slippage_cash: float
    disposed_book_cost_cash: float
    realized_pnl_cash: float
    exit_reason: str
    fill_date: int
    schema_version: str = "position_lineage_v1"

    def __post_init__(self):
        _positive(self.disposed_quantity, "disposed_quantity")


@dataclass(frozen=True)
class SellReservation:
    logical_order_id: str
    trade_id: str
    lot_id: str
    quantity: int
    created_at: int
    expires_at: int | None
    status: str = "ACTIVE"
    schema_version: str = "position_lineage_v1"

    def __post_init__(self):
        if self.quantity <= 0:
            raise ValueError("sell reservation quantity must be positive")
        if self.status not in ("ACTIVE", "FILLED", "CANCELLED", "EXPIRED"):
            raise ValueError("invalid sell reservation status")


@dataclass(frozen=True)
class LegacyPositionMigration:
    physical_position: PhysicalPosition
    logical_trade: LogicalTrade
    position_lot: PositionLot
    id_mapping: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def migrate_legacy_position(position: Position, allocation_id="GLOBAL",
                            sellable_date=None) -> LegacyPositionMigration:
    """Map one legacy aggregate position to one trade and one OPEN lot."""
    trade = make_record_id(
        "legacy-trade", position.strategy_id, position.strategy_version,
        position.symbol, position.entry_date)
    legacy_fill = make_record_id("legacy-fill", trade)
    allocation = fill_allocation_id(legacy_fill, make_record_id("legacy-order", trade))
    position_lot_id = lot_id(trade, allocation)
    risk_per_share = float(position.initial_r_per_share_raw or 0.0)
    logical = LogicalTrade(
        trade_id=trade, allocation_id=allocation_id, symbol=position.symbol,
        selection_strategy_id=position.strategy_id,
        selection_strategy_version=position.strategy_version,
        entry_intent_id=make_record_id("legacy-intent", trade), status="ACTIVE",
        opened_at=int(position.entry_date), lot_ids=(position_lot_id,),
        initial_entry_price_raw=float(position.entry_price_raw),
        current_stop_raw=position.current_stop_raw,
        initial_r_cash_frozen=float(position.initial_r_cash_frozen),
    )
    lot = PositionLot(
        lot_id=position_lot_id, trade_id=trade, allocation_id=allocation_id,
        symbol=position.symbol, position_effect="OPEN", source_policy_id="",
        source_policy_version="", order_id=make_record_id("legacy-order", trade),
        fill_id=legacy_fill, fill_date=int(position.entry_date),
        sellable_date=int(sellable_date or position.entry_date),
        original_quantity=int(position.quantity),
        quantity_remaining=int(position.quantity),
        fill_price_raw=float(position.entry_price_raw),
        allocated_commission_cash=0.0, allocated_transfer_fee_cash=0.0,
        allocated_stamp_tax_cash=0.0, allocated_slippage_cash=0.0,
        remaining_book_cost_cash=float(position.total_cost),
        stop_raw_at_fill=position.initial_stop_raw,
        risk_per_share_raw_at_fill=risk_per_share,
        risk_cash_frozen=float(position.initial_r_cash_frozen),
        adjustment_factor_at_fill=None,
    )
    physical = PhysicalPosition(
        symbol=position.symbol, total_quantity=int(position.quantity),
        sellable_quantity=int(position.quantity), reserved_sell_quantity=0,
        remaining_book_cost_cash=float(position.total_cost),
        logical_trade_ids=(trade,),
    )
    return LegacyPositionMigration(
        physical_position=physical, logical_trade=logical, position_lot=lot,
        id_mapping={"legacy_symbol": position.symbol, "trade_id": trade,
                    "fill_id": legacy_fill, "lot_id": position_lot_id},
    )


def records_to_dicts(records: Iterable[object]):
    """Stable serialization helper used by research exporters."""
    return [asdict(record) for record in records]


class PositionLedger(object):
    """Append-only allocation facts plus immutable current-state records."""

    def __init__(self):
        self.physical_positions = {}
        self.logical_trades = {}
        self.lots = {}
        self.fill_allocations = []
        self.lot_dispositions = []
        self.sell_reservations = []

    _TRANSITIONS = {
        ("PENDING_OPEN", "OPEN_FILLED"): "ACTIVE",
        ("PENDING_OPEN", "OPEN_FAILED"): "CANCELLED",
        ("ACTIVE", "EXIT_REQUESTED"): "EXIT_REQUESTED",
        ("EXIT_REQUESTED", "EXIT_ORDER_APPROVED"): "EXIT_PENDING",
        ("EXIT_PENDING", "EXIT_ORDER_CANCELLED"): "EXIT_REQUESTED",
        ("EXIT_PENDING", "EXIT_PARTIAL_FILLED"): "EXIT_PENDING",
        ("EXIT_PENDING", "EXIT_FILLED"): "CLOSED",
        ("ACTIVE", "RISK_REDUCE_FILLED"): "ACTIVE",
        ("ACTIVE", "RISK_CLOSE_FILLED"): "CLOSED",
    }

    def transition(self, trade_id_value, event, **changes):
        trade = self.logical_trades.get(trade_id_value)
        if trade is None:
            raise ValueError("unknown trade_id")
        target = self._TRANSITIONS.get((trade.status, event))
        if target is None:
            raise ValueError("INVALID_TRADE_STATE_TRANSITION")
        updated = replace(trade, status=target, **changes)
        self.logical_trades[trade_id_value] = updated
        return updated

    def register_order(self, order):
        effect = order.position_effect or ("OPEN" if order.side == "buy" else "CLOSE")
        trade_id_value = order.target_trade_id or make_record_id("trade", order.intent_id)
        if order.side == "buy" and effect == "OPEN":
            if trade_id_value not in self.logical_trades:
                self.logical_trades[trade_id_value] = LogicalTrade(
                    trade_id=trade_id_value, allocation_id=order.allocation_id,
                    symbol=order.symbol,
                    selection_strategy_id=order.strategy_id,
                    selection_strategy_version=order.strategy_version,
                    entry_intent_id=order.intent_id, status="PENDING_OPEN",
                    initial_stop_adjusted=order.initial_stop_adjusted,
                    current_stop_raw=order.initial_stop_raw,
                )
            return trade_id_value
        if order.side == "buy" and effect == "INCREASE":
            trade = self.logical_trades.get(trade_id_value)
            if trade is None or trade.status != "ACTIVE":
                raise ValueError("TARGET_TRADE_NOT_ACTIVE")
            if order.logical_order_id not in trade.pending_add_order_ids:
                self.logical_trades[trade_id_value] = replace(
                    trade,
                    pending_add_order_ids=(trade.pending_add_order_ids +
                                           (order.logical_order_id,)),
                    reserved_add_risk_cash=(trade.reserved_add_risk_cash +
                                            order.planned_initial_r_cash),
                )
            return trade_id_value
        if order.side == "sell":
            trade_id_value = self.resolve_trade_id(
                order.symbol, order.target_trade_id)
            trade = self.logical_trades[trade_id_value]
            if trade.status == "ACTIVE":
                trade = self.transition(trade_id_value, "EXIT_REQUESTED")
            if trade.status == "EXIT_REQUESTED":
                self.transition(
                    trade_id_value, "EXIT_ORDER_APPROVED",
                    pending_exit_order_ids=(trade.pending_exit_order_ids +
                                            (order.logical_order_id,)),
                )
            return trade_id_value
        return trade_id_value

    def cancel_order(self, order, status="cancelled"):
        effect = order.position_effect or ("OPEN" if order.side == "buy" else "CLOSE")
        trade_id_value = order.target_trade_id or make_record_id("trade", order.intent_id)
        trade = self.logical_trades.get(trade_id_value)
        if trade is None:
            return
        if order.side == "buy" and effect == "OPEN" and trade.status == "PENDING_OPEN":
            self.transition(trade_id_value, "OPEN_FAILED")
        elif order.side == "buy" and effect == "INCREASE":
            self.logical_trades[trade_id_value] = replace(
                trade, pending_add_order_ids=tuple(
                    item for item in trade.pending_add_order_ids
                    if item != order.logical_order_id),
                reserved_add_risk_cash=max(
                    0.0, trade.reserved_add_risk_cash -
                    order.planned_initial_r_cash),
            )
        elif order.side == "sell":
            self.release_sell_reservations(order.logical_order_id, status.upper())
            if trade.status == "EXIT_PENDING":
                pending = tuple(item for item in trade.pending_exit_order_ids
                                if item != order.logical_order_id)
                self.transition(trade_id_value, "EXIT_ORDER_CANCELLED",
                                pending_exit_order_ids=pending)

    def request_exit(self, trade_id_value):
        trade = self.logical_trades[trade_id_value]
        if trade.status == "ACTIVE":
            return self.transition(trade_id_value, "EXIT_REQUESTED",
                                   pending_add_order_ids=())
        if trade.status in ("EXIT_REQUESTED", "EXIT_PENDING"):
            return trade
        raise ValueError("INVALID_TRADE_STATE_TRANSITION")

    def reserve_sell(self, logical_order_id_value, trade_id_value, quantity,
                     created_at, executable_date):
        available_lots = [lot for lot in self.lots_for_trade(trade_id_value)
                          if lot.sellable_date <= int(executable_date)]
        already = {}
        for item in self.sell_reservations:
            if item.status == "ACTIVE":
                already[item.lot_id] = already.get(item.lot_id, 0) + item.quantity
        remaining = int(quantity)
        records = []
        for lot in available_lots:
            free = max(0, lot.quantity_remaining - already.get(lot.lot_id, 0))
            selected = min(remaining, free)
            if selected:
                records.append(SellReservation(
                    logical_order_id=logical_order_id_value,
                    trade_id=trade_id_value, lot_id=lot.lot_id,
                    quantity=selected, created_at=int(created_at),
                    expires_at=None,
                ))
                remaining -= selected
            if remaining == 0:
                break
        if remaining:
            raise ValueError("T1_SELLABLE_QUANTITY_INSUFFICIENT")
        self.sell_reservations.extend(records)
        self._refresh_physical(self.logical_trades[trade_id_value].symbol,
                               created_at)
        return tuple(records)

    def release_sell_reservations(self, logical_order_id_value, status="CANCELLED"):
        updated = []
        for item in self.sell_reservations:
            if item.logical_order_id == logical_order_id_value and item.status == "ACTIVE":
                updated.append(replace(item, status=status))
            else:
                updated.append(item)
        self.sell_reservations = updated

    def active_trades(self, symbol=None):
        values = [trade for trade in self.logical_trades.values()
                  if trade.status not in ("CLOSED", "CANCELLED")]
        if symbol is not None:
            values = [trade for trade in values if trade.symbol == symbol]
        return sorted(values, key=lambda item: item.trade_id)

    def resolve_trade_id(self, symbol, requested=""):
        if requested:
            trade = self.logical_trades.get(requested)
            if trade is None or trade.symbol != symbol:
                raise ValueError("target trade does not exist for symbol")
            return requested
        active = self.active_trades(symbol)
        if len(active) != 1:
            raise ValueError("trade_id required when active trade count is not one")
        return active[0].trade_id

    def quantity_for_trade(self, trade_id_value):
        return sum(lot.quantity_remaining for lot in self.lots.values()
                   if lot.trade_id == trade_id_value and lot.status == "ACTIVE")

    def lots_for_trade(self, trade_id_value, active_only=True):
        values = [lot for lot in self.lots.values()
                  if lot.trade_id == trade_id_value and
                  (not active_only or lot.status == "ACTIVE")]
        return sorted(values, key=lambda item: (item.fill_date, item.lot_id))

    def _refresh_physical(self, symbol, current_date=None):
        lots = [lot for lot in self.lots.values()
                if lot.symbol == symbol and lot.status == "ACTIVE" and
                lot.quantity_remaining > 0]
        if not lots:
            self.physical_positions.pop(symbol, None)
            return None
        reserved_by_lot = {}
        for item in self.sell_reservations:
            if item.status == "ACTIVE":
                reserved_by_lot[item.lot_id] = (reserved_by_lot.get(item.lot_id, 0) +
                                                 item.quantity)
        total = sum(lot.quantity_remaining for lot in lots)
        reserved = sum(reserved_by_lot.get(lot.lot_id, 0) for lot in lots)
        if current_date is None:
            sellable = max(0, total - reserved)
        else:
            sellable = sum(lot.quantity_remaining for lot in lots
                           if lot.sellable_date <= int(current_date)) - reserved
            sellable = max(0, sellable)
        record = PhysicalPosition(
            symbol=symbol, total_quantity=total, sellable_quantity=sellable,
            reserved_sell_quantity=reserved,
            remaining_book_cost_cash=sum(lot.remaining_book_cost_cash for lot in lots),
            logical_trade_ids=tuple(sorted(set(lot.trade_id for lot in lots))),
        )
        self.physical_positions[symbol] = record
        return record

    def record_buy(self, order, fill, sellable_date, session_index=None):
        if fill.status != "filled" or fill.quantity <= 0:
            raise ValueError("record_buy requires a positive filled trade")
        effect = order.position_effect or "OPEN"
        validate_side_effect("buy", effect)
        trade_id_value = (order.target_trade_id or
                          make_record_id("trade", order.intent_id))
        logical_id = order.logical_order_id or order.order_id
        physical_id = order.physical_order_id or order.order_id
        physical_fill = fill.fill_id or fill_id(physical_id, fill.date)
        allocation_id_value = fill_allocation_id(physical_fill, logical_id)
        created_lot = lot_id(trade_id_value, allocation_id_value)
        if effect == "OPEN":
            existing = self.logical_trades.get(trade_id_value)
            if existing is not None and existing.status != "PENDING_OPEN":
                raise ValueError("duplicate OPEN trade_id")
            trade = LogicalTrade(
                trade_id=trade_id_value, allocation_id=order.allocation_id,
                symbol=order.symbol, selection_strategy_id=order.strategy_id,
                selection_strategy_version=order.strategy_version,
                entry_intent_id=order.intent_id, status="ACTIVE",
                opened_at=fill.date, lot_ids=(created_lot,),
                initial_entry_price_adjusted=order.signal_price_adjusted,
                initial_entry_price_raw=fill.fill_price_raw,
                initial_stop_adjusted=order.initial_stop_adjusted,
                current_stop_raw=order.initial_stop_raw,
                initial_r_cash_frozen=fill.actual_initial_r_cash,
                initial_r_per_share_adjusted=max(
                    0.0, float(order.signal_price_adjusted or 0.0) -
                    float(order.initial_stop_adjusted or
                          order.signal_price_adjusted or 0.0)),
                entry_equity_cash=float(order.portfolio_equity_asof or 0.0),
                entry_session_index=session_index,
                last_buy_fill_session_index=session_index,
            )
        else:
            trade = self.logical_trades.get(trade_id_value)
            if trade is None or trade.status != "ACTIVE":
                raise ValueError("INCREASE target trade is not ACTIVE")
            trade = replace(
                trade, lot_ids=trade.lot_ids + (created_lot,),
                add_count=trade.add_count + 1,
                filled_add_risk_cash_frozen=(trade.filled_add_risk_cash_frozen +
                                             fill.actual_initial_r_cash),
                reserved_add_risk_cash=max(
                    0.0, trade.reserved_add_risk_cash -
                    float(order.planned_initial_r_cash)),
                last_buy_fill_session_index=session_index,
                last_add_session_index=session_index,
                current_stop_raw=(order.initial_stop_raw
                                  if order.initial_stop_raw is not None
                                  else trade.current_stop_raw),
                pending_add_order_ids=tuple(
                    item for item in trade.pending_add_order_ids
                    if item != logical_id),
            )
        allocation = FillAllocation(
            fill_allocation_id=allocation_id_value,
            physical_fill_id=physical_fill, physical_order_id=physical_id,
            logical_order_id=logical_id, trade_id=trade_id_value,
            allocation_id=order.allocation_id, symbol=order.symbol, side="buy",
            position_effect=effect, allocated_quantity=fill.quantity,
            allocated_fill_price_raw=fill.fill_price_raw,
            allocated_gross_cash=fill.quantity * fill.fill_price_raw,
            allocated_commission_cash=fill.commission,
            allocated_transfer_fee_cash=fill.transfer_fee,
            allocated_stamp_tax_cash=fill.stamp_tax,
            allocated_slippage_cash=fill.slippage_cost,
            created_lot_id_optional=created_lot, created_at=fill.date,
        )
        lot = PositionLot(
            lot_id=created_lot, trade_id=trade_id_value,
            allocation_id=order.allocation_id, symbol=order.symbol,
            position_effect=effect, source_policy_id=order.source_policy_id,
            source_policy_version=order.source_policy_version,
            order_id=logical_id, fill_id=physical_fill, fill_date=fill.date,
            sellable_date=int(sellable_date), original_quantity=fill.quantity,
            quantity_remaining=fill.quantity, fill_price_raw=fill.fill_price_raw,
            allocated_commission_cash=fill.commission,
            allocated_transfer_fee_cash=fill.transfer_fee,
            allocated_stamp_tax_cash=fill.stamp_tax,
            allocated_slippage_cash=fill.slippage_cost,
            remaining_book_cost_cash=(fill.quantity * fill.fill_price_raw +
                                      fill.commission + fill.transfer_fee +
                                      fill.stamp_tax),
            stop_raw_at_fill=order.initial_stop_raw,
            risk_per_share_raw_at_fill=fill.actual_initial_r_per_share_raw,
            risk_cash_frozen=fill.actual_initial_r_cash,
            adjustment_factor_at_fill=None,
        )
        self.logical_trades[trade_id_value] = trade
        self.lots[created_lot] = lot
        self.fill_allocations.append(allocation)
        self._refresh_physical(order.symbol, fill.date)
        self.assert_conservation(fill, allocation)
        return allocation, lot, trade

    @staticmethod
    def _allocate_amount(total, quantities):
        if not quantities:
            return []
        denominator = float(sum(quantities))
        allocated = []
        running = 0.0
        for quantity in quantities[:-1]:
            value = total * quantity / denominator
            allocated.append(value)
            running += value
        allocated.append(total - running)
        return allocated

    def record_sell(self, order, fill, exit_reason=""):
        if fill.status != "filled" or fill.quantity <= 0:
            raise ValueError("record_sell requires a positive filled trade")
        trade_id_value = self.resolve_trade_id(order.symbol, order.target_trade_id)
        trade = self.logical_trades[trade_id_value]
        available = self.quantity_for_trade(trade_id_value)
        if fill.quantity > available:
            raise ValueError("sell quantity exceeds logical trade")
        effect = order.position_effect or (
            "CLOSE" if fill.quantity == available else "REDUCE")
        validate_side_effect("sell", effect)
        logical_id = order.logical_order_id or order.order_id
        physical_id = order.physical_order_id or order.order_id
        physical_fill = fill.fill_id or fill_id(physical_id, fill.date)
        allocation_id_value = fill_allocation_id(physical_fill, logical_id)
        allocation = FillAllocation(
            fill_allocation_id=allocation_id_value,
            physical_fill_id=physical_fill, physical_order_id=physical_id,
            logical_order_id=logical_id, trade_id=trade_id_value,
            allocation_id=trade.allocation_id, symbol=order.symbol, side="sell",
            position_effect=effect, allocated_quantity=fill.quantity,
            allocated_fill_price_raw=fill.fill_price_raw,
            allocated_gross_cash=fill.quantity * fill.fill_price_raw,
            allocated_commission_cash=fill.commission,
            allocated_transfer_fee_cash=fill.transfer_fee,
            allocated_stamp_tax_cash=fill.stamp_tax,
            allocated_slippage_cash=fill.slippage_cost, created_at=fill.date,
        )
        selected = []
        remaining = fill.quantity
        for lot in self.lots_for_trade(trade_id_value):
            quantity = min(remaining, lot.quantity_remaining)
            if quantity:
                selected.append((lot, quantity))
                remaining -= quantity
            if remaining == 0:
                break
        if remaining:
            raise AssertionError("FIFO selection failed to cover sell fill")
        quantities = [item[1] for item in selected]
        commissions = self._allocate_amount(fill.commission, quantities)
        transfers = self._allocate_amount(fill.transfer_fee, quantities)
        stamps = self._allocate_amount(fill.stamp_tax, quantities)
        slippages = self._allocate_amount(fill.slippage_cost, quantities)
        dispositions = []
        for index, (lot, quantity) in enumerate(selected):
            cost = (lot.remaining_book_cost_cash * quantity /
                    lot.quantity_remaining)
            gross = quantity * fill.fill_price_raw
            pnl = gross - commissions[index] - transfers[index] - stamps[index] - cost
            disposition = LotDisposition(
                disposition_id=disposition_id(allocation_id_value, lot.lot_id),
                physical_fill_id=physical_fill,
                fill_allocation_id=allocation_id_value,
                logical_order_id=logical_id, trade_id=trade_id_value,
                allocation_id=lot.allocation_id, lot_id=lot.lot_id,
                symbol=lot.symbol, disposed_quantity=quantity,
                allocated_gross_proceeds_cash=gross,
                allocated_sell_commission_cash=commissions[index],
                allocated_sell_transfer_fee_cash=transfers[index],
                allocated_stamp_tax_cash=stamps[index],
                allocated_sell_slippage_cash=slippages[index],
                disposed_book_cost_cash=cost, realized_pnl_cash=pnl,
                exit_reason=exit_reason, fill_date=fill.date,
            )
            left = lot.quantity_remaining - quantity
            self.lots[lot.lot_id] = replace(
                lot, quantity_remaining=left,
                remaining_book_cost_cash=max(0.0, lot.remaining_book_cost_cash-cost),
                status="ACTIVE" if left else "CLOSED",
            )
            dispositions.append(disposition)
        self.fill_allocations.append(allocation)
        self.lot_dispositions.extend(dispositions)
        quantity_left = self.quantity_for_trade(trade_id_value)
        if trade.status == "EXIT_PENDING":
            event = "EXIT_PARTIAL_FILLED" if quantity_left else "EXIT_FILLED"
            self.logical_trades[trade_id_value] = replace(
                trade, status=self._TRANSITIONS[(trade.status, event)],
                closed_at=None if quantity_left else fill.date,
                pending_exit_order_ids=tuple(
                    item for item in trade.pending_exit_order_ids
                    if item != logical_id),
            )
        else:
            event = "RISK_REDUCE_FILLED" if quantity_left else "RISK_CLOSE_FILLED"
            self.logical_trades[trade_id_value] = replace(
                trade, status=self._TRANSITIONS[(trade.status, event)],
                closed_at=None if quantity_left else fill.date,
            )
        self.release_sell_reservations(logical_id, "FILLED")
        self._refresh_physical(order.symbol, fill.date)
        self.assert_conservation(fill, allocation, dispositions)
        return allocation, tuple(dispositions)

    def assert_conservation(self, fill, allocation, dispositions=()):
        tolerance = 1e-8
        if fill.quantity != allocation.allocated_quantity:
            raise AssertionError("fill/allocation quantity conservation failed")
        if abs(fill.commission-allocation.allocated_commission_cash) > tolerance:
            raise AssertionError("commission conservation failed")
        if abs(fill.transfer_fee-allocation.allocated_transfer_fee_cash) > tolerance:
            raise AssertionError("transfer fee conservation failed")
        if abs(fill.stamp_tax-allocation.allocated_stamp_tax_cash) > tolerance:
            raise AssertionError("stamp tax conservation failed")
        if dispositions:
            if sum(item.disposed_quantity for item in dispositions) != fill.quantity:
                raise AssertionError("disposition quantity conservation failed")
            for field_name, fill_value in (
                    ("allocated_sell_commission_cash", fill.commission),
                    ("allocated_sell_transfer_fee_cash", fill.transfer_fee),
                    ("allocated_stamp_tax_cash", fill.stamp_tax),
                    ("allocated_sell_slippage_cash", fill.slippage_cost)):
                if abs(sum(getattr(item, field_name) for item in dispositions) -
                       fill_value) > tolerance:
                    raise AssertionError("disposition fee conservation failed")

    def compatibility_position(self, symbol):
        physical = self.physical_positions.get(symbol)
        if physical is None:
            return None
        lots = [lot for lot in self.lots.values()
                if lot.symbol == symbol and lot.status == "ACTIVE"]
        trades = self.active_trades(symbol)
        first_trade = trades[0]
        weighted_fill = sum(lot.fill_price_raw * lot.quantity_remaining
                            for lot in lots) / physical.total_quantity
        initial_r = sum(lot.risk_cash_frozen for lot in lots)
        return Position(
            symbol=symbol, quantity=physical.total_quantity,
            entry_date=min(lot.fill_date for lot in lots),
            entry_price_raw=weighted_fill,
            total_cost=physical.remaining_book_cost_cash,
            strategy_id=first_trade.selection_strategy_id,
            strategy_version=first_trade.selection_strategy_version,
            initial_stop_raw=lots[0].stop_raw_at_fill,
            current_stop_raw=first_trade.current_stop_raw,
            initial_r_per_share_raw=(initial_r / physical.total_quantity),
            initial_r_cash_frozen=initial_r,
        )

    def update_trade_stop(self, trade_id_value, current_stop_raw):
        trade = self.logical_trades[trade_id_value]
        self.logical_trades[trade_id_value] = replace(
            trade, current_stop_raw=float(current_stop_raw))

    def configure_add_risk_budget(self, trade_id_value, budget_cash):
        trade = self.logical_trades[trade_id_value]
        if budget_cash < 0:
            raise ValueError("add risk budget must be non-negative")
        if trade.add_risk_budget_cash_frozen and abs(
                trade.add_risk_budget_cash_frozen-budget_cash) > 1e-8:
            raise ValueError("add risk budget is already frozen")
        self.logical_trades[trade_id_value] = replace(
            trade, add_risk_budget_cash_frozen=float(budget_cash))

    def sync_legacy_position(self, position):
        """Accept old callers that directly replace the compatibility record."""
        trades = self.active_trades(position.symbol)
        lots = [lot for lot in self.lots.values()
                if lot.symbol == position.symbol and lot.status == "ACTIVE"]
        if len(trades) != 1 or len(lots) != 1:
            return False
        lot = lots[0]
        delta = int(position.quantity) - lot.quantity_remaining
        if delta == 0:
            return True
        if lot.quantity_remaining + delta < 0:
            return False
        self.lots[lot.lot_id] = replace(
            lot, original_quantity=lot.original_quantity + delta,
            quantity_remaining=lot.quantity_remaining + delta,
            remaining_book_cost_cash=float(position.total_cost),
        )
        self._refresh_physical(position.symbol)
        return True

    def apply_stock_action(self, symbol, stock_per_share, sellable_date):
        """Apply a split/stock dividend deterministically across active lots."""
        ratio = float(stock_per_share)
        if ratio <= 0:
            return 0
        lots = [lot for lot in self.lots.values()
                if lot.symbol == symbol and lot.status == "ACTIVE"]
        theoretical = [(lot, lot.quantity_remaining * ratio) for lot in lots]
        target = int(sum(value for _, value in theoretical))
        base = {lot.lot_id: int(value) for lot, value in theoretical}
        remainder = target - sum(base.values())
        ordered = sorted(theoretical,
                         key=lambda item: (-(item[1]-int(item[1])), item[0].lot_id))
        for lot, _ in ordered[:remainder]:
            base[lot.lot_id] += 1
        factor = 1.0 + ratio
        for lot in lots:
            addition = base[lot.lot_id]
            self.lots[lot.lot_id] = replace(
                lot, original_quantity=lot.original_quantity + addition,
                quantity_remaining=lot.quantity_remaining + addition,
                fill_price_raw=lot.fill_price_raw / factor,
                stop_raw_at_fill=(lot.stop_raw_at_fill / factor
                                  if lot.stop_raw_at_fill is not None else None),
                sellable_date=max(lot.sellable_date, int(sellable_date)),
            )
        for trade in self.active_trades(symbol):
            self.logical_trades[trade.trade_id] = replace(
                trade,
                initial_entry_price_raw=(trade.initial_entry_price_raw / factor
                                         if trade.initial_entry_price_raw is not None
                                         else None),
                current_stop_raw=(trade.current_stop_raw / factor
                                  if trade.current_stop_raw is not None else None),
            )
        self._refresh_physical(symbol, sellable_date)
        return target
