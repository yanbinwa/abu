from __future__ import absolute_import

import json
from dataclasses import asdict, dataclass

from ..AlphaBu.ABuPriceLimit import can_sell_at_open
from ..AlphaBu.ABuTradeIntent import ApprovedOrder, make_record_id
from .ABuTransactionalAccount import AccountEventEffects
from .ABuPaperLedgerStore import TransactionalPaperLedger
from .ABuTransactionalIntradayBroker import PRICE_MICROS, TransactionalExecutionCosts


@dataclass(frozen=True)
class AccountReceivableInstruction:
    receivable_id: str
    symbol: str
    receivable_type: str
    due_session: int
    reason: str
    cash_micros: int = 0
    quantity: int = 0
    trade_id: str = ""

    def __post_init__(self):
        if self.receivable_type not in ("CASH", "SHARE"):
            raise ValueError("receivable_type must be CASH or SHARE")
        if len(str(int(self.due_session))) != 8:
            raise ValueError("due_session must use YYYYMMDD")
        if self.receivable_type == "CASH":
            if self.cash_micros <= 0 or self.quantity or self.trade_id:
                raise ValueError("invalid cash receivable")
        elif self.quantity <= 0 or self.cash_micros or not self.trade_id:
            raise ValueError("invalid share receivable")


@dataclass(frozen=True)
class DailyOpenQuote:
    symbol: str
    opening_raw: float
    sell_tradable: bool
    lower_limit_raw: float | None = None
    limit_rule_id: str = ""
    limit_reference_quality: str = ""

    def __post_init__(self):
        if self.opening_raw <= 0:
            raise ValueError("opening_raw must be positive")
        if self.lower_limit_raw is not None and self.lower_limit_raw <= 0:
            raise ValueError("lower_limit_raw must be positive")


