from __future__ import absolute_import

import json
from dataclasses import dataclass

from ..AlphaBu.ABuTradeIntent import ApprovedOrder
from .ABuAccountSession import AccountSessionStore
from .ABuAccountSessionCoordinator import AccountSessionCoordinator
from .ABuDomainEventStore import DomainEventStore, build_domain_event
from .ABuTransactionalAccount import TransactionalAccountRepository
from .ABuTransactionalDailyBroker import (
    DailyOpenQuote, TransactionalDailyBroker,
)
from .ABuTransactionalDailyClose import (
    DailyClosingMark, TransactionalDailyCloser,
)
from .ABuTransactionalIntradayBroker import TransactionalIntradayBroker


@dataclass(frozen=True)
class AccountOpenRequest:
    account_id: str
    trading_session: int
    preopen_snapshot_id: str
    preopen_event_id: str
    opening_quotes: dict
    processed_at: str


@dataclass(frozen=True)
class AccountCloseRequest:
    account_id: str
    trading_session: int
    daily_snapshot_id: str
    closing_marks: dict
    processed_at: str


class TransactionalDailyWorkflow(object):
    """Drive one account day using only committed, pinned input facts.

    Strategy and risk code publish ``ApprovedOrderCommitted`` events before
    preopen.  This workflow never generates or changes a strategy decision; it
    registers those frozen decisions and advances the account phase barrier.
    """

    def __init__(self, operational_store, *, execution_policy_id="M1",
                 accounts=None, sessions=None, coordinator=None,
                 intraday_broker=None, daily_broker=None, daily_closer=None):
        self.store = operational_store
        self.accounts = accounts or TransactionalAccountRepository(
            operational_store)
        self.sessions = sessions or AccountSessionStore()
        self.coordinator = coordinator or AccountSessionCoordinator(
            self.accounts, self.sessions)
        self.intraday = intraday_broker or TransactionalIntradayBroker(
            self.accounts, execution_policy_id=execution_policy_id)
        self.daily = daily_broker or TransactionalDailyBroker(
            self.accounts, self.coordinator)
        self.closer = daily_closer or TransactionalDailyCloser(
            self.accounts, self.coordinator)
        self.events = DomainEventStore(operational_store)

    def _session(self, account_id, trading_session):
        return self.store.connection.execute(
            "SELECT * FROM account_sessions WHERE account_id=? "
            "AND trading_session=?", (account_id, int(trading_session))).fetchone()

    def _ensure_session(self, account_id, trading_session, processed_at):
        row = self._session(account_id, trading_session)
        if row is not None:
            return dict(row), False
        account = self.accounts.account(account_id)
        result = self.accounts.execute(
            account_id, account["account_version"], processed_at,
            lambda connection, context: self.sessions.create(
                connection, context, trading_session, processed_at))
        row, created = result.value
        return dict(row), bool(created)

    @staticmethod
    def _order(payload):
        try:
            return ApprovedOrder(**payload["approved_order"])
        except (KeyError, TypeError) as error:
            raise ValueError("approved order event payload is incomplete") from error

    def _register_pending_orders(self, account_id, trading_session,
                                 processed_at):
        rows = self.store.connection.execute(
            "SELECT e.* FROM domain_events e WHERE e.account_id=? "
            "AND e.trading_session=? AND e.event_type='ApprovedOrderCommitted' "
            "AND NOT EXISTS (SELECT 1 FROM processed_events p "
            "WHERE p.account_id=? AND p.event_id=e.event_id) "
            "ORDER BY e.available_at,e.event_id",
            (account_id, int(trading_session), account_id)).fetchall()
        registered = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            order = self._order(payload)
            if (order.account_id and order.account_id != account_id) or \
                    int(order.valid_session) != int(trading_session):
                raise ValueError("approved order namespace or session mismatch")
            version = self.accounts.account(account_id)["account_version"]
            if order.side == "buy":
                required = (
                    "reservation_id", "reserved_cash_micros",
                    "reserved_risk_micros", "risk_decision_id",
                )
                if (any(name not in payload for name in required) or
                        not payload.get("reservation_id") or
                        not payload.get("risk_decision_id") or
                        int(payload.get("reserved_cash_micros", 0)) <= 0 or
                        int(payload.get("reserved_risk_micros", -1)) < 0):
                    raise ValueError("buy order event lacks frozen risk reservation")
                result = self.intraday.register_approved_order(
                    account_id, row["event_id"], version, order,
                    reservation_id=payload["reservation_id"],
                    reserved_cash_micros=int(payload["reserved_cash_micros"]),
                    reserved_risk_micros=int(payload["reserved_risk_micros"]),
                    risk_decision_id=payload["risk_decision_id"],
                    processed_at=processed_at,
                    upper_limit_raw=payload.get("upper_limit_raw"))
            else:
                if not payload.get("reservation_id"):
                    raise ValueError("sell order event lacks frozen reservation")
                result = self.daily.register_sell_order(
                    account_id, row["event_id"], version, order,
                    reservation_id=payload["reservation_id"],
                    processed_at=processed_at)
            registered.append({"order_id": order.order_id,
                               "side": order.side,
                               "account_version": result.account_version})
        return registered

    def _phase_event(self, account_id, trading_session, sequence_no,
                     event_type, processed_at):
        stream = "account-phase-input:{}:{}".format(
            account_id, int(trading_session))
        previous = self.store.connection.execute(
            "SELECT event_id FROM domain_events WHERE stream_id=? "
            "AND sequence_no=?", (stream, int(sequence_no) - 1)).fetchone()
        event = build_domain_event(
            event_type, stream, sequence_no,
            {"phase_command": event_type},
            previous_event_id=(None if previous is None else previous["event_id"]),
            occurred_at=processed_at, available_at=processed_at,
            source_service="transactional-daily-workflow",
            trading_session=int(trading_session), account_id=account_id,
            correlation_id="account-day:{}:{}".format(
                account_id, int(trading_session)))
        row, unused_created = self.events.append(event)
        return row

    def open_day(self, request):
        if not isinstance(request, AccountOpenRequest):
            raise TypeError("request must be AccountOpenRequest")
        session = int(request.trading_session)
        for symbol, quote in request.opening_quotes.items():
            if not isinstance(quote, DailyOpenQuote) or quote.symbol != symbol:
                raise TypeError("opening_quotes must contain DailyOpenQuote values")
        row, created = self._ensure_session(
            request.account_id, session, request.processed_at)
        registered = []
        if row["phase"] == "CREATED":
            registered = self._register_pending_orders(
                request.account_id, session, request.processed_at)
            row = self._session(request.account_id, session)
            event = self.store.connection.execute(
                "SELECT * FROM domain_events WHERE event_id=?",
                (request.preopen_event_id,)).fetchone()
            if (event is None or event["event_type"] != "PreopenSnapshotCommitted" or
                    event["snapshot_id"] != request.preopen_snapshot_id or
                    int(event["trading_session"] or 0) != session):
                raise ValueError("preopen request is not pinned by committed event")
            self.coordinator.accept_preopen_inputs(
                request.account_id, request.preopen_event_id,
                self.accounts.account(request.account_id)["account_version"],
                session, int(row["phase_version"]),
                request.preopen_snapshot_id, request.processed_at)

        row = self._session(request.account_id, session)
        if row["phase"] == "PREOPEN_INPUTS_READY":
            event = self._phase_event(
                request.account_id, session, 1,
                "ApplyReceivablesAndCorporateActions", request.processed_at)
            self.daily.apply_due_receivables(
                request.account_id, event["event_id"],
                self.accounts.account(request.account_id)["account_version"],
                session, int(row["phase_version"]), request.processed_at)

        row = self._session(request.account_id, session)
        if row["phase"] == "RECEIVABLES_APPLIED":
            event = self._phase_event(
                request.account_id, session, 2,
                "ProcessOpenSells", request.processed_at)
            self.daily.process_open_sells(
                request.account_id, event["event_id"],
                self.accounts.account(request.account_id)["account_version"],
                session, int(row["phase_version"]), request.opening_quotes,
                request.processed_at)

        row = self._session(request.account_id, session)
        if row["phase"] == "OPEN_SELLS_PROCESSED":
            event = self._phase_event(
                request.account_id, session, 3,
                "EnableIntradayBuys", request.processed_at)
            self.coordinator.enable_intraday_buys(
                request.account_id, event["event_id"],
                self.accounts.account(request.account_id)["account_version"],
                session, int(row["phase_version"]), request.processed_at)

        row = dict(self._session(request.account_id, session))
        if row["phase"] not in ("INTRADAY_BUYS_ENABLED",
                                "DAILY_CLOSE_COMPLETED"):
            raise ValueError("account day did not reach intraday barrier")
        return {"status": row["phase"], "account_id": request.account_id,
                "trading_session": session, "session_created": created,
                "registered_orders": registered,
                "account_version": self.accounts.account(
                    request.account_id)["account_version"],
                "output_snapshot_ids": [request.preopen_snapshot_id]}

    def close_day(self, request):
        if not isinstance(request, AccountCloseRequest):
            raise TypeError("request must be AccountCloseRequest")
        session = int(request.trading_session)
        for symbol, mark in request.closing_marks.items():
            if not isinstance(mark, DailyClosingMark) or mark.symbol != symbol:
                raise TypeError("closing_marks must contain DailyClosingMark values")
        row = self._session(request.account_id, session)
        if row is None:
            raise KeyError("account session has not been created")
        if row["phase"] == "DAILY_CLOSE_COMPLETED":
            return {"status": "DAILY_CLOSE_COMPLETED",
                    "account_id": request.account_id,
                    "trading_session": session,
                    "account_version": self.accounts.account(
                        request.account_id)["account_version"],
                    "output_snapshot_ids": [request.daily_snapshot_id]}
        if row["phase"] != "INTRADAY_BUYS_ENABLED":
            raise ValueError("daily close requires intraday phase completion")
        snapshot = self.store.connection.execute(
            "SELECT * FROM market_snapshots WHERE snapshot_id=?",
            (request.daily_snapshot_id,)).fetchone()
        if (snapshot is None or snapshot["snapshot_type"] != "DAILY" or
                snapshot["status"] != "COMMITTED" or
                int(snapshot["trading_session"]) != session):
            raise ValueError("committed daily snapshot is required")
        stream = "account-close-input:{}:{}".format(
            request.account_id, session)
        event = build_domain_event(
            "DailyClosingMarksPrepared", stream, 1, {
                "source_snapshot_id": request.daily_snapshot_id,
                "closing_marks_sha256": self.closer.marks_sha256(
                    request.closing_marks),
            }, occurred_at=request.processed_at,
            available_at=request.processed_at,
            source_service="transactional-daily-workflow",
            trading_session=session, snapshot_id=request.daily_snapshot_id,
            account_id=request.account_id,
            correlation_id="account-day:{}:{}".format(
                request.account_id, session))
        event_row, unused_created = self.events.append(event)
        result = self.closer.complete_daily_close(
            request.account_id, event_row["event_id"],
            self.accounts.account(request.account_id)["account_version"],
            session, int(row["phase_version"]), request.closing_marks,
            request.processed_at)
        return {"status": "DAILY_CLOSE_COMPLETED",
                "account_id": request.account_id,
                "trading_session": session,
                "account_version": result.account_version,
                "reconciliation_status": result.value[
                    "domain_value"]["reconciliation_status"],
                "output_snapshot_ids": [request.daily_snapshot_id]}


class TransactionalAccountOpenJob(object):

    def __init__(self, workflow, request_provider):
        self.workflow = workflow
        self.request_provider = request_provider

    def __call__(self):
        results = [self.workflow.open_day(item)
                   for item in self.request_provider()]
        return {"status": "ACCOUNT_OPEN_BATCH_COMPLETED", "results": results,
                "output_snapshot_ids": sorted({snapshot_id for result in results
                                               for snapshot_id in result[
                                                   "output_snapshot_ids"]})}


class TransactionalAccountCloseJob(object):

    def __init__(self, workflow, request_provider):
        self.workflow = workflow
        self.request_provider = request_provider

    def __call__(self):
        results = [self.workflow.close_day(item)
                   for item in self.request_provider()]
        return {"status": "ACCOUNT_CLOSE_BATCH_COMPLETED", "results": results,
                "output_snapshot_ids": sorted({snapshot_id for result in results
                                               for snapshot_id in result[
                                                   "output_snapshot_ids"]})}
