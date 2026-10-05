from __future__ import absolute_import

import json
from dataclasses import asdict, dataclass

import pandas as pd

from ..AlphaBu.ABuIntradayExecution import (
    IntradayExecutionConfig, IntradayOrderMachine, instruction_from_order,
)
from ..AlphaBu.ABuTradeIntent import ApprovedOrder, make_record_id
from .ABuTransactionalAccount import AccountEventEffects
from .ABuPaperLedgerStore import TransactionalPaperLedger


PRICE_MICROS = 1_000_000


@dataclass(frozen=True)
class TransactionalExecutionCosts:
    broker_rate: float = 0.00025
    min_commission: float = 5.0
    transfer_rate: float = 0.00001
    sell_stamp_rate: float = 0.0005

    def __post_init__(self):
        if any(value < 0 for value in (
                self.broker_rate, self.min_commission, self.transfer_rate,
                self.sell_stamp_rate)):
            raise ValueError("execution costs must be non-negative")

    def buy_fees_micros(self, quantity, price_raw):
        gross = int(quantity) * float(price_raw)
        commission = max(gross * self.broker_rate, self.min_commission)
        transfer = gross * self.transfer_rate
        return int(round((commission + transfer) * PRICE_MICROS))

    def sell_fees_micros(self, quantity, price_raw):
        gross = int(quantity) * float(price_raw)
        commission = max(gross * self.broker_rate, self.min_commission)
        transfer = gross * self.transfer_rate
        stamp = gross * self.sell_stamp_rate
        return int(round((commission + transfer + stamp) * PRICE_MICROS))


