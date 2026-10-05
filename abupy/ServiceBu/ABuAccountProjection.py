from __future__ import absolute_import

from .ABuContentStore import ContentAddressedStore, sha256_json


class AccountProjectionExporter(object):
    """Build immutable, version-consistent read models without account writes."""

    TABLES = (
        ("sessions", "account_sessions", "trading_session"),
        ("trades", "logical_trades", "trade_id"),
        ("lots", "position_lots", "lot_id"),
        ("positions", "positions", "symbol"),
        ("orders", "orders", "order_id"),
        ("execution_states", "order_execution_states", "order_id"),
        ("cash_reservations", "reservations", "reservation_id"),
        ("sell_reservations", "sell_reservations", "reservation_id"),
        ("fills", "fills", "fill_id"),
        ("risk_decisions", "risk_decisions", "risk_decision_id"),
        ("receivables", "account_receivables", "receivable_id"),
        ("daily_closes", "account_daily_closes", "trading_session"),
        ("reconciliations", "reconciliation_runs", "trading_session"),
        ("account_events", "account_events", "account_version"),
    )

    def __init__(self, operational_store, output_root=None):
        self.store = operational_store
        self.content = (
            None if output_root is None else ContentAddressedStore(output_root))

    @staticmethod
    def _rows(connection, table, account_id, order_by):
        return [dict(row) for row in connection.execute(
            "SELECT * FROM {} WHERE account_id=? ORDER BY {}".format(
                table, order_by), (account_id,))]

    def build(self, account_id, generated_at):
        with self.store.transaction(immediate=False) as connection:
            account = connection.execute(
                "SELECT * FROM accounts WHERE account_id=?",
                (account_id,)).fetchone()
            if account is None:
                raise KeyError("unknown account_id: {}".format(account_id))
            balance = connection.execute(
                "SELECT * FROM account_balances WHERE account_id=?",
                (account_id,)).fetchone()
            projection = {
                "schema_version": "account_projection_v1",
                "generated_at": generated_at,
                "account": dict(account),
                "balance": dict(balance),
            }
            for name, table, order_by in self.TABLES:
                projection[name] = self._rows(
                    connection, table, account_id, order_by)
        core = dict(projection)
        projection["projection_sha256"] = sha256_json(core)
        return projection

    def export(self, account_id, generated_at):
        if self.content is None:
            raise ValueError("output_root is required for export")
        projection = self.build(account_id, generated_at)
        path, file_sha256, created = self.content.write_json(
            "account-projections", projection)
        return {
            "account_id": account_id,
            "account_version": projection["account"]["account_version"],
            "projection_sha256": projection["projection_sha256"],
            "path": str(path), "file_sha256": file_sha256,
            "created": created,
        }
