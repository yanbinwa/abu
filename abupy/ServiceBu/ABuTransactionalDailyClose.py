from __future__ import absolute_import

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime

from ..AlphaBu.ABuTradeIntent import make_record_id
from .ABuContentStore import sha256_json


PRICE_MICROS = 1_000_000


class AccountReconciliationError(RuntimeError):
    """Daily close invariants failed; no account-day close was committed."""

    def __init__(self, findings):
        self.findings = tuple(findings)
        super(AccountReconciliationError, self).__init__(
            "account reconciliation failed: {}".format(
                ", ".join(item["code"] for item in self.findings)))


@dataclass(frozen=True)
class DailyClosingMark:
    symbol: str
    close_raw: float
    available_at: str
    source_version: str

    def __post_init__(self):
        value = float(self.close_raw)
        if not self.symbol or not math.isfinite(value) or value <= 0:
            raise ValueError("closing mark symbol and close_raw are required")
        if not self.available_at or not self.source_version:
            raise ValueError("closing mark lineage is required")
        parsed = datetime.fromisoformat(self.available_at)
        if parsed.tzinfo is None:
            raise ValueError("closing mark available_at must include an offset")


class TransactionalDailyCloser(object):
    """Complete account-day valuation only after deterministic reconciliation."""

    def __init__(self, accounts, coordinator):
        self.accounts = accounts
        self.coordinator = coordinator

    @staticmethod
    def marks_payload(marks):
        normalized = {}
        for symbol, mark in marks.items():
            if not isinstance(mark, DailyClosingMark) or mark.symbol != symbol:
                raise TypeError("marks must map symbol to DailyClosingMark")
            normalized[symbol] = asdict(mark)
        return {symbol: normalized[symbol] for symbol in sorted(normalized)}

    @classmethod
    def marks_sha256(cls, marks):
        return sha256_json(cls.marks_payload(marks))

    @staticmethod
    def _finding(code, **detail):
        return {"code": code, "detail": detail}

    def _reconcile(self, connection, context, trading_session, marks):
        findings = []
        session = int(trading_session)
        balance = connection.execute(
            "SELECT * FROM account_balances WHERE account_id=?",
            (context.account_id,)).fetchone()
        if balance is None:
            findings.append(self._finding("MISSING_ACCOUNT_BALANCE"))
            cash = reserved_cash = 0
        else:
            cash = int(balance["cash_micros"])
            reserved_cash = int(balance["reserved_cash_micros"])
            if cash < 0:
                findings.append(self._finding(
                    "NEGATIVE_CASH", cash_micros=cash))
            if reserved_cash > cash:
                findings.append(self._finding(
                    "RESERVED_CASH_EXCEEDS_CASH",
                    cash_micros=cash,
                    reserved_cash_micros=reserved_cash))

        active_cash = connection.execute(
            "SELECT COALESCE(sum(reserved_cash_micros), 0) FROM reservations "
            "WHERE account_id=? AND status='ACTIVE'",
            (context.account_id,)).fetchone()[0]
        if int(active_cash) != reserved_cash:
            findings.append(self._finding(
                "CASH_RESERVATION_MISMATCH", projection=reserved_cash,
                active_reservations=int(active_cash)))

        positions = connection.execute(
            "SELECT * FROM positions WHERE account_id=? ORDER BY symbol",
            (context.account_id,)).fetchall()
        lots = connection.execute(
            "SELECT * FROM position_lots WHERE account_id=? "
            "ORDER BY symbol, opened_at, lot_id",
            (context.account_id,)).fetchall()
        lots_by_symbol = {}
        for lot in lots:
            lots_by_symbol.setdefault(lot["symbol"], []).append(lot)
        position_symbols = {row["symbol"] for row in positions}
        orphan_symbols = sorted(set(lots_by_symbol) - position_symbols)
        for symbol in orphan_symbols:
            findings.append(self._finding(
                "LOTS_WITHOUT_POSITION", symbol=symbol,
                lot_count=len(lots_by_symbol[symbol])))

        market_value = 0
        position_cost_total = 0
        for position in positions:
            symbol = position["symbol"]
            quantity = int(position["quantity"])
            symbol_lots = lots_by_symbol.get(symbol, ())
            lot_quantity = sum(int(item["quantity"]) for item in symbol_lots)
            if quantity <= 0:
                findings.append(self._finding(
                    "NON_POSITIVE_POSITION", symbol=symbol, quantity=quantity))
            if lot_quantity != quantity:
                findings.append(self._finding(
                    "POSITION_LOT_QUANTITY_MISMATCH", symbol=symbol,
                    position_quantity=quantity, lot_quantity=lot_quantity))
            expected_sellable = sum(
                int(item["quantity"]) for item in symbol_lots
                if int(item["sellable_on_session"]) <= session)
            if expected_sellable != int(position["sellable_quantity"]):
                findings.append(self._finding(
                    "SELLABLE_QUANTITY_MISMATCH", symbol=symbol,
                    position_sellable=int(position["sellable_quantity"]),
                    lot_sellable=expected_sellable))
            lot_cost = sum(
                int(item["quantity"]) * int(item["cost_price_micros"])
                for item in symbol_lots)
            position_cost = quantity * int(position["average_cost_micros"])
            position_cost_total += position_cost
            if abs(lot_cost - position_cost) > max(1, quantity):
                findings.append(self._finding(
                    "POSITION_COST_MISMATCH", symbol=symbol,
                    position_cost_micros=position_cost,
                    lot_cost_micros=lot_cost))
            mark = marks.get(symbol)
            if mark is None:
                findings.append(self._finding(
                    "MISSING_CLOSING_MARK", symbol=symbol))
            else:
                market_value += quantity * int(round(
                    float(mark.close_raw) * PRICE_MICROS))

        open_trade_ids = {
            row["trade_id"] for row in connection.execute(
                "SELECT trade_id FROM logical_trades "
                "WHERE account_id=? AND status='OPEN'", (context.account_id,))}
        lot_trade_ids = {row["trade_id"] for row in lots}
        for trade_id in sorted(open_trade_ids - lot_trade_ids):
            findings.append(self._finding(
                "OPEN_TRADE_WITHOUT_LOT", trade_id=trade_id))
        closed_with_lots = connection.execute(
            "SELECT DISTINCT t.trade_id FROM logical_trades t "
            "JOIN position_lots l ON l.account_id=t.account_id "
            "AND l.trade_id=t.trade_id "
            "WHERE t.account_id=? AND t.status='CLOSED' ORDER BY t.trade_id",
            (context.account_id,)).fetchall()
        for row in closed_with_lots:
            findings.append(self._finding(
                "CLOSED_TRADE_WITH_LOT", trade_id=row["trade_id"]))

        active_orders = connection.execute(
            "SELECT * FROM orders WHERE account_id=? "
            "AND status IN ('APPROVED', 'WAITING') ORDER BY order_id",
            (context.account_id,)).fetchall()
        for order in active_orders:
            table = "reservations" if order["side"] == "buy" else "sell_reservations"
            count = connection.execute(
                "SELECT count(*) FROM {} WHERE account_id=? AND order_id=? "
                "AND status='ACTIVE'".format(table),
                (context.account_id, order["order_id"])).fetchone()[0]
            if int(count) != 1:
                findings.append(self._finding(
                    "ACTIVE_ORDER_RESERVATION_MISMATCH",
                    order_id=order["order_id"], side=order["side"],
                    active_reservation_count=int(count)))
        terminal_active = connection.execute(
            "SELECT o.order_id FROM orders o JOIN reservations r "
            "ON r.account_id=o.account_id AND r.order_id=o.order_id "
            "WHERE o.account_id=? AND o.status NOT IN ('APPROVED','WAITING') "
            "AND r.status='ACTIVE' UNION ALL "
            "SELECT o.order_id FROM orders o JOIN sell_reservations r "
            "ON r.account_id=o.account_id AND r.order_id=o.order_id "
            "WHERE o.account_id=? AND o.status NOT IN ('APPROVED','WAITING') "
            "AND r.status='ACTIVE'",
            (context.account_id, context.account_id)).fetchall()
        for row in terminal_active:
            findings.append(self._finding(
                "TERMINAL_ORDER_HAS_ACTIVE_RESERVATION",
                order_id=row["order_id"]))

        filled_orders = connection.execute(
            "SELECT o.order_id, count(f.fill_id) AS fill_count "
            "FROM orders o LEFT JOIN fills f ON f.account_id=o.account_id "
            "AND f.order_id=o.order_id WHERE o.account_id=? "
            "AND o.status='FILLED' GROUP BY o.order_id",
            (context.account_id,)).fetchall()
        for row in filled_orders:
            if int(row["fill_count"]) != 1:
                findings.append(self._finding(
                    "FILLED_ORDER_FILL_COUNT_MISMATCH",
                    order_id=row["order_id"],
                    fill_count=int(row["fill_count"])))
        impossible_fills = connection.execute(
            "SELECT DISTINCT o.order_id FROM orders o JOIN fills f "
            "ON f.account_id=o.account_id AND f.order_id=o.order_id "
            "WHERE o.account_id=? AND o.status!='FILLED' ORDER BY o.order_id",
            (context.account_id,)).fetchall()
        for row in impossible_fills:
            findings.append(self._finding(
                "NON_FILLED_ORDER_HAS_FILL", order_id=row["order_id"]))

        overdue = connection.execute(
            "SELECT receivable_id FROM account_receivables "
            "WHERE account_id=? AND status='PENDING' AND due_session<=? "
            "ORDER BY receivable_id",
            (context.account_id, session)).fetchall()
        for row in overdue:
            findings.append(self._finding(
                "OVERDUE_RECEIVABLE", receivable_id=row["receivable_id"]))

        active_sell = connection.execute(
            "SELECT o.trade_id, sum(sr.reserved_quantity) AS reserved_quantity "
            "FROM sell_reservations sr JOIN orders o "
            "ON o.account_id=sr.account_id AND o.order_id=sr.order_id "
            "WHERE sr.account_id=? AND sr.status='ACTIVE' "
            "GROUP BY o.trade_id",
            (context.account_id,)).fetchall()
        for row in active_sell:
            lot_quantity = connection.execute(
                "SELECT COALESCE(sum(quantity), 0) FROM position_lots "
                "WHERE account_id=? AND trade_id=?",
                (context.account_id, row["trade_id"])).fetchone()[0]
            if int(row["reserved_quantity"]) > int(lot_quantity):
                findings.append(self._finding(
                    "SELL_RESERVATION_EXCEEDS_TRADE",
                    trade_id=row["trade_id"],
                    reserved_quantity=int(row["reserved_quantity"]),
                    lot_quantity=int(lot_quantity)))

        return findings, {
            "cash_micros": cash,
            "reserved_cash_micros": reserved_cash,
            "available_cash_micros": cash - reserved_cash,
            "position_cost_micros": position_cost_total,
            "market_value_micros": market_value,
            "unrealized_pnl_micros": market_value - position_cost_total,
            "equity_micros": cash + market_value,
            "position_count": len(positions),
        }

    @staticmethod
    def _reconciliation_id(account_id, trading_session):
        return make_record_id(
            "reconciliation", account_id, int(trading_session))

    @staticmethod
    def _finding_id(account_id, trading_session):
        return make_record_id(
            "finding", account_id, int(trading_session),
            "ACCOUNT_DAILY_RECONCILIATION")

    def _record_failed(self, account_id, trading_session, snapshot_id,
                       event_id, findings, processed_at):
        reconciliation_id = self._reconciliation_id(
            account_id, trading_session)
        finding_id = self._finding_id(account_id, trading_session)
        encoded = json.dumps(
            list(findings), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False)
        with self.accounts.store.transaction() as connection:
            connection.execute(
                "INSERT INTO reconciliation_runs "
                "(reconciliation_id, account_id, trading_session, status, "
                "findings_json, started_at, completed_at) "
                "VALUES (?, ?, ?, 'FAILED', ?, ?, ?) "
                "ON CONFLICT(account_id, trading_session) DO UPDATE SET "
                "status='FAILED', findings_json=excluded.findings_json, "
                "completed_at=excluded.completed_at",
                (reconciliation_id, account_id, int(trading_session),
                 encoded, processed_at, processed_at))
            detail = json.dumps({
                "trading_session": int(trading_session),
                "findings": list(findings),
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute(
                "INSERT INTO audit_findings "
                "(finding_id, severity, category, account_id, snapshot_id, "
                "event_id, detail_json, created_at, resolved_at) "
                "VALUES (?, 'CRITICAL', 'ACCOUNT_DAILY_RECONCILIATION', "
                "?, ?, ?, ?, ?, NULL) "
                "ON CONFLICT(finding_id) DO UPDATE SET "
                "severity='CRITICAL', snapshot_id=excluded.snapshot_id, "
                "event_id=excluded.event_id, detail_json=excluded.detail_json, "
                "resolved_at=NULL",
                (finding_id, account_id, snapshot_id, event_id, detail,
                 processed_at))

    def complete_daily_close(
            self, account_id, input_event_id, expected_account_version,
            trading_session, expected_phase_version, marks, processed_at):
        marks_payload = self.marks_payload(marks)
        marks_hash = sha256_json(marks_payload)
        session = int(trading_session)

        def apply(connection, context, input_event):
            if input_event["event_type"] != "DailyClosingMarksPrepared":
                raise ValueError("daily close input event type mismatch")
            if int(input_event["trading_session"] or 0) != session:
                raise ValueError("daily close event trading session mismatch")
            payload = json.loads(input_event["payload_json"])
            if (payload.get("source_snapshot_id") != input_event["snapshot_id"] or
                    payload.get("closing_marks_sha256") != marks_hash):
                raise ValueError("closing marks are not pinned by input event")
            event_available_at = datetime.fromisoformat(
                input_event["available_at"])
            for mark in marks.values():
                if datetime.fromisoformat(mark.available_at) > event_available_at:
                    raise ValueError(
                        "closing mark was not available at input event cutoff")
            snapshot = connection.execute(
                "SELECT * FROM market_snapshots WHERE snapshot_id=?",
                (input_event["snapshot_id"],)).fetchone()
            if (snapshot is None or snapshot["snapshot_type"] != "DAILY" or
                    snapshot["status"] != "COMMITTED" or
                    int(snapshot["trading_session"]) != session):
                raise ValueError("committed DAILY snapshot is required")
            findings, close = self._reconcile(
                connection, context, session, marks)
            if findings:
                raise AccountReconciliationError(findings)
            connection.execute(
                "INSERT INTO account_daily_closes "
                "(account_id, trading_session, source_snapshot_id, cash_micros, "
                "reserved_cash_micros, available_cash_micros, "
                "position_cost_micros, market_value_micros, "
                "unrealized_pnl_micros, equity_micros, "
                "position_count, closing_marks_sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (account_id, session, input_event["snapshot_id"],
                 close["cash_micros"], close["reserved_cash_micros"],
                 close["available_cash_micros"],
                 close["position_cost_micros"],
                 close["market_value_micros"],
                 close["unrealized_pnl_micros"], close["equity_micros"],
                 close["position_count"], marks_hash, processed_at))
            reconciliation_id = self._reconciliation_id(account_id, session)
            connection.execute(
                "INSERT INTO reconciliation_runs "
                "(reconciliation_id, account_id, trading_session, status, "
                "findings_json, started_at, completed_at) "
                "VALUES (?, ?, ?, 'PASSED', '[]', ?, ?) "
                "ON CONFLICT(account_id, trading_session) DO UPDATE SET "
                "status='PASSED', findings_json='[]', "
                "completed_at=excluded.completed_at",
                (reconciliation_id, account_id, session,
                 processed_at, processed_at))
            connection.execute(
                "UPDATE audit_findings SET resolved_at=? WHERE finding_id=? "
                "AND resolved_at IS NULL",
                (processed_at, self._finding_id(account_id, session)))
            return {
                "source_snapshot_id": input_event["snapshot_id"],
                "closing_marks_sha256": marks_hash,
                "reconciliation_id": reconciliation_id,
                "reconciliation_status": "PASSED",
                **close,
            }

        snapshot_id = self.accounts.store.connection.execute(
            "SELECT snapshot_id FROM domain_events WHERE event_id=?",
            (input_event_id,)).fetchone()
        with self.accounts.command_queue.serial(account_id):
            try:
                return self.coordinator.complete_daily_close(
                    account_id, input_event_id, expected_account_version,
                    session, expected_phase_version, processed_at, apply)
            except AccountReconciliationError as error:
                self._record_failed(
                    account_id, session,
                    None if snapshot_id is None else snapshot_id["snapshot_id"],
                    input_event_id, error.findings, processed_at)
                raise
