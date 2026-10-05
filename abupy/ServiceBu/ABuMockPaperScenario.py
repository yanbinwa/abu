from __future__ import absolute_import

from dataclasses import asdict
from pathlib import Path

from ..AlphaBu.ABuIntradayExecution import IntradayExecutionConfig
from ..AlphaBu.ABuTradeIntent import ApprovedOrder
from ..MarketBu.ABuRealtimeMarket import MinuteBarEvent
from .ABuAccountProjection import AccountProjectionExporter
from .ABuAccountSession import AccountSessionStore
from .ABuAccountSessionCoordinator import AccountSessionCoordinator
from .ABuDomainEventStore import DomainEventStore, build_domain_event
from .ABuMarketSnapshotCatalog import SnapshotCatalog
from .ABuMinuteMarketHub import MinuteSnapshotBatch
from .ABuOperationalStore import OperationalStore
from .ABuPreopenSnapshot import PreopenSnapshotBuilder
from .ABuStrategyAccountStore import StrategyAccountStore
from .ABuTransactionalAccount import TransactionalAccountRepository
from .ABuTransactionalDailyClose import DailyClosingMark, TransactionalDailyCloser
from .ABuTransactionalIntradayBroker import TransactionalIntradayBroker


class MockPaperTradingScenario(object):
    """One-command deterministic account day through fill, close and outbox."""

    SESSION = 20261009
    NEXT_SESSION = 20261012
    NOW = "2026-10-09T09:20:00+08:00"

    def __init__(self, root):
        self.root = Path(root)

    @staticmethod
    def _bar(clock, opening):
        hour, minute, unused_second = (int(value) for value in clock.split(":"))
        start_minute = hour * 60 + minute - 1
        start = "{:02d}:{:02d}:00".format(
            start_minute // 60, start_minute % 60)
        prefix = "2026-10-09T"
        return MinuteBarEvent(
            symbol="sh600000", interval_minutes=1,
            source_timestamp=prefix + clock + "+08:00",
            bar_start=prefix + start + "+08:00",
            bar_end=prefix + clock + "+08:00",
            request_started_at=prefix + clock + "+08:00",
            received_at=prefix + clock + "+08:00",
            available_at=prefix + clock + "+08:00",
            open_raw=opening, high_raw=opening + .1,
            low_raw=opening - .1, close_raw=opening,
            volume_shares=10_000, amount_raw=opening * 10_000,
            source="mock")

    def run(self):
        self.root.mkdir(parents=True, exist_ok=True)
        store = OperationalStore(
            self.root / "operational.sqlite3", target_schema_version=3)
        registry = StrategyAccountStore(store)
        registry.register_strategy_instance(
            "mock-strategy", "mock_vcp", "1", self.NOW)
        registry.register_account(
            "mock-account", "Mock Account", "mock-strategy", self.NOW,
            initial_cash_micros=3_000_000_000)
        registry.activate_strategy(
            "mock-activation", "mock-account", {"mode": "mock"},
            self.SESSION, change_reason="mock scenario", approved_at=self.NOW)
        accounts = TransactionalAccountRepository(store)
        sessions = AccountSessionStore()
        coordinator = AccountSessionCoordinator(accounts, sessions)
        events = DomainEventStore(store)
        catalog = SnapshotCatalog(store, self.root / "content")
        accounts.execute(
            "mock-account", 0, self.NOW,
            lambda connection, context: sessions.create(
                connection, context, self.SESSION, self.NOW))
        order = ApprovedOrder(
            order_id="mock-order", intent_id="mock-intent",
            strategy_id="mock_vcp", strategy_version="1",
            symbol="sh600000", side="buy", quantity=100,
            created_asof=20261008, valid_session=self.SESSION,
            max_buy_price_raw=11.0, initial_stop_raw=8.0,
            position_effect="OPEN", account_id="mock-account",
            strategy_instance_id="mock-strategy",
            actor_activation_id="mock-activation")
        order_event = build_domain_event(
            "ApprovedOrderCommitted", "mock-approved", 1,
            {"approved_order": asdict(order)}, occurred_at=self.NOW,
            available_at=self.NOW, source_service="mock",
            trading_session=self.SESSION, account_id="mock-account")
        events.append(order_event)
        broker = TransactionalIntradayBroker(
            accounts, execution_policy_id="M1",
            intraday_config=IntradayExecutionConfig(slippage_bps=0))
        broker.register_approved_order(
            "mock-account", order_event["event_id"], 1, order,
            reservation_id="mock-reservation",
            reserved_cash_micros=1_106_000_000,
            reserved_risk_micros=100_000_000,
            risk_decision_id="mock-risk", processed_at=self.NOW,
            upper_limit_raw=11.0)
        preopen, preopen_event, unused_created = PreopenSnapshotBuilder(
            catalog).publish(
                trading_session=self.SESSION, decision_cutoff=self.NOW,
                created_at=self.NOW, security_master_version="mock",
                corporate_action_version="mock", security_status_version="mock",
                limit_reference_version="mock", receivables_cutoff=self.NOW,
                pending_order_snapshot_id="mock-orders")
        coordinator.accept_preopen_inputs(
            "mock-account", preopen_event["event_id"], 2, self.SESSION, 0,
            preopen["snapshot_id"], self.NOW)
        previous = None
        for sequence, event_type, action in (
                (1, "ApplyReceivables", "receivables"),
                (2, "ProcessOpenSells", "sells"),
                (3, "EnableIntradayBuys", "enable")):
            event = build_domain_event(
                event_type, "mock-phase", sequence, {"action": action},
                previous_event_id=previous, occurred_at=self.NOW,
                available_at=self.NOW, source_service="mock",
                trading_session=self.SESSION, account_id="mock-account")
            events.append(event)
            version = accounts.account("mock-account")["account_version"]
            if action == "receivables":
                coordinator.apply_receivables_and_corporate_actions(
                    "mock-account", event["event_id"], version,
                    self.SESSION, 1, self.NOW,
                    lambda *unused: {})
            elif action == "sells":
                coordinator.process_open_sells(
                    "mock-account", event["event_id"], version,
                    self.SESSION, 2, self.NOW, lambda *unused: {})
            else:
                coordinator.enable_intraday_buys(
                    "mock-account", event["event_id"], version,
                    self.SESSION, 3, self.NOW)
            previous = event["event_id"]
        previous_snapshot = previous_event = None
        for sequence, bars in ((1, (self._bar("09:35:00", 10.0),)),
                               (2, (self._bar("09:37:00", 10.2),))):
            snapshot_id = "mock-minute-{}".format(sequence)
            cutoff = bars[-1].available_at
            with store.transaction() as connection:
                connection.execute(
                    "INSERT INTO market_snapshots "
                    "(snapshot_id,snapshot_type,stream_id,sequence_no,"
                    "previous_snapshot_id,trading_session,decision_cutoff,status,"
                    "manifest_path,manifest_sha256,created_at,committed_at,quality_codes_json) "
                    "VALUES (?,'MINUTE','mock-minute',?,?,?,?, 'COMMITTED',?,?,?,?, '[]')",
                    (snapshot_id, sequence, previous_snapshot, self.SESSION,
                     cutoff, "/mock", "a" * 64, cutoff, cutoff))
            event = build_domain_event(
                "MinuteSnapshotCommitted", "mock-minute", sequence,
                {"snapshot_id": snapshot_id}, previous_event_id=previous_event,
                occurred_at=cutoff, available_at=cutoff, source_service="mock",
                trading_session=self.SESSION, snapshot_id=snapshot_id)
            events.append(event)
            batch = MinuteSnapshotBatch(
                snapshot_id, "mock-minute", sequence, previous_snapshot,
                cutoff, "mock-watchlist", 1, {}, {"sh600000": bars})
            broker.process_snapshot_batch(
                "mock-account", event["event_id"],
                accounts.account("mock-account")["account_version"],
                self.SESSION, batch, cutoff,
                next_trading_session=self.NEXT_SESSION)
            previous_snapshot, previous_event = snapshot_id, event["event_id"]
        marks = {"sh600000": DailyClosingMark(
            "sh600000", 10.5, "2026-10-09T15:00:00+08:00", "mock-daily")}
        daily_id = "mock-daily"
        with store.transaction() as connection:
            connection.execute(
                "INSERT INTO market_snapshots "
                "(snapshot_id,snapshot_type,stream_id,sequence_no,trading_session,"
                "decision_cutoff,status,manifest_path,manifest_sha256,created_at,"
                "committed_at,quality_codes_json) VALUES "
                "(?,'DAILY','mock-daily-stream',1,?,?, 'COMMITTED',?,?,?,?, '[]')",
                (daily_id, self.SESSION, "2026-10-09T15:10:00+08:00",
                 "/mock", "d" * 64, "2026-10-09T15:10:00+08:00",
                 "2026-10-09T15:10:00+08:00"))
        closer = TransactionalDailyCloser(accounts, coordinator)
        close_event = build_domain_event(
            "DailyClosingMarksPrepared", "mock-close", 1, {
                "source_snapshot_id": daily_id,
                "closing_marks_sha256": closer.marks_sha256(marks)},
            occurred_at="2026-10-09T15:10:00+08:00",
            available_at="2026-10-09T15:10:00+08:00",
            source_service="mock", trading_session=self.SESSION,
            snapshot_id=daily_id, account_id="mock-account")
        events.append(close_event)
        closer.complete_daily_close(
            "mock-account", close_event["event_id"],
            accounts.account("mock-account")["account_version"],
            self.SESSION, 4, marks, "2026-10-09T15:10:00+08:00")
        projection = AccountProjectionExporter(
            store, self.root / "exports").export(
                "mock-account", "2026-10-09T15:11:00+08:00")
        result = {
            "database_path": str(store.path),
            "account_version": accounts.account("mock-account")["account_version"],
            "fills": store.connection.execute(
                "SELECT count(*) FROM fills").fetchone()[0],
            "notifications": store.connection.execute(
                "SELECT count(*) FROM notification_outbox").fetchone()[0],
            "daily_close": store.connection.execute(
                "SELECT status FROM reconciliation_runs").fetchone()[0],
            "projection": projection,
        }
        store.close()
        return result
