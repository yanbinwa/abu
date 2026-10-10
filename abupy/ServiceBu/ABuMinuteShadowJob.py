from __future__ import absolute_import

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..MarketBu.ABuMinuteBarStore import MinuteBarStore
from ..MarketBu.ABuRealtimeMarket import (
    AKShareRealtimeMarketData, FailoverMinuteMarketData,
    SinaMinuteMarketData, TencentMinuteMarketData, normalize_cn_symbol,
)
from .ABuDailyDataCenter import ProviderRateLimiter
from .ABuMarketSnapshotCatalog import SnapshotCatalog
from .ABuMinuteMarketHub import (
    MinuteCollector, MinuteSnapshotBuilder, WatchlistManager,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _symbols_from_legacy_state(path):
    path = Path(path)
    if not path.exists():
        return {"positions": set(), "pending_orders": set(), "candidates": set()}
    payload = json.loads(path.read_text(encoding="utf-8"))
    active = payload.get("active", {})
    positions = set(active.get("positions", {}))
    pending = {
        item.get("symbol") for item in active.get("orders", ())
        if item.get("symbol") and item.get("status", "APPROVED")
        in ("APPROVED", "WAITING")
    }
    candidates = set()
    intents = active.get("entry_intents", {})
    values = intents.values() if isinstance(intents, dict) else intents
    for item in values:
        if isinstance(item, dict) and item.get("symbol"):
            candidates.add(item["symbol"])
    return {
        "positions": positions,
        "pending_orders": pending,
        "candidates": candidates,
    }


class MinuteShadowSnapshotJob(object):
    """Collect one shared minute snapshot without touching account state."""

    def __init__(self, operational_store, content_root, config_path,
                 *, adapter=None, clock=None, rate_limiter=None):
        self.store = operational_store
        self.catalog = SnapshotCatalog(operational_store, content_root)
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self._adapter_injected = adapter is not None
        self._validate_config()
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        minute_store = MinuteBarStore(self.config["minute_store_root"])
        primary_limiter = rate_limiter or ProviderRateLimiter(
            float(self.config["provider_requests_per_second"])).wait
        if adapter is None:
            primary = TencentMinuteMarketData(
                raw_archive=minute_store.append_raw_response,
                retries=int(self.config["provider_retries"]),
                retry_wait=float(self.config["provider_retry_wait_seconds"]),
                timeout_seconds=float(self.config["request_timeout_seconds"]),
                rows=int(self.config.get("provider_max_rows", 120)),
                stale_after_seconds=float(
                    self.config.get("stale_after_seconds", 125.0)),
                request_rate_limiter=primary_limiter)
            fallback = SinaMinuteMarketData(
                raw_archive=minute_store.append_raw_response,
                retries=1, retry_wait=0.0,
                stale_after_seconds=float(
                    self.config.get("stale_after_seconds", 125.0)))
            fallback_limiter = ProviderRateLimiter(float(
                self.config.get("fallback_requests_per_second", 0.5))).wait
            adapter = FailoverMinuteMarketData(
                primary, fallback,
                failure_threshold=int(
                    self.config.get("circuit_breaker_failures", 3)),
                circuit_breaker_seconds=float(
                    self.config.get("circuit_breaker_seconds", 900.0)),
                max_fallback_per_cycle=int(
                    self.config.get("maximum_fallback_symbols_per_cycle", 5)),
                fallback_rate_limiter=fallback_limiter)
        self.watchlists = WatchlistManager(self.catalog)
        limiter = (primary_limiter if self._adapter_injected else (lambda: None))
        self.collector = MinuteCollector(
            adapter, minute_store,
            batch_size=int(self.config["batch_size"]),
            request_timeout_seconds=float(
                self.config["request_timeout_seconds"]),
            rate_limiter=limiter)
        self.builder = MinuteSnapshotBuilder(
            self.catalog, minute_store,
            provider_policy_version=self.config["provider_policy_version"])

    def _validate_config(self):
        required = (
            "first_eligible_session", "minute_store_root", "trading_calendar_path",
            "collection_windows", "sentinel_symbols", "benchmark_symbols",
            "legacy_state_paths", "batch_size", "request_timeout_seconds",
            "provider_retries", "provider_retry_wait_seconds",
            "provider_policy_version", "provider_requests_per_second",
        )
        missing = [name for name in required if name not in self.config]
        if missing:
            raise ValueError("minute shadow config missing: {}".format(
                ", ".join(missing)))
        if self.config.get("execution_mode") != "DATA_ONLY":
            raise ValueError("minute shadow job must remain DATA_ONLY")
        target = int(self.config.get("maximum_watchlist_symbols", 30))
        hard = int(self.config.get("hard_maximum_watchlist_symbols", 50))
        if not 1 <= target <= hard <= 50:
            raise ValueError("minute watchlist limits must satisfy 1 <= target <= hard <= 50")
        if (not self._adapter_injected and
                float(self.config["provider_requests_per_second"]) > 2.0):
            raise ValueError("Tencent minute source is capped at 2 requests/second")
        if (not self._adapter_injected and
                float(self.config.get("fallback_requests_per_second", 0.5)) > 0.5):
            raise ValueError("Sina fallback is capped at 0.5 requests/second")

    def _is_trading_session(self, session):
        payload = json.loads(Path(
            self.config["trading_calendar_path"]).read_text(encoding="utf-8"))
        return int(session) in {int(value) for value in payload["dates"]}

    def _in_collection_window(self, local_now):
        clock = local_now.strftime("%H:%M:%S")
        return any(window["start"] <= clock <= window["end"]
                   for window in self.config["collection_windows"])

    def _database_watchlist(self):
        connection = self.store.connection
        positions = {
            row["symbol"] for row in connection.execute(
                "SELECT symbol FROM positions WHERE quantity > 0")}
        pending = {
            row["symbol"] for row in connection.execute(
                "SELECT symbol FROM orders WHERE status IN ('APPROVED', 'WAITING')")}
        unfinished = {
            row["symbol"] for row in connection.execute(
                "SELECT o.symbol FROM order_execution_states s "
                "JOIN orders o ON o.account_id=s.account_id AND o.order_id=s.order_id "
                "WHERE o.status IN ('APPROVED', 'WAITING')")}
        return positions, pending, unfinished

    def _watchlist_inputs(self):
        positions, pending, unfinished = self._database_watchlist()
        candidates = set()
        for path in self.config["legacy_state_paths"]:
            legacy = _symbols_from_legacy_state(path)
            positions.update(legacy["positions"])
            pending.update(legacy["pending_orders"])
            candidates.update(legacy["candidates"])

        def normalize(values):
            return tuple(sorted(
                {normalize_cn_symbol(value) for value in values if value}))

        values = {
            "positions": normalize(positions),
            "pending_orders": normalize(pending),
            "candidates": normalize(candidates),
            "benchmarks": normalize(self.config["benchmark_symbols"]),
            "sentinels": normalize(self.config["sentinel_symbols"]),
            "unfinished_executions": normalize(unfinished),
        }
        critical = set(values["positions"]) | set(values["pending_orders"]) | set(
            values["unfinished_executions"])
        target = int(self.config.get("maximum_watchlist_symbols", 30))
        hard = int(self.config.get("hard_maximum_watchlist_symbols", 50))
        if len(critical) > hard:
            raise ValueError(
                "critical minute watchlist exceeds hard maximum: {} > {}".format(
                    len(critical), hard))
        admitted = set(critical)
        limit = min(hard, max(target, len(admitted)))
        for category in ("benchmarks", "sentinels", "candidates"):
            for symbol in values[category]:
                if len(admitted) >= limit:
                    break
                admitted.add(symbol)
        return {
            name: tuple(symbol for symbol in symbols if symbol in admitted)
            for name, symbols in values.items()
        }

    def __call__(self):
        local_now = self.clock()
        if local_now.tzinfo is None or local_now.utcoffset() is None:
            raise ValueError("minute shadow clock must be timezone-aware")
        local_now = local_now.astimezone(SHANGHAI)
        session = int(local_now.strftime("%Y%m%d"))
        if session < int(self.config["first_eligible_session"]):
            return {"status": "SKIPPED_BEFORE_ELIGIBLE_SESSION",
                    "output_snapshot_ids": []}
        if not self._is_trading_session(session):
            return {"status": "SKIPPED_NON_TRADING_SESSION",
                    "output_snapshot_ids": []}
        if not self._in_collection_window(local_now):
            return {"status": "SKIPPED_OUTSIDE_COLLECTION_WINDOW",
                    "output_snapshot_ids": []}

        started_at = local_now.isoformat()
        inputs = self._watchlist_inputs()
        watchlist, unused_created = self.watchlists.publish(
            session, started_at, **inputs)
        symbols = tuple(item["symbol"] for item in watchlist["symbols"])
        results = self.collector.collect(
            symbols, session, local_now.strftime("%Y-%m-%d %H:%M:%S"))
        completed = self.clock().astimezone(SHANGHAI)
        published = self.builder.publish(
            watchlist, results, decision_cutoff=completed.isoformat(),
            collection_started_at=started_at,
            collection_completed_at=completed.isoformat(), interval_minutes=1)
        return {
            "status": "COMMITTED",
            "snapshot_id": published["snapshot"]["snapshot_id"],
            "watchlist_id": watchlist["watchlist_id"],
            "metrics": published["metrics"],
            "output_snapshot_ids": [published["snapshot"]["snapshot_id"]],
        }
