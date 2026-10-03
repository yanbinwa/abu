# -*- encoding: utf-8 -*-
"""Auditable next-open executor with fixed orders and an independent ledger."""
from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import fields
from pathlib import Path

import numpy as np
import pandas as pd

from .ABuPriceLimit import (
    PRICE_LIMIT_MODEL_VERSION, LimitRuleContext, can_buy_at_open,
    can_sell_at_open, limit_prices, limit_rule_for_context, price_limit_rule,
)
from .ABuLimitReference import LimitReferenceStore
from .ABuSecurityLifecycle import accounting_mark
from .ABuTradeIntent import (
    ApprovedOrder, Fill, Position, PositionEvent, Reservation, TradeIntent,
    make_record_id,
)


@dataclass(frozen=True)
class ExecutionConfig:
    initial_cash: float = 1_000_000.0
    broker_rate: float = 0.00025
    min_commission: float = 5.0
    transfer_rate: float = 0.00001
    sell_stamp_rate: float = 0.0005
    slippage_bps: float = 25.0
    mode: str = "pit_corrected"
    max_positions: int | None = None

    def __post_init__(self):
        if self.initial_cash <= 0 or self.slippage_bps < 0:
            raise ValueError("invalid execution configuration")
        if any(value < 0 for value in (
                self.broker_rate, self.min_commission, self.transfer_rate,
                self.sell_stamp_rate)):
            raise ValueError("execution fees must be non-negative")
        if self.mode not in ("pit_corrected", "legacy_compat", "limit_reference_v2"):
            raise ValueError("unknown execution mode")
        if self.max_positions is not None and self.max_positions <= 0:
            raise ValueError("max_positions must be positive")


def load_execution_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(ExecutionConfig)}
    if set(payload) != expected:
        raise ValueError("execution config fields mismatch")
    return ExecutionConfig(**payload)