class TransactionalIntradayBroker(object):
    """Persist frozen M1/M2 order machines through the account commit point."""

    POLICY_FAMILY = "hybrid_intraday_entry_v1"

    def __init__(self, accounts, *, execution_policy_id,
                 intraday_config=None, costs=None, ledger=None,
                 exit_policy_id="daily_exit_v1", exit_policy_version="1",
                 risk_policy_id="portfolio_risk_v1", risk_policy_version="1"):
        if execution_policy_id not in ("M1", "M2"):
            raise ValueError("execution_policy_id must be M1 or M2")
        self.accounts = accounts
        self.execution_policy_id = execution_policy_id
        self.config = intraday_config or IntradayExecutionConfig()
        self.costs = costs or TransactionalExecutionCosts()
        self.ledger = ledger or TransactionalPaperLedger()
        self.exit_policy_id = str(exit_policy_id)
        self.exit_policy_version = str(exit_policy_version)
        self.risk_policy_id = str(risk_policy_id)
        self.risk_policy_version = str(risk_policy_version)
        if not all((self.exit_policy_id, self.exit_policy_version,
                    self.risk_policy_id, self.risk_policy_version)):
            raise ValueError("trade policy identities are required")

    @staticmethod
    def _micros(value):
        return int(round(float(value) * PRICE_MICROS))

    @staticmethod
    def _validate_order_namespace(order, context):
        if order.side != "buy":
            raise ValueError("hybrid intraday v1 only accepts buy orders")
        if order.position_effect not in ("", "OPEN"):
            raise ValueError("hybrid intraday v1 does not support scale-in")
        if order.account_id and order.account_id != context.account_id:
            raise ValueError("approved order belongs to another account")
        if (order.strategy_instance_id and
                order.strategy_instance_id != context.strategy_instance_id):
            raise ValueError("approved order strategy instance mismatch")
        if (order.actor_activation_id and
                order.actor_activation_id != context.active_activation_id):
            raise ValueError("approved order activation mismatch")

    def register_approved_order(
            self, account_id, input_event_id, expected_account_version,
            approved_order, *, reservation_id, reserved_cash_micros,
            reserved_risk_micros, risk_decision_id, processed_at,
            upper_limit_raw=None):
        """Persist one frozen approved order and its initial ACTIVE machine."""
        if not isinstance(approved_order, ApprovedOrder):
            raise TypeError("approved_order must be ApprovedOrder")
        reserved_cash_micros = int(reserved_cash_micros)
        reserved_risk_micros = int(reserved_risk_micros)
        minimum_reservation = (
            self._micros(
                approved_order.quantity * approved_order.max_buy_price_raw) +
            self.costs.buy_fees_micros(
                approved_order.quantity, approved_order.max_buy_price_raw))
        if reserved_cash_micros < minimum_reservation:
            raise ValueError("frozen reservation is below maximum buy cost")
        if reserved_risk_micros < 0:
            raise ValueError("reserved risk cannot be negative")

        def handler(connection, context, input_event):
            self._validate_order_namespace(approved_order, context)
            if input_event["event_type"] != "ApprovedOrderCommitted":
                raise ValueError("approved order input event type mismatch")
            input_payload = json.loads(input_event["payload_json"])
            if input_payload.get("approved_order") != asdict(approved_order):
                raise ValueError("approved order input payload mismatch")
            if int(input_event["trading_session"] or 0) != int(
                    approved_order.valid_session):
                raise ValueError("approved order event trading session mismatch")
            account_session = connection.execute(
                "SELECT phase, blocked_reason FROM account_sessions "
                "WHERE account_id=? AND trading_session=?",
                (account_id, int(approved_order.valid_session))).fetchone()
            if (account_session is None or
                    account_session["phase"] != "CREATED" or
                    account_session["blocked_reason"] is not None):
                raise ValueError(
                    "approved orders must be frozen before preopen readiness")
            balance = connection.execute(
                "SELECT * FROM account_balances WHERE account_id=?",
                (account_id,)).fetchone()
            available = int(balance["cash_micros"]) - int(
                balance["reserved_cash_micros"])
            if reserved_cash_micros <= 0 or reserved_cash_micros > available:
                raise ValueError("insufficient cash for frozen reservation")
            self.ledger.insert_order(
                connection, context, order_id=approved_order.order_id,
                intent_id=approved_order.intent_id,
                symbol=approved_order.symbol, side="buy",
                quantity=approved_order.quantity, status="APPROVED",
                valid_session=approved_order.valid_session,
                created_at=processed_at, updated_at=processed_at,
                trade_id=make_record_id(
                    "trade", context.account_id, approved_order.order_id),
                actor_activation_id=(
                    approved_order.actor_activation_id or None))
            self.ledger.insert_reservation(
                connection, context, reservation_id=reservation_id,
                order_id=approved_order.order_id,
                reserved_cash_micros=reserved_cash_micros,
                reserved_risk_micros=reserved_risk_micros,
                status="ACTIVE", created_at=processed_at,
                updated_at=processed_at)
            self.ledger.insert_risk_decision(
                connection, context, risk_decision_id=risk_decision_id,
                order_id=approved_order.order_id, decision="APPROVE",
                reason_codes=("WITHIN_RISK_BUDGET",),
                policy_id="portfolio_risk_v1", policy_version="1",
                source_snapshot_id=(
                    approved_order.source_snapshot_id or None),
                created_at=processed_at)
            self.ledger.set_balance(
                connection, context,
                cash_micros=int(balance["cash_micros"]),
                reserved_cash_micros=(
                    int(balance["reserved_cash_micros"]) +
                    reserved_cash_micros),
                updated_at=processed_at)
            instruction = instruction_from_order(
                approved_order, approved_order.valid_session,
                self.execution_policy_id, self.config,
                created_at=processed_at, reservation_id=reservation_id)
            machine = IntradayOrderMachine(
                instruction, approved_order, config=self.config,
                upper_limit_raw=upper_limit_raw)
            state = machine.export_state()
            self.ledger.stage_execution_state(
                connection, context, order_id=approved_order.order_id,
                execution_policy_id=self.execution_policy_id,
                state=machine.state, last_consumed_minute_sequence=0,
                machine_state=state, updated_at=processed_at)
            return AccountEventEffects(
                account_event_type="IntradayOrderRegistered",
                account_event_payload={
                    "policy_family": self.POLICY_FAMILY,
                    "execution_policy_id": self.execution_policy_id,
                    "approved_order": asdict(approved_order),
                    "execution_states": {approved_order.order_id: state},
                },
                value={
                    "order_id": approved_order.order_id,
                    "state": machine.state,
                    "reservation_id": reservation_id,
                },
                transitioned_order_ids=(approved_order.order_id,),
                trading_session=approved_order.valid_session)

        return self.accounts.execute_event(
            account_id, input_event_id, expected_account_version,
            processed_at, handler)

    @staticmethod
    def _active_reservation(connection, account_id, order_id):
        row = connection.execute(
            "SELECT * FROM reservations WHERE account_id=? AND order_id=? "
            "AND status='ACTIVE'", (account_id, order_id)).fetchone()
        if row is None:
            raise ValueError("active reservation is required")
        return row

    def _release_terminal(self, connection, context, order_id, state,
                          processed_at):
        reservation = self._active_reservation(
            connection, context.account_id, order_id)
        balance = connection.execute(
            "SELECT * FROM account_balances WHERE account_id=?",
            (context.account_id,)).fetchone()
        release = int(reservation["reserved_cash_micros"])
        if release > int(balance["reserved_cash_micros"]):
            raise ValueError("reservation exceeds account reserved cash")
        order_status = "CANCELLED" if state == "CANCELLED" else "EXPIRED"
        reservation_status = "RELEASED" if state == "CANCELLED" else "EXPIRED"
        self.ledger.transition_order(
            connection, context, order_id, order_status, processed_at)
        self.ledger.transition_reservation(
            connection, context, reservation["reservation_id"],
            reservation_status, processed_at)
        self.ledger.set_balance(
            connection, context, cash_micros=int(balance["cash_micros"]),
            reserved_cash_micros=(
                int(balance["reserved_cash_micros"]) - release),
            updated_at=processed_at)

    def _apply_fill(self, connection, context, machine, outcome, snapshot_id,
                    source_event_id, next_trading_session, processed_at):
        order = machine.order
        reservation = self._active_reservation(
            connection, context.account_id, order.order_id)
        balance = connection.execute(
            "SELECT * FROM account_balances WHERE account_id=?",
            (context.account_id,)).fetchone()
        existing = connection.execute(
            "SELECT quantity FROM positions WHERE account_id=? AND symbol=?",
            (context.account_id, order.symbol)).fetchone()
        if existing is not None and int(existing["quantity"]) > 0:
            raise ValueError("hybrid intraday v1 cannot add to a position")
        if (next_trading_session is None or
                int(next_trading_session) <= int(order.valid_session)):
            raise ValueError("next trading session is required for T+1 lot")
        gross_micros = self._micros(
            order.quantity * outcome.fill_price_raw)
        fees_micros = self.costs.buy_fees_micros(
            order.quantity, outcome.fill_price_raw)
        total_cost = gross_micros + fees_micros
        reserved = int(reservation["reserved_cash_micros"])
        if (total_cost > int(balance["cash_micros"]) or
                reserved > int(balance["reserved_cash_micros"])):
            raise ValueError("frozen buy no longer has sufficient cash")
        fill_id = make_record_id(
            "fill", context.account_id, order.order_id,
            machine.instruction.trading_date)
        self.ledger.insert_fill(
            connection, context, fill_id=fill_id,
            order_id=order.order_id, symbol=order.symbol, side="buy",
            quantity=order.quantity,
            reference_price_micros=self._micros(outcome.reference_price),
            fill_price_micros=self._micros(outcome.fill_price_raw),
            fees_micros=fees_micros, source_snapshot_id=snapshot_id,
            occurred_at=outcome.available_at or processed_at)
        self.ledger.transition_order(
            connection, context, order.order_id, "FILLED", processed_at)
        self.ledger.transition_reservation(
            connection, context, reservation["reservation_id"], "CONSUMED",
            processed_at)
        self.ledger.set_balance(
            connection, context,
            cash_micros=int(balance["cash_micros"]) - total_cost,
            reserved_cash_micros=(
                int(balance["reserved_cash_micros"]) - reserved),
            updated_at=processed_at)
        self.ledger.set_position(
            connection, context, symbol=order.symbol,
            quantity=order.quantity, sellable_quantity=0,
            average_cost_micros=int(round(total_cost / order.quantity)),
            updated_at=processed_at)
        trade_id = make_record_id(
            "trade", context.account_id, order.order_id)
        activation_id = order.actor_activation_id or context.active_activation_id
        self.ledger.insert_logical_trade(
            connection, context, trade_id=trade_id, symbol=order.symbol,
            opened_under_activation_id=activation_id,
            management_activation_id=activation_id,
            entry_policy_id=self.POLICY_FAMILY,
            entry_policy_version=self.execution_policy_id,
            exit_policy_id=self.exit_policy_id,
            exit_policy_version=self.exit_policy_version,
            risk_policy_id=self.risk_policy_id,
            risk_policy_version=self.risk_policy_version,
            opened_at=outcome.available_at or processed_at)
        lot_id = make_record_id("lot", context.account_id, fill_id)
        self.ledger.insert_position_lot(
            connection, context, lot_id=lot_id, trade_id=trade_id,
            symbol=order.symbol, quantity=order.quantity,
            sellable_on_session=int(next_trading_session),
            cost_price_micros=int(round(total_cost / order.quantity)),
            opened_at=outcome.available_at or processed_at)
        self.ledger.insert_position_event(
            connection, context,
            position_event_id=make_record_id(
                "position-event", context.account_id, fill_id),
            symbol=order.symbol, event_type="BUY_FILLED",
            source_event_id=source_event_id,
            payload={
                "fill_id": fill_id, "trade_id": trade_id,
                "lot_id": lot_id, "quantity": order.quantity,
                "sellable_on_session": int(next_trading_session),
            }, occurred_at=outcome.available_at or processed_at)
        return {
            "fill_id": fill_id, "order_id": order.order_id,
            "trade_id": trade_id, "lot_id": lot_id,
            "symbol": order.symbol, "quantity": order.quantity,
            "fill_price_micros": self._micros(outcome.fill_price_raw),
            "fees_micros": fees_micros,
        }

    def process_snapshot_batch(
            self, account_id, input_event_id, expected_account_version,
            trading_session, snapshot_batch, processed_at,
            next_trading_session=None):
        """Advance every pending frozen order with one pinned minute snapshot."""
        session = int(trading_session)

        def handler(connection, context, input_event):
            if input_event["snapshot_id"] != snapshot_batch.snapshot_id:
                raise ValueError("minute event and batch snapshot mismatch")
            if input_event["stream_id"] != snapshot_batch.stream_id:
                raise ValueError("minute event and batch stream mismatch")
            if int(input_event["sequence_no"]) != int(snapshot_batch.sequence_no):
                raise ValueError("minute event and batch sequence mismatch")
            snapshot = connection.execute(
                "SELECT previous_snapshot_id, decision_cutoff FROM market_snapshots "
                "WHERE snapshot_id=?", (snapshot_batch.snapshot_id,)).fetchone()
            if (snapshot is None or
                    snapshot["previous_snapshot_id"] !=
                    snapshot_batch.previous_snapshot_id or
                    snapshot["decision_cutoff"] != snapshot_batch.decision_cutoff):
                raise ValueError("minute batch metadata is not catalog-pinned")
            rows = connection.execute(
                "SELECT order_id, status FROM orders WHERE account_id=? "
                "AND side='buy' AND valid_session=? "
                "AND status IN ('APPROVED', 'WAITING') ORDER BY order_id",
                (account_id, session)).fetchall()
            states = {}
            fills = []
            outcomes = []
            transitioned = []
            for row in rows:
                persisted = self.ledger.load_execution_state(
                    connection, account_id, row["order_id"])
                if persisted is None:
                    raise ValueError("pending order has no execution state")
                prior_sequence = int(persisted["projection"][
                    "last_consumed_minute_sequence"])
                if int(snapshot_batch.sequence_no) != prior_sequence + 1:
                    raise ValueError(
                        "order expected minute sequence {}, got {}".format(
                            prior_sequence + 1, snapshot_batch.sequence_no))
                machine = IntradayOrderMachine.restore_exported(
                    persisted["machine_state"])
                for minute_bar in snapshot_batch.events_by_symbol.get(
                        machine.order.symbol, ()):
                    machine.on_bar(minute_bar)
                date = str(session)
                session_close = pd.Timestamp(
                    "{}-{}-{}T{}+08:00".format(
                        date[:4], date[4:6], date[6:],
                        machine.config.last_candidate_start)) + pd.Timedelta(
                            minutes=1)
                if (machine.state not in ("FILLED", "CANCELLED", "EXPIRED") and
                        pd.Timestamp(snapshot_batch.decision_cutoff) >= session_close):
                    machine.finalize()
                outcome = machine.outcome()
                state = machine.export_state()
                self.ledger.stage_execution_state(
                    connection, context, order_id=machine.order.order_id,
                    execution_policy_id=self.execution_policy_id,
                    state=machine.state,
                    last_consumed_minute_sequence=snapshot_batch.sequence_no,
                    machine_state=state, updated_at=processed_at)
                if row["status"] == "APPROVED" and machine.state not in (
                        "FILLED", "CANCELLED", "EXPIRED"):
                    self.ledger.transition_order(
                        connection, context, machine.order.order_id,
                        "WAITING", processed_at)
                if machine.state == "FILLED":
                    fills.append(self._apply_fill(
                        connection, context, machine, outcome,
                        snapshot_batch.snapshot_id, input_event["event_id"],
                        next_trading_session, processed_at))
                elif machine.state in ("CANCELLED", "EXPIRED"):
                    self._release_terminal(
                        connection, context, machine.order.order_id,
                        machine.state, processed_at)
                states[machine.order.order_id] = state
                outcomes.append({
                    "order_id": machine.order.order_id,
                    "state": machine.state,
                    "reason_code": machine.reason_code,
                })
                transitioned.append(machine.order.order_id)
            return AccountEventEffects(
                account_event_type="MinuteBuyExecutionAdvanced",
                account_event_payload={
                    "policy_family": self.POLICY_FAMILY,
                    "execution_policy_id": self.execution_policy_id,
                    "snapshot_id": snapshot_batch.snapshot_id,
                    "snapshot_sequence": int(snapshot_batch.sequence_no),
                    "execution_states": states,
                    "outcomes": outcomes,
                    "fills": fills,
                },
                value={"outcomes": outcomes, "fills": fills},
                notification_parts=(
                    ("TEXT", "CHART_IMAGE") if fills else ()),
                transitioned_order_ids=tuple(transitioned),
                trading_session=session)

        return self.accounts.execute_intraday_buy_event(
            account_id, input_event_id, expected_account_version,
            processed_at, session, handler)
