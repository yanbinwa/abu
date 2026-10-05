from __future__ import absolute_import

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .ABuContentStore import sha256_json
from .ABuDomainEventStore import DomainEventStore, build_domain_event


@dataclass(frozen=True)
class AccountView:
    """Read-only account projection exposed to strategy plugins."""

    account_id: str
    account_name: str
    strategy_instance_id: str
    active_activation_id: str | None
    status: str
    account_version: int
    cash_micros: int
    reserved_cash_micros: int
    logical_trades: tuple[Mapping, ...]
    position_lots: tuple[Mapping, ...]
    positions: tuple[Mapping, ...]


def config_sha256(config):
    """Hash canonical JSON so key order never creates a new activation."""
    if not isinstance(config, dict):
        raise TypeError("strategy config must be a dictionary")
    return sha256_json(config)


class StrategyAccountStore(object):
    """Transactional identity, activation and trade-management registry.

    This class deliberately does not run strategies or execute orders.  It is
    the M4 ownership boundary used before the transactional broker is enabled.
    """

    def __init__(self, operational_store):
        self.store = operational_store
        self.events = DomainEventStore(operational_store)

    @staticmethod
    def _row(connection, table, key_name, key):
        return connection.execute(
            "SELECT * FROM {} WHERE {}=?".format(table, key_name), (key,)
        ).fetchone()

    def register_strategy_instance(self, strategy_instance_id, strategy_id,
                                   strategy_version, created_at):
        if not all((strategy_instance_id, strategy_id, strategy_version, created_at)):
            raise ValueError("strategy instance fields are required")
        with self.store.transaction() as connection:
            existing = self._row(
                connection, "strategy_instances", "strategy_instance_id",
                strategy_instance_id)
            expected = (strategy_id, strategy_version)
            if existing is not None:
                actual = (existing["strategy_id"], existing["strategy_version"])
                if actual != expected:
                    raise ValueError("strategy_instance_id is already bound")
                return dict(existing), False
            connection.execute(
                "INSERT INTO strategy_instances "
                "(strategy_instance_id, strategy_id, strategy_version, created_at) "
                "VALUES (?, ?, ?, ?)",
                (strategy_instance_id, strategy_id, strategy_version, created_at))
            return dict(self._row(
                connection, "strategy_instances", "strategy_instance_id",
                strategy_instance_id)), True

    def register_account(self, account_id, account_name, strategy_instance_id,
                         created_at, *, status="SHADOW", initial_cash_micros=0):
        if status not in ("SHADOW", "PAPER", "PAUSED", "BLOCKED", "RETIRED"):
            raise ValueError("invalid account status")
        if int(initial_cash_micros) < 0:
            raise ValueError("initial cash must be non-negative")
        with self.store.transaction() as connection:
            instance = self._row(
                connection, "strategy_instances", "strategy_instance_id",
                strategy_instance_id)
            if instance is None or instance["retired_at"] is not None:
                raise ValueError("active strategy instance is required")
            existing = self._row(connection, "accounts", "account_id", account_id)
            expected = (account_name, strategy_instance_id, status)
            if existing is not None:
                actual = (existing["account_name"],
                          existing["strategy_instance_id"], existing["status"])
                if actual != expected:
                    raise ValueError("account_id is already bound")
                balance = self._row(
                    connection, "account_balances", "account_id", account_id)
                if balance is None or balance["cash_micros"] != int(initial_cash_micros):
                    raise ValueError("account initial cash is immutable")
                return dict(existing), False
            connection.execute(
                "INSERT INTO accounts "
                "(account_id, account_name, account_version, strategy_instance_id, "
                "active_activation_id, status, created_at, updated_at) "
                "VALUES (?, ?, 0, ?, NULL, ?, ?, ?)",
                (account_id, account_name, strategy_instance_id, status,
                 created_at, created_at))
            connection.execute(
                "INSERT INTO account_balances "
                "(account_id, cash_micros, reserved_cash_micros, updated_at) "
                "VALUES (?, ?, 0, ?)",
                (account_id, int(initial_cash_micros), created_at))
            return dict(self._row(connection, "accounts", "account_id", account_id)), True

    def activate_strategy(self, activation_id, account_id, config,
                          effective_from_session, *, change_reason, approved_at,
                          activation_session=None):
        """Activate a config only at its declared trading-session boundary."""
        effective_from_session = int(effective_from_session)
        activation_session = int(
            effective_from_session if activation_session is None else activation_session)
        if activation_session != effective_from_session:
            raise ValueError("activation must occur at its effective session boundary")
        digest = config_sha256(config)
        with self.store.transaction() as connection:
            account = self._row(connection, "accounts", "account_id", account_id)
            if account is None:
                raise KeyError("unknown account_id: {}".format(account_id))
            if account["status"] in ("RETIRED", "BLOCKED"):
                raise ValueError("account cannot activate a strategy in current status")
            existing = self._row(
                connection, "strategy_activations", "activation_id", activation_id)
            if existing is not None:
                expected = (account_id, account["strategy_instance_id"], digest,
                            effective_from_session)
                actual = (existing["account_id"], existing["strategy_instance_id"],
                          existing["config_sha256"], existing["effective_from_session"])
                if actual != expected:
                    raise ValueError("activation_id is already bound")
                return dict(existing), False

            previous_id = account["active_activation_id"]
            if previous_id is not None:
                previous = self._row(
                    connection, "strategy_activations", "activation_id", previous_id)
                if previous is None or previous["account_id"] != account_id:
                    raise ValueError("account active activation is invalid")
                if effective_from_session <= previous["effective_from_session"]:
                    raise ValueError("activation sessions must increase monotonically")
                connection.execute(
                    "UPDATE strategy_activations SET effective_until_session=? "
                    "WHERE activation_id=? AND effective_until_session IS NULL",
                    (effective_from_session - 1, previous_id))

            connection.execute(
                "INSERT INTO strategy_activations "
                "(activation_id, strategy_instance_id, account_id, config_sha256, "
                "effective_from_session, effective_until_session, "
                "previous_activation_id, change_reason, approved_at) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                (activation_id, account["strategy_instance_id"], account_id,
                 digest, effective_from_session, previous_id,
                 change_reason, approved_at))
            connection.execute(
                "UPDATE accounts SET active_activation_id=?, updated_at=? "
                "WHERE account_id=?",
                (activation_id, approved_at, account_id))
            return dict(self._row(
                connection, "strategy_activations", "activation_id",
                activation_id)), True

    def activation_for_session(self, account_id, trading_session):
        row = self.store.connection.execute(
            "SELECT * FROM strategy_activations WHERE account_id=? "
            "AND effective_from_session<=? "
            "AND (effective_until_session IS NULL OR effective_until_session>=?) "
            "ORDER BY effective_from_session DESC LIMIT 1",
            (account_id, int(trading_session), int(trading_session))).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def _activation_for_account(connection, account_id, activation_id):
        row = connection.execute(
            "SELECT * FROM strategy_activations "
            "WHERE activation_id=? AND account_id=?",
            (activation_id, account_id)).fetchone()
        if row is None:
            raise ValueError("activation does not belong to account")
        return row

    def open_logical_trade(self, account_id, trade_id, symbol, activation_id,
                           *, entry_policy_id, entry_policy_version,
                           exit_policy_id, exit_policy_version,
                           risk_policy_id, risk_policy_version, opened_at):
        with self.store.transaction() as connection:
            self._activation_for_account(connection, account_id, activation_id)
            existing = connection.execute(
                "SELECT * FROM logical_trades WHERE account_id=? AND trade_id=?",
                (account_id, trade_id)).fetchone()
            expected = (
                symbol, activation_id, activation_id, entry_policy_id,
                entry_policy_version, exit_policy_id, exit_policy_version,
                risk_policy_id, risk_policy_version, opened_at, "OPEN")
            if existing is not None:
                actual = tuple(existing[name] for name in (
                    "symbol", "opened_under_activation_id",
                    "management_activation_id", "entry_policy_id",
                    "entry_policy_version", "exit_policy_id",
                    "exit_policy_version", "risk_policy_id",
                    "risk_policy_version", "opened_at", "status"))
                if actual != expected:
                    raise ValueError("logical trade identity collision")
                return dict(existing), False
            connection.execute(
                "INSERT INTO logical_trades "
                "(account_id, trade_id, symbol, opened_under_activation_id, "
                "management_activation_id, entry_policy_id, entry_policy_version, "
                "exit_policy_id, exit_policy_version, risk_policy_id, "
                "risk_policy_version, opened_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')",
                (account_id, trade_id, symbol, activation_id, activation_id,
                 entry_policy_id, entry_policy_version, exit_policy_id,
                 exit_policy_version, risk_policy_id, risk_policy_version,
                 opened_at))
            row = connection.execute(
                "SELECT * FROM logical_trades WHERE account_id=? AND trade_id=?",
                (account_id, trade_id)).fetchone()
            return dict(row), True

    def add_position_lot(self, lot_id, account_id, trade_id, symbol, quantity,
                         sellable_on_session, cost_price_micros, opened_at):
        if int(quantity) <= 0 or int(cost_price_micros) <= 0:
            raise ValueError("lot quantity and price must be positive")
        with self.store.transaction() as connection:
            trade = connection.execute(
                "SELECT * FROM logical_trades WHERE account_id=? AND trade_id=?",
                (account_id, trade_id)).fetchone()
            if trade is None or trade["status"] != "OPEN" or trade["symbol"] != symbol:
                raise ValueError("open logical trade for symbol is required")
            existing = self._row(connection, "position_lots", "lot_id", lot_id)
            expected = (account_id, trade_id, symbol, int(quantity),
                        int(sellable_on_session), int(cost_price_micros), opened_at)
            if existing is not None:
                actual = tuple(existing[name] for name in (
                    "account_id", "trade_id", "symbol", "quantity",
                    "sellable_on_session", "cost_price_micros", "opened_at"))
                if actual != expected:
                    raise ValueError("lot_id is already bound")
                return dict(existing), False
            connection.execute(
                "INSERT INTO position_lots "
                "(lot_id, account_id, trade_id, symbol, quantity, "
                "sellable_on_session, cost_price_micros, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (lot_id,) + expected)
            return dict(self._row(connection, "position_lots", "lot_id", lot_id)), True

    def takeover_trade_management(self, account_id, trade_id, to_activation_id,
                                  *, exit_policy_id, exit_policy_version,
                                  effective_from, reason, occurred_at,
                                  trading_session):
        """Atomically append takeover evidence and change one trade owner."""
        with self.store.transaction() as connection:
            trade = connection.execute(
                "SELECT * FROM logical_trades WHERE account_id=? AND trade_id=?",
                (account_id, trade_id)).fetchone()
            if trade is None or trade["status"] != "OPEN":
                raise ValueError("open logical trade is required")
            target = self._activation_for_account(
                connection, account_id, to_activation_id)
            existing = connection.execute(
                "SELECT * FROM trade_management_assignments "
                "WHERE account_id=? AND trade_id=? AND effective_from=?",
                (account_id, trade_id, effective_from)).fetchone()
            if existing is not None:
                expected = (to_activation_id, exit_policy_id,
                            exit_policy_version, reason)
                actual = tuple(existing[name] for name in (
                    "to_activation_id", "exit_policy_id",
                    "exit_policy_version", "reason"))
                if actual != expected:
                    raise ValueError("management takeover boundary collision")
                return dict(existing), False
            if target["activation_id"] == trade["management_activation_id"]:
                raise ValueError("takeover target already manages the trade")

            stream_id = "account:{}".format(account_id)
            head = connection.execute(
                "SELECT event_id, sequence_no FROM domain_events "
                "WHERE stream_id=? ORDER BY sequence_no DESC LIMIT 1",
                (stream_id,)).fetchone()
            sequence = 1 if head is None else int(head["sequence_no"]) + 1
            previous_event_id = None if head is None else head["event_id"]
            payload = {
                "account_id": account_id,
                "trade_id": trade_id,
                "from_activation_id": trade["management_activation_id"],
                "to_activation_id": to_activation_id,
                "exit_policy_id": exit_policy_id,
                "exit_policy_version": exit_policy_version,
                "effective_from": effective_from,
                "reason": reason,
            }
            event = build_domain_event(
                "PositionManagementTakenOver", stream_id, sequence, payload,
                previous_event_id=previous_event_id, occurred_at=occurred_at,
                available_at=occurred_at, source_service="strategy-account-registry",
                trading_session=int(trading_session), account_id=account_id,
                actor_strategy_instance_id=target["strategy_instance_id"],
                actor_activation_id=to_activation_id,
                affected_trade_ids=(trade_id,),
                affected_management_activation_ids=(
                    trade["management_activation_id"], to_activation_id))
            self.events.append(event, connection=connection)
            assignment_id = "assignment-{}".format(sha256_json(payload)[:24])
            connection.execute(
                "INSERT INTO trade_management_assignments "
                "(assignment_id, account_id, trade_id, from_activation_id, "
                "to_activation_id, exit_policy_id, exit_policy_version, "
                "effective_from, reason, takeover_event_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (assignment_id, account_id, trade_id,
                 trade["management_activation_id"], to_activation_id,
                 exit_policy_id, exit_policy_version, effective_from, reason,
                 event["event_id"]))
            connection.execute(
                "UPDATE logical_trades SET management_activation_id=?, "
                "exit_policy_id=?, exit_policy_version=? "
                "WHERE account_id=? AND trade_id=?",
                (to_activation_id, exit_policy_id, exit_policy_version,
                 account_id, trade_id))
            row = connection.execute(
                "SELECT * FROM trade_management_assignments "
                "WHERE assignment_id=?", (assignment_id,)).fetchone()
            return dict(row), True

    def account_view(self, account_id):
        connection = self.store.connection
        account = self._row(connection, "accounts", "account_id", account_id)
        if account is None:
            raise KeyError("unknown account_id: {}".format(account_id))
        balance = self._row(connection, "account_balances", "account_id", account_id)

        def rows(sql):
            return tuple(MappingProxyType(dict(row))
                         for row in connection.execute(sql, (account_id,)))

        return AccountView(
            account_id=account_id, account_name=account["account_name"],
            strategy_instance_id=account["strategy_instance_id"],
            active_activation_id=account["active_activation_id"],
            status=account["status"], account_version=account["account_version"],
            cash_micros=balance["cash_micros"],
            reserved_cash_micros=balance["reserved_cash_micros"],
            logical_trades=rows(
                "SELECT * FROM logical_trades WHERE account_id=? ORDER BY trade_id"),
            position_lots=rows(
                "SELECT * FROM position_lots WHERE account_id=? ORDER BY lot_id"),
            positions=rows(
                "SELECT * FROM positions WHERE account_id=? ORDER BY symbol"),
        )