class TransactionalDailyBroker(object):
    """Persist existing next-open sells, T+1 lots and corporate receivables.

    The broker intentionally has no minute-sell API.  Sell decisions remain
    close-derived ApprovedOrder facts and are evaluated once at a later daily
    open through the account phase coordinator.
    """

    POLICY_ID = "daily_open_execution_v1"

    def __init__(self, accounts, coordinator, *, costs=None, ledger=None,
                 slippage_bps=25.0):
        if float(slippage_bps) < 0:
            raise ValueError("slippage_bps cannot be negative")
        self.accounts = accounts
        self.coordinator = coordinator
        self.costs = costs or TransactionalExecutionCosts()
        self.ledger = ledger or TransactionalPaperLedger()
        self.slippage_bps = float(slippage_bps)

    @staticmethod
    def _micros(value):
        return int(round(float(value) * PRICE_MICROS))

    @staticmethod
    def _trade(connection, account_id, trade_id):
        row = connection.execute(
            "SELECT * FROM logical_trades WHERE account_id=? AND trade_id=?",
            (account_id, trade_id)).fetchone()
        if row is None or row["status"] != "OPEN":
            raise ValueError("open target trade is required")
        return row

    def register_receivable(
            self, account_id, input_event_id, expected_account_version,
            instruction, processed_at):
        if not isinstance(instruction, AccountReceivableInstruction):
            raise TypeError("instruction must be AccountReceivableInstruction")

        def handler(connection, context, input_event):
            if input_event["event_type"] != "CorporateActionReceivableDeclared":
                raise ValueError("receivable input event type mismatch")
            payload = json.loads(input_event["payload_json"])
            if payload.get("receivable") != asdict(instruction):
                raise ValueError("receivable input payload mismatch")
            self.ledger.insert_receivable(
                connection, context,
                receivable_id=instruction.receivable_id,
                symbol=instruction.symbol,
                receivable_type=instruction.receivable_type,
                due_session=instruction.due_session,
                source_event_id=input_event_id, reason=instruction.reason,
                created_at=processed_at,
                cash_micros=instruction.cash_micros,
                quantity=instruction.quantity,
                trade_id=instruction.trade_id or None)
            return AccountEventEffects(
                account_event_type="CorporateActionReceivableRegistered",
                account_event_payload={"receivable": asdict(instruction)},
                value={"receivable_id": instruction.receivable_id,
                       "status": "PENDING"},
                affected_trade_ids=(
                    (instruction.trade_id,) if instruction.trade_id else ()),
                trading_session=instruction.due_session)

        return self.accounts.execute_event(
            account_id, input_event_id, expected_account_version,
            processed_at, handler)

    def register_sell_order(
            self, account_id, input_event_id, expected_account_version,
            approved_order, *, reservation_id, processed_at):
        if not isinstance(approved_order, ApprovedOrder):
            raise TypeError("approved_order must be ApprovedOrder")
        if (approved_order.side != "sell" or
                approved_order.position_effect not in ("REDUCE", "CLOSE")):
            raise ValueError("daily broker requires an explicit sell effect")
        if not approved_order.target_trade_id:
            raise ValueError("sell order requires target_trade_id")

        def handler(connection, context, input_event):
            if input_event["event_type"] != "ApprovedOrderCommitted":
                raise ValueError("sell order input event type mismatch")
            payload = json.loads(input_event["payload_json"])
            if payload.get("approved_order") != asdict(approved_order):
                raise ValueError("sell order input payload mismatch")
            target_session = connection.execute(
                "SELECT phase, blocked_reason FROM account_sessions "
                "WHERE account_id=? AND trading_session=?",
                (account_id, int(approved_order.valid_session))).fetchone()
            if (target_session is not None and
                    (target_session["phase"] != "CREATED" or
                     target_session["blocked_reason"] is not None)):
                raise ValueError("sell orders must be frozen before preopen readiness")
            trade = self._trade(
                connection, account_id, approved_order.target_trade_id)
            if trade["symbol"] != approved_order.symbol:
                raise ValueError("sell order symbol differs from target trade")
            eligible = connection.execute(
                "SELECT COALESCE(sum(quantity), 0) FROM position_lots "
                "WHERE account_id=? AND trade_id=? AND sellable_on_session<=?",
                (account_id, approved_order.target_trade_id,
                 int(approved_order.valid_session))).fetchone()[0]
            reserved = connection.execute(
                "SELECT COALESCE(sum(sr.reserved_quantity), 0) "
                "FROM sell_reservations sr JOIN orders o "
                "ON o.account_id=sr.account_id AND o.order_id=sr.order_id "
                "WHERE sr.account_id=? AND sr.symbol=? AND sr.status='ACTIVE' "
                "AND o.trade_id=?",
                (account_id, approved_order.symbol,
                 approved_order.target_trade_id)).fetchone()[0]
            if int(approved_order.quantity) > int(eligible) - int(reserved):
                raise ValueError("sell quantity exceeds T+1 available shares")
            self.ledger.insert_order(
                connection, context, order_id=approved_order.order_id,
                intent_id=approved_order.intent_id,
                symbol=approved_order.symbol, side="sell",
                quantity=approved_order.quantity, status="APPROVED",
                valid_session=approved_order.valid_session,
                created_at=processed_at, updated_at=processed_at,
                trade_id=approved_order.target_trade_id,
                actor_activation_id=(
                    approved_order.actor_activation_id or None))
            self.ledger.insert_sell_reservation(
                connection, context, reservation_id=reservation_id,
                order_id=approved_order.order_id,
                symbol=approved_order.symbol,
                reserved_quantity=approved_order.quantity, status="ACTIVE",
                created_at=processed_at, updated_at=processed_at)
            return AccountEventEffects(
                account_event_type="DailySellOrderRegistered",
                account_event_payload={
                    "execution_policy_id": self.POLICY_ID,
                    "approved_order": asdict(approved_order),
                    "reservation_id": reservation_id,
                },
                value={"order_id": approved_order.order_id,
                       "reservation_id": reservation_id},
                affected_trade_ids=(approved_order.target_trade_id,),
                affected_management_activation_ids=(
                    trade["management_activation_id"],),
                trading_session=approved_order.valid_session)

        return self.accounts.execute_event(
            account_id, input_event_id, expected_account_version,
            processed_at, handler)

    def _apply_share_receivable(
            self, connection, context, receivable, trading_session,
            processed_at):
        trade_id = receivable["trade_id"]
        trade = self._trade(connection, context.account_id, trade_id)
        lots = connection.execute(
            "SELECT * FROM position_lots WHERE account_id=? AND trade_id=? "
            "ORDER BY opened_at, lot_id",
            (context.account_id, trade_id)).fetchall()
        if len(lots) != 1:
            raise ValueError(
                "v1 share receivable requires exactly one open position lot")
        lot = lots[0]
        added = int(receivable["quantity"])
        original_total_cost = int(lot["quantity"]) * int(
            lot["cost_price_micros"])
        new_quantity = int(lot["quantity"]) + added
        new_cost = int(round(original_total_cost / new_quantity))
        connection.execute(
            "UPDATE position_lots SET quantity=?, cost_price_micros=?, "
            "sellable_on_session=min(sellable_on_session, ?) WHERE lot_id=?",
            (new_quantity, new_cost, int(trading_session), lot["lot_id"]))
        position = connection.execute(
            "SELECT * FROM positions WHERE account_id=? AND symbol=?",
            (context.account_id, trade["symbol"])).fetchone()
        if position is None:
            raise ValueError("share receivable aggregate position is missing")
        aggregate_quantity = int(position["quantity"]) + added
        aggregate_total = int(position["quantity"]) * int(
            position["average_cost_micros"])
        self.ledger.set_position(
            connection, context, symbol=trade["symbol"],
            quantity=aggregate_quantity, sellable_quantity=0,
            average_cost_micros=int(round(aggregate_total / aggregate_quantity)),
            updated_at=processed_at)

    def _apply_receivables(
            self, connection, context, input_event, trading_session,
            processed_at):
        session = int(trading_session)
        due = connection.execute(
            "SELECT * FROM account_receivables WHERE account_id=? "
            "AND due_session<=? AND status='PENDING' "
            "ORDER BY due_session, receivable_id",
            (context.account_id, session)).fetchall()
        cash_total = 0
        share_total = 0
        for receivable in due:
            if receivable["receivable_type"] == "CASH":
                cash_total += int(receivable["cash_micros"])
            else:
                self._apply_share_receivable(
                    connection, context, receivable, session, processed_at)
                share_total += int(receivable["quantity"])
            connection.execute(
                "UPDATE account_receivables SET status='APPLIED', applied_at=? "
                "WHERE account_id=? AND receivable_id=? AND status='PENDING'",
                (processed_at, context.account_id,
                 receivable["receivable_id"]))
            self.ledger.insert_position_event(
                connection, context,
                position_event_id=make_record_id(
                    "position-event", context.account_id,
                    receivable["receivable_id"], "applied"),
                symbol=receivable["symbol"],
                event_type="{}_RECEIVABLE_APPLIED".format(
                    receivable["receivable_type"]),
                source_event_id=receivable["source_event_id"],
                payload={
                    "receivable_id": receivable["receivable_id"],
                    "cash_micros": int(receivable["cash_micros"]),
                    "quantity": int(receivable["quantity"]),
                    "due_session": int(receivable["due_session"]),
                }, occurred_at=processed_at)
        if cash_total:
            balance = connection.execute(
                "SELECT * FROM account_balances WHERE account_id=?",
                (context.account_id,)).fetchone()
            self.ledger.set_balance(
                connection, context,
                cash_micros=int(balance["cash_micros"]) + cash_total,
                reserved_cash_micros=int(balance["reserved_cash_micros"]),
                updated_at=processed_at)
        symbols = connection.execute(
            "SELECT symbol FROM positions WHERE account_id=? ORDER BY symbol",
            (context.account_id,)).fetchall()
        for row in symbols:
            self.ledger.refresh_sellable_position(
                connection, context, symbol=row["symbol"],
                trading_session=session, updated_at=processed_at)
        return {
            "receivables_applied": len(due),
            "cash_credited_micros": cash_total,
            "shares_credited": share_total,
        }

    def apply_due_receivables(
            self, account_id, input_event_id, expected_account_version,
            trading_session, expected_phase_version, processed_at):
        return self.coordinator.apply_receivables_and_corporate_actions(
            account_id, input_event_id, expected_account_version,
            trading_session, expected_phase_version, processed_at,
            lambda connection, context, event: self._apply_receivables(
                connection, context, event, trading_session, processed_at))

    @staticmethod
    def _active_sell_reservation(connection, account_id, order_id):
        row = connection.execute(
            "SELECT * FROM sell_reservations WHERE account_id=? "
            "AND order_id=? AND status='ACTIVE'",
            (account_id, order_id)).fetchone()
        if row is None:
            raise ValueError("active sell reservation is required")
        return row

    def _consume_trade_lots(
            self, connection, context, trade_id, quantity, trading_session,
            processed_at):
        remaining = int(quantity)
        lots = connection.execute(
            "SELECT * FROM position_lots WHERE account_id=? AND trade_id=? "
            "AND sellable_on_session<=? ORDER BY opened_at, lot_id",
            (context.account_id, trade_id, int(trading_session))).fetchall()
        for lot in lots:
            if remaining <= 0:
                break
            consumed = min(remaining, int(lot["quantity"]))
            after = int(lot["quantity"]) - consumed
            if after:
                connection.execute(
                    "UPDATE position_lots SET quantity=? WHERE lot_id=?",
                    (after, lot["lot_id"]))
            else:
                connection.execute(
                    "DELETE FROM position_lots WHERE lot_id=?", (lot["lot_id"],))
            remaining -= consumed
        if remaining:
            raise ValueError("sell fill exceeds eligible trade lots")
        open_quantity = connection.execute(
            "SELECT COALESCE(sum(quantity), 0) FROM position_lots "
            "WHERE account_id=? AND trade_id=?",
            (context.account_id, trade_id)).fetchone()[0]
        if int(open_quantity) == 0:
            connection.execute(
                "UPDATE logical_trades SET status='CLOSED', closed_at=? "
                "WHERE account_id=? AND trade_id=? AND status='OPEN'",
                (processed_at, context.account_id, trade_id))

    def _apply_sell_fill(
            self, connection, context, order, reservation, quote,
            source_snapshot_id, source_event_id, trading_session,
            processed_at):
        fill_price = float(quote.opening_raw) * (
            1.0 - self.slippage_bps / 10000.0)
        if (quote.lower_limit_raw is not None and
                fill_price < float(quote.lower_limit_raw) - 1e-12):
            return None, "SLIPPAGE_EXCEEDS_LIMIT"
        fill_price_micros = self._micros(fill_price)
        fees_micros = self.costs.sell_fees_micros(
            int(order["quantity"]), fill_price)
        gross_micros = self._micros(int(order["quantity"]) * fill_price)
        proceeds = gross_micros - fees_micros
        if proceeds < 0:
            raise ValueError("sell fees exceed proceeds")
        fill_id = make_record_id(
            "fill", context.account_id, order["order_id"],
            int(trading_session))
        trade = self._trade(connection, context.account_id, order["trade_id"])
        self.ledger.insert_fill(
            connection, context, fill_id=fill_id,
            order_id=order["order_id"], symbol=order["symbol"], side="sell",
            quantity=int(order["quantity"]),
            reference_price_micros=self._micros(quote.opening_raw),
            fill_price_micros=fill_price_micros, fees_micros=fees_micros,
            source_snapshot_id=source_snapshot_id, occurred_at=processed_at)
        self._consume_trade_lots(
            connection, context, order["trade_id"], int(order["quantity"]),
            trading_session, processed_at)
        position = connection.execute(
            "SELECT * FROM positions WHERE account_id=? AND symbol=?",
            (context.account_id, order["symbol"])).fetchone()
        new_quantity = int(position["quantity"]) - int(order["quantity"])
        if new_quantity < 0:
            raise ValueError("sell fill exceeds aggregate position")
        if new_quantity == 0:
            connection.execute(
                "DELETE FROM positions WHERE account_id=? AND symbol=?",
                (context.account_id, order["symbol"]))
        else:
            total_cost = connection.execute(
                "SELECT COALESCE(sum(quantity * cost_price_micros), 0) "
                "FROM position_lots WHERE account_id=? AND symbol=?",
                (context.account_id, order["symbol"])).fetchone()[0]
            self.ledger.set_position(
                connection, context, symbol=order["symbol"],
                quantity=new_quantity, sellable_quantity=0,
                average_cost_micros=int(round(int(total_cost) / new_quantity)),
                updated_at=processed_at)
            self.ledger.refresh_sellable_position(
                connection, context, symbol=order["symbol"],
                trading_session=trading_session, updated_at=processed_at)
        balance = connection.execute(
            "SELECT * FROM account_balances WHERE account_id=?",
            (context.account_id,)).fetchone()
        self.ledger.set_balance(
            connection, context,
            cash_micros=int(balance["cash_micros"]) + proceeds,
            reserved_cash_micros=int(balance["reserved_cash_micros"]),
            updated_at=processed_at)
        self.ledger.transition_order(
            connection, context, order["order_id"], "FILLED", processed_at)
        self.ledger.transition_sell_reservation(
            connection, context, reservation["reservation_id"], "CONSUMED",
            processed_at)
        self.ledger.insert_position_event(
            connection, context,
            position_event_id=make_record_id(
                "position-event", context.account_id, fill_id),
            symbol=order["symbol"], event_type="SELL_FILLED",
            source_event_id=source_event_id,
            payload={
                "fill_id": fill_id, "trade_id": order["trade_id"],
                "quantity": int(order["quantity"]),
                "fees_micros": fees_micros,
            }, occurred_at=processed_at)
        return {
            "fill_id": fill_id, "order_id": order["order_id"],
            "trade_id": order["trade_id"], "symbol": order["symbol"],
            "management_activation_id": trade["management_activation_id"],
            "quantity": int(order["quantity"]),
            "fill_price_micros": fill_price_micros,
            "fees_micros": fees_micros,
        }, None

    def _process_open_sells(
            self, connection, context, input_event, trading_session, quotes,
            processed_at):
        session = int(trading_session)
        account_session = connection.execute(
            "SELECT preopen_snapshot_id FROM account_sessions "
            "WHERE account_id=? AND trading_session=?",
            (context.account_id, session)).fetchone()
        source_snapshot_id = account_session["preopen_snapshot_id"]
        rows = connection.execute(
            "SELECT * FROM orders WHERE account_id=? AND side='sell' "
            "AND valid_session<=? AND status IN ('APPROVED', 'WAITING') "
            "ORDER BY valid_session, order_id",
            (context.account_id, session)).fetchall()
        fills = []
        outcomes = []
        for order in rows:
            reservation = self._active_sell_reservation(
                connection, context.account_id, order["order_id"])
            quote = quotes.get(order["symbol"])
            reason = None
            if quote is None or not quote.sell_tradable:
                reason = "NOT_SELL_TRADABLE"
            elif not can_sell_at_open(
                    quote.opening_raw, quote.lower_limit_raw):
                reason = "OPEN_AT_LIMIT_DOWN"
            if reason is None:
                fill, reason = self._apply_sell_fill(
                    connection, context, order, reservation, quote,
                    source_snapshot_id, input_event["event_id"], session,
                    processed_at)
                if fill is not None:
                    fills.append(fill)
            if reason is not None:
                if order["status"] == "APPROVED":
                    self.ledger.transition_order(
                        connection, context, order["order_id"], "WAITING",
                        processed_at)
                outcomes.append({
                    "order_id": order["order_id"], "status": "deferred",
                    "reason_code": reason,
                })
            else:
                outcomes.append({
                    "order_id": order["order_id"], "status": "filled",
                    "reason_code": "FILLED",
                })
        return {"fills": fills, "outcomes": outcomes}

    def process_open_sells(
            self, account_id, input_event_id, expected_account_version,
            trading_session, expected_phase_version, quotes, processed_at):
        normalized = {}
        for symbol, quote in quotes.items():
            if not isinstance(quote, DailyOpenQuote) or quote.symbol != symbol:
                raise TypeError("quotes must map symbol to DailyOpenQuote")
            normalized[symbol] = quote

        def apply(connection, context, event):
            return self._process_open_sells(
                connection, context, event, trading_session, normalized,
                processed_at)

        return self.coordinator.process_open_sells(
            account_id, input_event_id, expected_account_version,
            trading_session, expected_phase_version, processed_at, apply,
            notification_parts=lambda value: (
                ("TEXT", "CHART_IMAGE") if value["fills"] else ()),
            affected_trade_ids=lambda value: tuple(sorted({
                item["trade_id"] for item in value["fills"]})),
            affected_management_activation_ids=lambda value: tuple(sorted({
                item["management_activation_id"]
                for item in value["fills"]})))