class PortfolioExecutor(object):

    def __init__(self, panel, config=None, limit_references=None):
        self.panel = panel
        self.config = config or ExecutionConfig()
        self.cash = float(self.config.initial_cash)
        self.reserved_cash = 0.0
        self.positions = {}
        self.orders = []
        self.order_history = []
        self.reservations = {}
        self.reservation_history = []
        self.fills = []
        self.position_events = []
        self.curve = []
        self.last_close = np.full(len(panel.symbols), np.nan, dtype=np.float64)
        self._cash_receivables = {}
        self._share_receivables = {}
        self.limit_references = limit_references or LimitReferenceStore()
        self.limit_audit = []

    def _fees(self, quantity, price, side):
        gross = quantity * price
        commission = max(gross * self.config.broker_rate,
                         self.config.min_commission)
        transfer = gross * self.config.transfer_rate
        stamp = gross * self.config.sell_stamp_rate if side == "sell" else 0.0
        return commission, transfer, stamp

    @property
    def available_cash(self):
        return self.cash - self.reserved_cash

    def approve_order(self, intent: TradeIntent, quantity: int,
                      valid_session: int, max_buy_price_raw=None,
                      planned_risk_per_share=0.0):
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        if intent.side == "buy" and quantity % 100:
            raise ValueError("buy quantity must be a positive board lot")
        order_id = make_record_id(
            "order", intent.intent_id, valid_session, quantity,
            max_buy_price_raw, len(self.orders),
        )
        planned_risk_per_share = float(planned_risk_per_share or 0.0)
        order = ApprovedOrder(
            order_id=order_id, intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            symbol=intent.symbol, side=intent.side, quantity=int(quantity),
            created_asof=int(intent.signal_asof),
            valid_session=int(valid_session),
            max_buy_price_raw=(float(max_buy_price_raw)
                               if max_buy_price_raw is not None else None),
            initial_stop_adjusted=intent.initial_stop_adjusted,
            initial_stop_raw=intent.initial_stop_raw,
            planned_initial_r_per_share_raw=planned_risk_per_share,
            planned_initial_r_cash=planned_risk_per_share * quantity,
        )
        reserved = 0.0
        if order.side == "buy":
            fees = sum(self._fees(quantity, order.max_buy_price_raw, "buy"))
            reserved = quantity * order.max_buy_price_raw + fees
            if reserved > self.available_cash + 1e-9:
                rejection = Reservation(
                    reservation_id=make_record_id("reservation", order_id),
                    intent_id=intent.intent_id, reserved_cash=0.0,
                    reserved_risk=0.0, reserved_industry_risk=0.0,
                    reserved_same_day_risk=0.0, reserved_stress_loss=0.0,
                    expires_on=int(valid_session), decision="rejected",
                    reason_codes=("INSUFFICIENT_CASH_RESERVATION",),
                )
                self.reservation_history.append(rejection)
                return None, rejection
            self.reserved_cash += reserved
        reservation = Reservation(
            reservation_id=make_record_id("reservation", order_id),
            intent_id=intent.intent_id, reserved_cash=reserved,
            reserved_risk=order.planned_initial_r_cash,
            reserved_industry_risk=0.0,
            reserved_same_day_risk=order.planned_initial_r_cash,
            reserved_stress_loss=0.0, expires_on=int(valid_session),
            decision="approved", reason_codes=(),
        )
        self.orders.append(order)
        self.order_history.append(order)
        self.reservations[order.order_id] = reservation
        self.reservation_history.append(reservation)
        return order, reservation

    def _release(self, order):
        reservation = self.reservations.pop(order.order_id, None)
        if reservation is not None:
            self.reserved_cash = max(
                0.0, self.reserved_cash - reservation.reserved_cash
            )

    def _symbol(self, symbol):
        try:
            return self.panel.symbol_index[symbol]
        except KeyError:
            raise ValueError("symbol is not in panel: {}".format(symbol))

    def _previous_close(self, day, symbol):
        if day <= 0:
            return np.nan
        previous = self.panel.exec_close[:day, symbol]
        valid = previous[np.isfinite(previous) & (previous > 0)]
        return float(valid[-1]) if len(valid) else np.nan

    def _limit_rule(self, day, symbol):
        previous = self._previous_close(day, symbol)
        if not np.isfinite(previous):
            return previous, None
        if self.panel.list_date[symbol] < int(self.panel.dates[0]):
            # The panel may be a window cut from a much longer security life.
            # Six is sufficient to move every supported board beyond its
            # special first-five-session regime.
            listing_session = 6
        else:
            listing_session = int(self.panel.universe_mask[:day + 1, symbol].sum())
        rule = price_limit_rule(
            str(self.panel.board[symbol]), int(self.panel.dates[day]),
            bool(self.panel.st_status[day, symbol]), listing_session,
            bool(self.panel.st_status_known[day, symbol]),
        )
        return previous, rule

    def _limits(self, day, symbol):
        if self.config.mode == "limit_reference_v2":
            date = int(self.panel.dates[day])
            symbol_name = str(self.panel.symbols[symbol])
            date_text = str(date)
            decision_asof = "{}-{}-{}T09:15:00+08:00".format(
                date_text[:4], date_text[4:6], date_text[6:])
            record = self.limit_references.get(
                date, symbol_name, as_of=decision_asof)
            listing_session = (6 if self.panel.list_date[symbol] < int(self.panel.dates[0])
                               else int(self.panel.universe_mask[:day + 1, symbol].sum()))
            listing_stage = ("ipo_session_1" if listing_session == 1 else
                             "ipo_first_5" if listing_session <= 5 else "seasoned")
            context = LimitRuleContext(
                exchange=symbol_name[:2], board=str(self.panel.board[symbol]),
                trade_date=date,
                security_status=("risk_warning" if self.panel.st_status[day, symbol]
                                 else "normal"),
                listing_stage=listing_stage,
                status_known=bool(self.panel.st_status_known[day, symbol]),
            )
            rule = limit_rule_for_context(context)
            reasons = list(record.reason_codes) + list(rule.reason_codes)
            previous, legacy_rule = self._limit_rule(day, symbol)
            if (record.has_reference and record.previous_raw_close is not None and
                    abs(float(record.limit_reference_price_raw) -
                        float(record.previous_raw_close)) > 0.005):
                reasons.append("REFERENCE_PRICE_CHANGED")
            if (rule.known and legacy_rule is not None and
                    (rule.upper_fraction != legacy_rule.upper_fraction or
                     rule.lower_fraction != legacy_rule.lower_fraction)):
                reasons.append("LIMIT_REGIME_CHANGED")
            if record.has_direct_limits:
                lower = float(record.exchange_lower_limit_raw)
                upper = float(record.exchange_upper_limit_raw)
                rule_id = rule.rule_id if rule.known else "direct_exchange_limits"
                quality = record.quality
            elif record.has_reference and rule.known:
                lower, upper = limit_prices(record.limit_reference_price_raw, rule)
                rule_id = rule.rule_id
                quality = record.quality
            else:
                # Execution can fail closed with a conservative legacy band;
                # this never upgrades the unknown fact record to known.
                if legacy_rule is None:
                    return None, None, "missing_reference", PRICE_LIMIT_MODEL_VERSION, \
                        "unknown", tuple(dict.fromkeys(reasons))
                lower, upper = limit_prices(previous, legacy_rule)
                rule_id = "execution_fallback_{}".format(legacy_rule.rule_id)
                quality = "unknown"
                reasons.extend(("UNKNOWN_REFERENCE_PRICE", "LIMIT_REGIME_FALLBACK"))
            detail = {
                "date": date, "symbol": symbol_name,
                "execution_limit_model_version": PRICE_LIMIT_MODEL_VERSION,
                "limit_rule_id": rule_id, "reference_quality": quality,
                "reason_codes": tuple(dict.fromkeys(reasons)),
            }
            self.limit_audit.append(detail)
            return (lower, upper, rule_id, PRICE_LIMIT_MODEL_VERSION, quality,
                    detail["reason_codes"])
        previous, rule = self._limit_rule(day, symbol)
        if rule is None:
            return (None, None, "missing_previous_close", "legacy_v1", "",
                    ("UNKNOWN_REFERENCE_PRICE",))
        lower, upper = limit_prices(previous, rule)
        return lower, upper, rule.rule_id, "legacy_v1", "", ()

    def _record_unfilled(self, order, day, status, reason, rule_id="",
                         limit_model="legacy_v1", reference_quality="",
                         limit_reasons=()):
        fill = Fill(
            order_id=order.order_id, intent_id=order.intent_id,
            date=int(self.panel.dates[day]), symbol=order.symbol,
            side=order.side, status=status, quantity=0,
            reason_code=reason, limit_rule_id=rule_id,
            execution_limit_model_version=limit_model,
            limit_reference_quality=reference_quality,
            limit_reason_codes=tuple(limit_reasons),
        )
        self.fills.append(fill)
        return fill

    def _fill_buy(self, order, day):
        symbol = self._symbol(order.symbol)
        opening = float(self.panel.exec_open[day, symbol])
        legacy_locked = (self.config.mode == "legacy_compat" and
                         not self.panel.exec_high[day, symbol] >
                         self.panel.exec_low[day, symbol])
        if (not self.panel.buy_tradable_mask[day, symbol] or legacy_locked or
                not np.isfinite(opening) or opening <= 0):
            self._release(order)
            return self._record_unfilled(order, day, "rejected", "NOT_BUY_TRADABLE")
        lower, upper, rule_id, limit_model, reference_quality, limit_reasons = \
            self._limits(day, symbol)
        if (self.config.mode in ("pit_corrected", "limit_reference_v2") and
                not can_buy_at_open(opening, upper)):
            self._release(order)
            return self._record_unfilled(
                order, day, "rejected", "OPEN_AT_LIMIT_UP", rule_id,
                limit_model, reference_quality, limit_reasons
            )
        if opening > order.max_buy_price_raw + 1e-12:
            self._release(order)
            return self._record_unfilled(
                order, day, "rejected", "ABOVE_MAX_BUY_PRICE", rule_id,
                limit_model, reference_quality, limit_reasons
            )
        price = opening * (1 + self.config.slippage_bps / 10000.0)
        if ((upper is not None and price > upper + 1e-12) or
                price > order.max_buy_price_raw + 1e-12):
            self._release(order)
            return self._record_unfilled(
                order, day, "rejected", "SLIPPAGE_EXCEEDS_LIMIT", rule_id,
                limit_model, reference_quality, limit_reasons
            )
        if order.symbol in self.positions:
            self._release(order)
            return self._record_unfilled(order, day, "rejected", "DUPLICATE_POSITION")
        if (self.config.max_positions is not None and
                len(self.positions) >= self.config.max_positions):
            self._release(order)
            return self._record_unfilled(order, day, "rejected", "MAX_POSITIONS")
        commission, transfer, stamp = self._fees(order.quantity, price, "buy")
        cost = order.quantity * price + commission + transfer + stamp
        self._release(order)
        if cost > self.available_cash + 1e-9:
            return self._record_unfilled(order, day, "rejected", "INSUFFICIENT_CASH")
        self.cash -= cost
        initial_r = max(0.0, price - float(order.initial_stop_raw or price))
        self.positions[order.symbol] = Position(
            symbol=order.symbol, quantity=order.quantity,
            entry_date=int(self.panel.dates[day]), entry_price_raw=price,
            total_cost=cost, strategy_id=order.strategy_id,
            strategy_version=order.strategy_version,
            initial_stop_raw=order.initial_stop_raw,
            initial_r_per_share_raw=initial_r,
            initial_r_cash_frozen=initial_r * order.quantity,
        )
        fill = Fill(
            order_id=order.order_id, intent_id=order.intent_id,
            date=int(self.panel.dates[day]), symbol=order.symbol, side="buy",
            status="filled", quantity=order.quantity,
            reference_price=opening, fill_price_raw=price,
            commission=commission, transfer_fee=transfer,
            stamp_tax=stamp,
            slippage_cost=order.quantity * (price - opening),
            actual_initial_r_per_share_raw=initial_r,
            actual_initial_r_cash=initial_r * order.quantity,
            limit_rule_id=rule_id,
            execution_limit_model_version=limit_model,
            limit_reference_quality=reference_quality,
            limit_reason_codes=tuple(limit_reasons),
        )
        self.fills.append(fill)
        return fill

    def _fill_sell(self, order, day):
        symbol = self._symbol(order.symbol)
        position = self.positions.get(order.symbol)
        if position is None:
            self._release(order)
            return self._record_unfilled(order, day, "rejected", "NO_POSITION")
        if order.quantity > position.quantity:
            self._release(order)
            return self._record_unfilled(order, day, "rejected", "QUANTITY_EXCEEDS_POSITION")
        opening = float(self.panel.exec_open[day, symbol])
        legacy_locked = (self.config.mode == "legacy_compat" and
                         not self.panel.exec_high[day, symbol] >
                         self.panel.exec_low[day, symbol])
        if (not self.panel.sell_tradable_mask[day, symbol] or legacy_locked or
                not np.isfinite(opening) or opening <= 0):
            return self._record_unfilled(order, day, "deferred", "NOT_SELL_TRADABLE")
        lower, upper, rule_id, limit_model, reference_quality, limit_reasons = \
            self._limits(day, symbol)
        if (self.config.mode in ("pit_corrected", "limit_reference_v2") and
                not can_sell_at_open(opening, lower)):
            return self._record_unfilled(
                order, day, "deferred", "OPEN_AT_LIMIT_DOWN", rule_id,
                limit_model, reference_quality, limit_reasons
            )
        price = opening * (1 - self.config.slippage_bps / 10000.0)
        if lower is not None and price < lower - 1e-12:
            return self._record_unfilled(
                order, day, "deferred", "SLIPPAGE_EXCEEDS_LIMIT", rule_id,
                limit_model, reference_quality, limit_reasons
            )
        commission, transfer, stamp = self._fees(order.quantity, price, "sell")
        proceeds = order.quantity * price - commission - transfer - stamp
        self.cash += proceeds
        if order.quantity == position.quantity:
            self.positions.pop(order.symbol)
        else:
            remaining = position.quantity - order.quantity
            self.positions[order.symbol] = Position(
                **{**position.__dict__, "quantity": remaining,
                   "total_cost": position.total_cost * remaining / position.quantity,
                   "initial_r_cash_frozen": (position.initial_r_cash_frozen *
                                             remaining / position.quantity)}
            )
        self._release(order)
        fill = Fill(
            order_id=order.order_id, intent_id=order.intent_id,
            date=int(self.panel.dates[day]), symbol=order.symbol, side="sell",
            status="filled", quantity=order.quantity,
            reference_price=opening, fill_price_raw=price,
            commission=commission, transfer_fee=transfer,
            stamp_tax=stamp,
            slippage_cost=order.quantity * (opening - price),
            limit_rule_id=rule_id,
            execution_limit_model_version=limit_model,
            limit_reference_quality=reference_quality,
            limit_reason_codes=tuple(limit_reasons),
        )
        self.fills.append(fill)
        return fill

    def _credit_receivables(self, day):
        date = int(self.panel.dates[day])
        for item in self._cash_receivables.pop(day, []):
            self.cash += item["cash"]
            self.position_events.append(PositionEvent(
                date=date, symbol=item["symbol"], event_type="CASH_DIVIDEND",
                cash_delta=item["cash"], reason=item.get("reason", ""),
            ))
        for item in self._share_receivables.pop(day, []):
            position = self.positions.get(item["symbol"])
            if position is None:
                continue
            self.positions[item["symbol"]] = Position(
                **{**position.__dict__,
                   "quantity": position.quantity + item["quantity"],
                   "entry_price_raw": position.entry_price_raw / item["factor"],
                   "initial_stop_raw": (position.initial_stop_raw / item["factor"]
                                        if position.initial_stop_raw is not None else None)}
            )
            self.position_events.append(PositionEvent(
                date=date, symbol=item["symbol"], event_type="STOCK_DIVIDEND",
                quantity_delta=item["quantity"], reason=item.get("reason", ""),
            ))

    def process_open(self, day):
        self._credit_receivables(day)
        date = int(self.panel.dates[day])
        fills = []
        pending = []
        active = sorted(
            self.orders,
            key=lambda item: (0 if item.side == "sell" else 1,
                              item.strategy_id, item.symbol, item.order_id),
        )
        self.orders = []
        for order in active:
            if order.side == "buy" and date != order.valid_session:
                self._release(order)
                fills.append(self._record_unfilled(
                    order, day, "expired", "BUY_ORDER_EXPIRED"
                ))
                continue
            if order.side == "sell" and date < order.valid_session:
                pending.append(order)
                continue
            fill = self._fill_sell(order, day) if order.side == "sell" \
                else self._fill_buy(order, day)
            fills.append(fill)
            if order.side == "sell" and fill.status == "deferred":
                pending.append(order)
        self.orders.extend(pending)
        return fills

    def process_close(self, day):
        date = int(self.panel.dates[day])
        observed = self.panel.exec_close[day]
        fresh = np.isfinite(observed) & (observed > 0)
        self.last_close[fresh] = observed[fresh]

        for event in self.panel.corporate_actions.get(day, []):
            symbol = self.panel.symbols[event["symbol"]]
            position = self.positions.get(symbol)
            if position is None:
                continue
            if event["cash_per_share"] and event["cash_day"] is not None:
                self._cash_receivables.setdefault(event["cash_day"], []).append({
                    "symbol": symbol,
                    "cash": position.quantity * event["cash_per_share"],
                    "reason": event.get("description", ""),
                })
            if event["stock_per_share"] and event["stock_day"] is not None:
                quantity = int(np.floor(position.quantity * event["stock_per_share"]))
                if quantity:
                    self._share_receivables.setdefault(event["stock_day"], []).append({
                        "symbol": symbol, "quantity": quantity,
                        "factor": 1.0 + float(event["stock_per_share"]),
                        "reason": event.get("description", ""),
                    })

        holdings = 0.0
        stale_value = 0.0
        liquidation = {1: 0.0, 3: 0.0, 5: 0.0}
        for symbol, position in self.positions.items():
            column = self._symbol(symbol)
            terminated = bool(self.panel.terminated_mask[day, column])
            mark = accounting_mark(
                float(observed[column]), float(self.last_close[column]), terminated
            )
            value = position.quantity * mark
            holdings += value
            if self.panel.suspended_mask[day, column]:
                stale_value += value
            _, rule = self._limit_rule(day, column)
            limit_fraction = (float(rule.lower_fraction)
                              if rule is not None and
                              rule.lower_fraction is not None else 0.05)
            for limits in liquidation:
                liquidation[limits] += value * (1-limit_fraction) ** limits
        capital = self.cash + holdings
        row = {
            "date": date, "cash": self.cash, "stocks": holdings,
            "capital": capital,
            "exposure": holdings / capital if capital > 0 else np.nan,
            "holdings": len(self.positions), "stale_value": stale_value,
            "reserved_cash": self.reserved_cash,
            "liquidation_nav_1_limit": self.cash + liquidation[1],
            "liquidation_nav_3_limits": self.cash + liquidation[3],
            "liquidation_nav_5_limits": self.cash + liquidation[5],
            "liquidation_nav_zero_stale": self.cash + holdings - stale_value,
        }
        self.curve.append(row)
        return row

    def curve_frame(self):
        return pd.DataFrame(self.curve)

    def fills_frame(self):
        return pd.DataFrame([fill.__dict__ for fill in self.fills])

    def orders_frame(self):
        return pd.DataFrame([order.__dict__ for order in self.order_history])

    def reservations_frame(self):
        return pd.DataFrame([item.__dict__ for item in self.reservation_history])

    def position_events_frame(self):
        return pd.DataFrame([item.__dict__ for item in self.position_events])
