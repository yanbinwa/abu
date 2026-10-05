from __future__ import absolute_import

import json


class LedgerIdentityCollision(ValueError):
    """An immutable ledger identity was reused with different facts."""


class InvalidLedgerTransition(ValueError):
    """A persisted order or reservation transition is not allowed in v1."""


def _require_non_negative(**values):
    for name, value in values.items():
        if int(value) < 0:
            raise ValueError("{} cannot be negative".format(name))


class TransactionalPaperLedger(object):
    """Integer-micro projections written inside an account command transaction."""

    ORDER_TRANSITIONS = {
        "APPROVED": frozenset((
            "WAITING", "FILLED", "CANCELLED", "EXPIRED", "REJECTED")),
        "WAITING": frozenset(("FILLED", "CANCELLED", "EXPIRED", "REJECTED")),
        "FILLED": frozenset(), "CANCELLED": frozenset(),
        "EXPIRED": frozenset(), "REJECTED": frozenset(),
    }
    RESERVATION_TRANSITIONS = {
        "ACTIVE": frozenset(("RELEASED", "CONSUMED", "EXPIRED")),
        "RELEASED": frozenset(), "CONSUMED": frozenset(),
        "EXPIRED": frozenset(),
    }
    RECEIVABLE_TRANSITIONS = {
        "PENDING": frozenset(("APPLIED", "CANCELLED")),
        "APPLIED": frozenset(), "CANCELLED": frozenset(),
    }

    @staticmethod
    def _exact_insert(connection, table, key_sql, key_values, fields,
                      insert_sql, insert_values):
        existing = connection.execute(
            "SELECT * FROM {} WHERE {}".format(table, key_sql), key_values
        ).fetchone()
        if existing is not None:
            if any(existing[name] != value for name, value in fields.items()):
                raise LedgerIdentityCollision(
                    "{} identity is already bound".format(table))
            return dict(existing), False
        connection.execute(insert_sql, insert_values)
        row = connection.execute(
            "SELECT * FROM {} WHERE {}".format(table, key_sql), key_values
        ).fetchone()
        return dict(row), True

    @staticmethod
    def _order(connection, account_id, order_id):
        row = connection.execute(
            "SELECT * FROM orders WHERE account_id=? AND order_id=?",
            (account_id, order_id)).fetchone()
        if row is None:
            raise ValueError("account order is required")
        return row

    @staticmethod
    def set_balance(connection, context, *, cash_micros,
                    reserved_cash_micros, updated_at):
        _require_non_negative(
            cash_micros=cash_micros,
            reserved_cash_micros=reserved_cash_micros)
        updated = connection.execute(
            "UPDATE account_balances SET cash_micros=?, reserved_cash_micros=?, "
            "updated_at=? WHERE account_id=?",
            (int(cash_micros), int(reserved_cash_micros), updated_at,
             context.account_id))
        if updated.rowcount != 1:
            raise ValueError("account balance is missing")
        return dict(connection.execute(
            "SELECT * FROM account_balances WHERE account_id=?",
            (context.account_id,)).fetchone())

    @staticmethod
    def set_position(connection, context, *, symbol, quantity,
                     sellable_quantity, average_cost_micros, updated_at):
        _require_non_negative(
            quantity=quantity, sellable_quantity=sellable_quantity,
            average_cost_micros=average_cost_micros)
        if int(sellable_quantity) > int(quantity):
            raise ValueError("sellable quantity cannot exceed position quantity")
        connection.execute(
            "INSERT INTO positions "
            "(account_id, symbol, quantity, sellable_quantity, "
            "average_cost_micros, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(account_id, symbol) DO UPDATE SET "
            "quantity=excluded.quantity, "
            "sellable_quantity=excluded.sellable_quantity, "
            "average_cost_micros=excluded.average_cost_micros, "
            "updated_at=excluded.updated_at",
            (context.account_id, symbol, int(quantity), int(sellable_quantity),
             int(average_cost_micros), updated_at))
        return dict(connection.execute(
            "SELECT * FROM positions WHERE account_id=? AND symbol=?",
            (context.account_id, symbol)).fetchone())

    @classmethod
    def insert_logical_trade(
            cls, connection, context, *, trade_id, symbol,
            opened_under_activation_id, management_activation_id,
            entry_policy_id, entry_policy_version, exit_policy_id,
            exit_policy_version, risk_policy_id, risk_policy_version,
            opened_at):
        for activation_id in (
                opened_under_activation_id, management_activation_id):
            activation = connection.execute(
                "SELECT 1 FROM strategy_activations "
                "WHERE activation_id=? AND account_id=?",
                (activation_id, context.account_id)).fetchone()
            if activation is None:
                raise ValueError("trade activation does not belong to account")
        fields = {
            "symbol": symbol,
            "opened_under_activation_id": opened_under_activation_id,
            "management_activation_id": management_activation_id,
            "entry_policy_id": entry_policy_id,
            "entry_policy_version": entry_policy_version,
            "exit_policy_id": exit_policy_id,
            "exit_policy_version": exit_policy_version,
            "risk_policy_id": risk_policy_id,
            "risk_policy_version": risk_policy_version,
            "opened_at": opened_at, "closed_at": None, "status": "OPEN",
        }
        return cls._exact_insert(
            connection, "logical_trades", "account_id=? AND trade_id=?",
            (context.account_id, trade_id), fields,
            "INSERT INTO logical_trades "
            "(account_id, trade_id, symbol, opened_under_activation_id, "
            "management_activation_id, entry_policy_id, entry_policy_version, "
            "exit_policy_id, exit_policy_version, risk_policy_id, "
            "risk_policy_version, opened_at, closed_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'OPEN')",
            (context.account_id, trade_id, symbol,
             opened_under_activation_id, management_activation_id,
             entry_policy_id, entry_policy_version, exit_policy_id,
             exit_policy_version, risk_policy_id, risk_policy_version,
             opened_at))

    @classmethod
    def insert_position_lot(
            cls, connection, context, *, lot_id, trade_id, symbol, quantity,
            sellable_on_session, cost_price_micros, opened_at):
        if int(quantity) <= 0 or int(cost_price_micros) <= 0:
            raise ValueError("position lot quantity and cost must be positive")
        trade = connection.execute(
            "SELECT symbol, status FROM logical_trades "
            "WHERE account_id=? AND trade_id=?",
            (context.account_id, trade_id)).fetchone()
        if trade is None or trade["symbol"] != symbol or trade["status"] != "OPEN":
            raise ValueError("open account trade is required for lot")
        fields = {
            "account_id": context.account_id, "trade_id": trade_id,
            "symbol": symbol, "quantity": int(quantity),
            "sellable_on_session": int(sellable_on_session),
            "cost_price_micros": int(cost_price_micros),
            "opened_at": opened_at,
        }
        return cls._exact_insert(
            connection, "position_lots", "lot_id=?", (lot_id,), fields,
            "INSERT INTO position_lots "
            "(lot_id, account_id, trade_id, symbol, quantity, "
            "sellable_on_session, cost_price_micros, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (lot_id, context.account_id, trade_id, symbol, int(quantity),
             int(sellable_on_session), int(cost_price_micros), opened_at))

    @staticmethod
    def refresh_sellable_position(
            connection, context, *, symbol, trading_session, updated_at):
        position = connection.execute(
            "SELECT * FROM positions WHERE account_id=? AND symbol=?",
            (context.account_id, symbol)).fetchone()
        if position is None:
            return None
        sellable = connection.execute(
            "SELECT COALESCE(sum(quantity), 0) FROM position_lots "
            "WHERE account_id=? AND symbol=? AND sellable_on_session<=?",
            (context.account_id, symbol, int(trading_session))).fetchone()[0]
        if int(sellable) > int(position["quantity"]):
            raise ValueError("sellable lots exceed aggregate position")
        connection.execute(
            "UPDATE positions SET sellable_quantity=?, updated_at=? "
            "WHERE account_id=? AND symbol=?",
            (int(sellable), updated_at, context.account_id, symbol))
        return dict(connection.execute(
            "SELECT * FROM positions WHERE account_id=? AND symbol=?",
            (context.account_id, symbol)).fetchone())

    @classmethod
    def insert_position_event(
            cls, connection, context, *, position_event_id, symbol,
            event_type, source_event_id, payload, occurred_at):
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
        fields = {
            "account_id": context.account_id, "symbol": symbol,
            "event_type": event_type, "source_event_id": source_event_id,
            "payload_json": encoded, "occurred_at": occurred_at,
        }
        return cls._exact_insert(
            connection, "position_events", "position_event_id=?",
            (position_event_id,), fields,
            "INSERT INTO position_events "
            "(position_event_id, account_id, symbol, event_type, "
            "source_event_id, payload_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (position_event_id, context.account_id, symbol, event_type,
             source_event_id, encoded, occurred_at))

    @classmethod
    def insert_receivable(
            cls, connection, context, *, receivable_id, symbol,
            receivable_type, due_session, source_event_id, reason, created_at,
            cash_micros=0, quantity=0, trade_id=None):
        if receivable_type not in ("CASH", "SHARE"):
            raise ValueError("invalid receivable type")
        if receivable_type == "CASH":
            if int(cash_micros) <= 0 or int(quantity) != 0 or trade_id is not None:
                raise ValueError("cash receivable values are inconsistent")
        elif receivable_type == "SHARE":
            if int(quantity) <= 0 or int(cash_micros) != 0 or not trade_id:
                raise ValueError("share receivable values are inconsistent")
            trade = connection.execute(
                "SELECT symbol, status FROM logical_trades "
                "WHERE account_id=? AND trade_id=?",
                (context.account_id, trade_id)).fetchone()
            if trade is None or trade["symbol"] != symbol or trade["status"] != "OPEN":
                raise ValueError("share receivable requires an open trade")
        event = connection.execute(
            "SELECT account_id FROM domain_events WHERE event_id=?",
            (source_event_id,)).fetchone()
        if event is None or event["account_id"] not in (None, context.account_id):
            raise ValueError("receivable source event is unavailable")
        fields = {
            "symbol": symbol, "trade_id": trade_id,
            "receivable_type": receivable_type,
            "cash_micros": int(cash_micros), "quantity": int(quantity),
            "due_session": int(due_session), "status": "PENDING",
            "source_event_id": source_event_id, "reason": reason,
            "created_at": created_at, "applied_at": None,
        }
        return cls._exact_insert(
            connection, "account_receivables",
            "account_id=? AND receivable_id=?",
            (context.account_id, receivable_id), fields,
            "INSERT INTO account_receivables "
            "(account_id, receivable_id, symbol, trade_id, receivable_type, "
            "cash_micros, quantity, due_session, status, source_event_id, "
            "reason, created_at, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, NULL)",
            (context.account_id, receivable_id, symbol, trade_id,
             receivable_type, int(cash_micros), int(quantity),
             int(due_session), source_event_id, reason, created_at))

    @classmethod
    def insert_sell_reservation(
            cls, connection, context, *, reservation_id, order_id, symbol,
            reserved_quantity, status, created_at, updated_at):
        order = cls._order(connection, context.account_id, order_id)
        if order["side"] != "sell" or order["symbol"] != symbol:
            raise ValueError("sell reservation order mismatch")
        if int(reserved_quantity) <= 0 or status not in cls.RESERVATION_TRANSITIONS:
            raise ValueError("invalid sell reservation")
        fields = {
            "order_id": order_id, "symbol": symbol,
            "reserved_quantity": int(reserved_quantity), "status": status,
            "created_at": created_at, "updated_at": updated_at,
        }
        return cls._exact_insert(
            connection, "sell_reservations",
            "account_id=? AND reservation_id=?",
            (context.account_id, reservation_id), fields,
            "INSERT INTO sell_reservations "
            "(account_id, reservation_id, order_id, symbol, "
            "reserved_quantity, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (context.account_id, reservation_id, order_id, symbol,
             int(reserved_quantity), status, created_at, updated_at))

    @classmethod
    def transition_sell_reservation(
            cls, connection, context, reservation_id, status, updated_at):
        row = connection.execute(
            "SELECT * FROM sell_reservations "
            "WHERE account_id=? AND reservation_id=?",
            (context.account_id, reservation_id)).fetchone()
        if row is None:
            raise ValueError("account sell reservation is required")
        current = row["status"]
        if status == current:
            return dict(row), False
        if status not in cls.RESERVATION_TRANSITIONS.get(current, ()):
            raise InvalidLedgerTransition(
                "sell reservation cannot transition from {} to {}".format(
                    current, status))
        connection.execute(
            "UPDATE sell_reservations SET status=?, updated_at=? "
            "WHERE account_id=? AND reservation_id=?",
            (status, updated_at, context.account_id, reservation_id))
        return dict(connection.execute(
            "SELECT * FROM sell_reservations "
            "WHERE account_id=? AND reservation_id=?",
            (context.account_id, reservation_id)).fetchone()), True

    @classmethod
    def insert_order(cls, connection, context, *, order_id, intent_id,
                     symbol, side, quantity, status, valid_session,
                     created_at, updated_at, trade_id=None,
                     actor_activation_id=None):
        if side not in ("buy", "sell") or status not in cls.ORDER_TRANSITIONS:
            raise ValueError("invalid order side or status")
        if int(quantity) <= 0:
            raise ValueError("order quantity must be positive")
        activation_id = actor_activation_id or context.active_activation_id
        if activation_id is not None:
            activation = connection.execute(
                "SELECT activation_id FROM strategy_activations "
                "WHERE activation_id=? AND account_id=?",
                (activation_id, context.account_id)).fetchone()
            if activation is None:
                raise ValueError("order activation does not belong to account")
        fields = {
            "intent_id": intent_id,
            "strategy_instance_id": context.strategy_instance_id,
            "actor_activation_id": activation_id,
            "trade_id": trade_id,
            "symbol": symbol, "side": side, "quantity": int(quantity),
            "status": status, "valid_session": int(valid_session),
            "created_at": created_at, "updated_at": updated_at,
        }
        return cls._exact_insert(
            connection, "orders", "account_id=? AND order_id=?",
            (context.account_id, order_id), fields,
            "INSERT INTO orders "
            "(account_id, order_id, intent_id, strategy_instance_id, "
            "actor_activation_id, trade_id, symbol, side, quantity, status, "
            "valid_session, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (context.account_id, order_id, intent_id,
             context.strategy_instance_id, activation_id, trade_id, symbol,
             side, int(quantity), status, int(valid_session), created_at,
             updated_at))

    @classmethod
    def transition_order(cls, connection, context, order_id, status, updated_at):
        order = cls._order(connection, context.account_id, order_id)
        current = order["status"]
        if status == current:
            return dict(order), False
        if status not in cls.ORDER_TRANSITIONS.get(current, ()):
            raise InvalidLedgerTransition(
                "order cannot transition from {} to {}".format(current, status))
        connection.execute(
            "UPDATE orders SET status=?, updated_at=? "
            "WHERE account_id=? AND order_id=?",
            (status, updated_at, context.account_id, order_id))
        return dict(cls._order(
            connection, context.account_id, order_id)), True

    @classmethod
    def insert_reservation(cls, connection, context, *, reservation_id,
                           order_id, reserved_cash_micros,
                           reserved_risk_micros, status, created_at, updated_at):
        cls._order(connection, context.account_id, order_id)
        _require_non_negative(
            reserved_cash_micros=reserved_cash_micros,
            reserved_risk_micros=reserved_risk_micros)
        if status not in cls.RESERVATION_TRANSITIONS:
            raise ValueError("invalid reservation status")
        fields = {
            "order_id": order_id,
            "reserved_cash_micros": int(reserved_cash_micros),
            "reserved_risk_micros": int(reserved_risk_micros),
            "status": status, "created_at": created_at,
            "updated_at": updated_at,
        }
        return cls._exact_insert(
            connection, "reservations",
            "account_id=? AND reservation_id=?",
            (context.account_id, reservation_id), fields,
            "INSERT INTO reservations "
            "(account_id, reservation_id, order_id, reserved_cash_micros, "
            "reserved_risk_micros, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (context.account_id, reservation_id, order_id,
             int(reserved_cash_micros), int(reserved_risk_micros), status,
             created_at, updated_at))

    @classmethod
    def transition_reservation(cls, connection, context, reservation_id,
                               status, updated_at):
        row = connection.execute(
            "SELECT * FROM reservations WHERE account_id=? AND reservation_id=?",
            (context.account_id, reservation_id)).fetchone()
        if row is None:
            raise ValueError("account reservation is required")
        current = row["status"]
        if status == current:
            return dict(row), False
        if status not in cls.RESERVATION_TRANSITIONS.get(current, ()):
            raise InvalidLedgerTransition(
                "reservation cannot transition from {} to {}".format(
                    current, status))
        connection.execute(
            "UPDATE reservations SET status=?, updated_at=? "
            "WHERE account_id=? AND reservation_id=?",
            (status, updated_at, context.account_id, reservation_id))
        return dict(connection.execute(
            "SELECT * FROM reservations WHERE account_id=? AND reservation_id=?",
            (context.account_id, reservation_id)).fetchone()), True

    @classmethod
    def insert_fill(cls, connection, context, *, fill_id, order_id,
                    symbol, side, quantity, reference_price_micros,
                    fill_price_micros, fees_micros, source_snapshot_id,
                    occurred_at):
        order = cls._order(connection, context.account_id, order_id)
        if (symbol, side, int(quantity)) != (
                order["symbol"], order["side"], int(order["quantity"])):
            raise ValueError("v1 fill must exactly match its order")
        _require_non_negative(fees_micros=fees_micros)
        if int(reference_price_micros) <= 0 or int(fill_price_micros) <= 0:
            raise ValueError("fill prices must be positive")
        fields = {
            "order_id": order_id, "symbol": symbol, "side": side,
            "quantity": int(quantity),
            "reference_price_micros": int(reference_price_micros),
            "fill_price_micros": int(fill_price_micros),
            "fees_micros": int(fees_micros),
            "source_snapshot_id": source_snapshot_id,
            "occurred_at": occurred_at,
        }
        return cls._exact_insert(
            connection, "fills", "account_id=? AND fill_id=?",
            (context.account_id, fill_id), fields,
            "INSERT INTO fills "
            "(account_id, fill_id, order_id, symbol, side, quantity, "
            "reference_price_micros, fill_price_micros, fees_micros, "
            "source_snapshot_id, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (context.account_id, fill_id, order_id, symbol, side, int(quantity),
             int(reference_price_micros), int(fill_price_micros),
             int(fees_micros), source_snapshot_id, occurred_at))

    @classmethod
    def insert_risk_decision(cls, connection, context, *, risk_decision_id,
                             order_id, decision, reason_codes, policy_id,
                             policy_version, source_snapshot_id, created_at):
        if decision not in ("APPROVE", "RESIZE", "REJECT"):
            raise ValueError("invalid risk decision")
        if order_id is not None:
            cls._order(connection, context.account_id, order_id)
        encoded_reasons = json.dumps(
            sorted(set(reason_codes)), separators=(",", ":"))
        fields = {
            "account_id": context.account_id, "order_id": order_id,
            "decision": decision, "reason_codes_json": encoded_reasons,
            "policy_id": policy_id, "policy_version": policy_version,
            "source_snapshot_id": source_snapshot_id,
            "created_at": created_at,
        }
        return cls._exact_insert(
            connection, "risk_decisions", "risk_decision_id=?",
            (risk_decision_id,), fields,
            "INSERT INTO risk_decisions "
            "(risk_decision_id, account_id, order_id, decision, "
            "reason_codes_json, policy_id, policy_version, source_snapshot_id, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (risk_decision_id, context.account_id, order_id, decision,
             encoded_reasons, policy_id, policy_version, source_snapshot_id,
             created_at))

    @classmethod
    def stage_execution_state(cls, connection, context, *, order_id,
                              execution_policy_id, state,
                              last_consumed_minute_sequence, machine_state,
                              updated_at):
        cls._order(connection, context.account_id, order_id)
        sequence = int(last_consumed_minute_sequence)
        if sequence < 0:
            raise ValueError("minute sequence cannot be negative")
        encoded = json.dumps(
            machine_state, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
        decoded = json.loads(encoded)
        if decoded.get("state") != state:
            raise ValueError("machine state and projection state differ")
        existing = connection.execute(
            "SELECT * FROM order_execution_states "
            "WHERE account_id=? AND order_id=?",
            (context.account_id, order_id)).fetchone()
        if existing is not None:
            if existing["execution_policy_id"] != execution_policy_id:
                raise LedgerIdentityCollision(
                    "order execution policy is already bound")
            if sequence < int(existing["last_consumed_minute_sequence"]):
                raise InvalidLedgerTransition(
                    "minute sequence cannot move backwards")
            if (sequence == int(existing["last_consumed_minute_sequence"]) and
                    existing["state"] != state):
                raise InvalidLedgerTransition(
                    "same minute sequence cannot change execution state")
            connection.execute(
                "UPDATE order_execution_states SET state=?, "
                "last_consumed_minute_sequence=?, updated_at=? "
                "WHERE account_id=? AND order_id=?",
                (state, sequence, updated_at, context.account_id, order_id))
        else:
            connection.execute(
                "INSERT INTO order_execution_states "
                "(account_id, order_id, execution_policy_id, state, "
                "last_consumed_minute_sequence, last_transition_event_id, updated_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                (context.account_id, order_id, execution_policy_id, state,
                 sequence, updated_at))
        return decoded

    @staticmethod
    def load_execution_state(connection, account_id, order_id):
        row = connection.execute(
            "SELECT * FROM order_execution_states "
            "WHERE account_id=? AND order_id=?",
            (account_id, order_id)).fetchone()
        if row is None:
            return None
        event_id = row["last_transition_event_id"]
        if event_id is None:
            raise ValueError("committed execution state has no transition event")
        event = connection.execute(
            "SELECT payload_json FROM domain_events WHERE event_id=?",
            (event_id,)).fetchone()
        if event is None:
            raise ValueError("execution transition event is missing")
        payload = json.loads(event["payload_json"])
        state = payload.get("execution_states", {}).get(order_id)
        if state is None:
            raise ValueError("transition event has no machine state for order")
        if state.get("state") != row["state"]:
            raise ValueError("execution projection and event state differ")
        return {"projection": dict(row), "machine_state": state}
