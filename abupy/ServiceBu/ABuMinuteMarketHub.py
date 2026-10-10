from __future__ import absolute_import

import json
import time
import uuid
from dataclasses import dataclass

import pandas as pd

from ..MarketBu.ABuMinuteBarStore import (
    MinuteBarStore, minute_events_from_frame,
)
from ..MarketBu.ABuRealtimeMarket import RealtimeMarketDataError
from .ABuContentStore import sha256_json
from .ABuDomainEventStore import DomainEventStore, build_domain_event
from .ABuEventDispatcher import EventDispatcher


TERMINAL_STATUSES = frozenset((
    "AVAILABLE", "NO_DATA", "STALE", "PROVIDER_ERROR", "SCHEMA_ERROR",
    "NOT_TRADING", "SUSPENSION_UNKNOWN",
))
REASON_ORDER = ("POSITION", "PENDING_ORDER", "CANDIDATE", "BENCHMARK", "SENTINEL")
BAR_SELECTION_POLICY_VERSION = "available_at_cutoff_latest_revision_v1"


class IncompleteMinuteCollection(ValueError):
    pass


@dataclass(frozen=True)
class MinuteSnapshotBatch:
    snapshot_id: str
    stream_id: str
    sequence_no: int
    previous_snapshot_id: str | None
    decision_cutoff: str
    watchlist_id: str
    watchlist_version: int
    manifest: dict
    events_by_symbol: dict


def _reasoned_symbols(positions=(), pending_orders=(), candidates=(),
                      benchmarks=(), sentinels=(), unfinished_executions=()):
    reasons = {}
    for reason, values in (
            ("POSITION", positions),
            ("PENDING_ORDER", tuple(pending_orders) + tuple(unfinished_executions)),
            ("CANDIDATE", candidates),
            ("BENCHMARK", benchmarks),
            ("SENTINEL", sentinels)):
        for symbol in values:
            if symbol:
                reasons.setdefault(str(symbol), set()).add(reason)
    return [{
        "symbol": symbol,
        "reasons": [reason for reason in REASON_ORDER if reason in reasons[symbol]],
    } for symbol in sorted(reasons)]


class WatchlistManager(object):

    def __init__(self, snapshot_catalog):
        self.catalog = snapshot_catalog

    def publish(self, trading_session, created_at, *, positions=(),
                pending_orders=(), candidates=(), benchmarks=(), sentinels=(),
                unfinished_executions=(), reason_codes=("WATCHLIST_UPDATED",)):
        trading_session = int(trading_session)
        symbols = _reasoned_symbols(
            positions, pending_orders, candidates, benchmarks, sentinels,
            unfinished_executions)
        if not symbols:
            raise ValueError("minute watchlist cannot be empty")
        committed = self.catalog.list_committed(
            "WATCHLIST", trading_session=trading_session)
        latest = None if not committed else self.catalog.manifest(
            committed[-1]["snapshot_id"])
        if latest is not None and latest["symbols"] == symbols:
            return latest, False
        version = 1 if latest is None else int(latest["watchlist_version"]) + 1
        identity = {"trading_session": trading_session, "symbols": symbols}
        watchlist_id = "watchlist-{}-{}".format(
            trading_session, sha256_json(identity)[:20])
        stream_id = "market-watchlist:{}".format(trading_session)
        manifest = {
            "schema_version": "watchlist_v1",
            "stream_id": stream_id,
            "sequence_no": version,
            "previous_snapshot_id": (
                None if latest is None else latest["snapshot_id"]),
            "trading_session": trading_session,
            "decision_cutoff": created_at,
            "watchlist_id": watchlist_id,
            "watchlist_version": version,
            "previous_watchlist_id": (
                None if latest is None else latest["watchlist_id"]),
            "symbols": symbols,
            "created_at": created_at,
            "reason_codes": sorted(set(reason_codes)),
        }
        row, unused_event, created = self.catalog.publish(
            "WATCHLIST", manifest, source_service="minute-market-hub",
            event_type="WatchlistCommitted")
        return self.catalog.manifest(row["snapshot_id"]), created


class MinuteCollector(object):
    """Bounded shared collector; adapter remains responsible for HTTP timeout."""

    def __init__(self, adapter, minute_store, *, batch_size=20,
                 request_timeout_seconds=15.0, rate_limiter=None):
        if int(batch_size) <= 0 or float(request_timeout_seconds) <= 0:
            raise ValueError("collector batch size and timeout must be positive")
        self.adapter = adapter
        self.store = (minute_store if isinstance(minute_store, MinuteBarStore)
                      else MinuteBarStore(minute_store))
        if hasattr(self.adapter, "raw_archive") and self.adapter.raw_archive is None:
            self.adapter.raw_archive = self.store.append_raw_response
        self.batch_size = int(batch_size)
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.rate_limiter = rate_limiter or (lambda: None)
        self.provider_by_symbol = {}

    def collect(self, symbols, trading_session, collection_end):
        symbols = tuple(sorted(set(symbols)))
        if callable(getattr(self.adapter, "begin_cycle", None)):
            self.adapter.begin_cycle()
        date = str(int(trading_session))
        day = "{}-{}-{}".format(date[:4], date[4:6], date[6:])
        results = {}
        for offset in range(0, len(symbols), self.batch_size):
            for symbol in symbols[offset:offset + self.batch_size]:
                self.rate_limiter()
                started = time.monotonic()
                try:
                    frame = self.adapter.minute_bars(
                        symbol, period="1", start=day + " 09:30:00",
                        end=collection_end, adjust="")
                    elapsed = time.monotonic() - started
                    if elapsed > self.request_timeout_seconds:
                        results[symbol] = {
                            "terminal_status": "PROVIDER_ERROR",
                            "error": "PROVIDER_TIMEOUT", "elapsed_seconds": elapsed}
                        continue
                    events = minute_events_from_frame(frame)
                    if not events:
                        results[symbol] = {"terminal_status": "NO_DATA"}
                        continue
                    if any(item.symbol != symbol or item.interval_minutes != 1
                           for item in events):
                        raise ValueError("normalized provider semantics mismatch")
                    sources = {item.source for item in events}
                    if len(sources) != 1:
                        raise ValueError("one symbol batch contains multiple providers")
                    source = next(iter(sources))
                    previous = self.provider_by_symbol.get(symbol)
                    quality_codes = []
                    health = (self.adapter.health(symbol)
                              if callable(getattr(self.adapter, "health", None))
                              else None)
                    health = health.to_dict() if health is not None else {}
                    stale = health.get("data_fresh") is False
                    quality_codes.extend(health.get("reason_codes", ()))
                    if stale:
                        quality_codes.append("STALE_DATA")
                    if health.get("last_warning"):
                        quality_codes.append("PROVIDER_FALLBACK")
                    if previous is not None and previous != source:
                        quality_codes.append("PROVIDER_SWITCHED")
                    appended = self.store.append(events)
                    self.provider_by_symbol[symbol] = source
                    results[symbol] = {
                        "terminal_status": "STALE" if stale else "AVAILABLE",
                        "source": source,
                        "partition_manifest_sha256": appended[-1]["manifest_sha256"],
                        "appended": sum(item["appended"] for item in appended),
                        "elapsed_seconds": elapsed,
                        "quality_codes": sorted(set(quality_codes)),
                    }
                except RealtimeMarketDataError as error:
                    results[symbol] = {
                        "terminal_status": "PROVIDER_ERROR", "error": str(error)}
                except (KeyError, TypeError, ValueError, AttributeError) as error:
                    results[symbol] = {
                        "terminal_status": "SCHEMA_ERROR", "error": str(error)}
        return results


class MinuteSnapshotBuilder(object):

    def __init__(self, snapshot_catalog, minute_store,
                 provider_policy_version="provider-v1"):
        self.catalog = snapshot_catalog
        self.store = (minute_store if isinstance(minute_store, MinuteBarStore)
                      else MinuteBarStore(minute_store))
        self.provider_policy_version = provider_policy_version
        self.events = DomainEventStore(snapshot_catalog.store)

    def _revision_audit(self, previous, current, created_at, trading_session,
                        interval_minutes):
        if previous is None:
            return (), ()
        old = {
            (symbol, item["business_bar_key"]): item
            for symbol, record in previous["symbols"].items()
            for item in record.get("selected_bars", ())}
        changed = []
        for symbol, record in current.items():
            for item in record.get("selected_bars", ()):
                prior = old.get((symbol, item["business_bar_key"]))
                if prior is not None and prior["event_id"] != item["event_id"]:
                    changed.append({
                        "symbol": symbol,
                        "business_bar_key": item["business_bar_key"],
                        "previous_event_id": prior["event_id"],
                        "selected_event_id": item["event_id"],
                        "previous_revision": prior["revision"],
                        "selected_revision": item["revision"],
                        "action": "APPEND_AUDIT_CORRECTION_ONLY",
                    })
        if not changed:
            return (), ()
        stream_id = "market-minute-revision:{}:{}".format(
            trading_session, interval_minutes)
        head = self.catalog.store.connection.execute(
            "SELECT event_id, sequence_no FROM domain_events WHERE stream_id=? "
            "ORDER BY sequence_no DESC LIMIT 1", (stream_id,)).fetchone()
        sequence = 1 if head is None else int(head["sequence_no"]) + 1
        event = build_domain_event(
            "LateMinuteRevisionObserved", stream_id, sequence,
            {"revisions": changed, "does_not_replay_account_state": True},
            previous_event_id=None if head is None else head["event_id"],
            occurred_at=created_at, available_at=created_at,
            source_service="minute-market-hub",
            trading_session=int(trading_session))
        finding = {
            "finding_id": "late-minute-{}".format(uuid.uuid4().hex),
            "severity": "WARNING", "category": "LATE_MINUTE_REVISION",
            "event_id": event["event_id"], "detail": {"revisions": changed},
            "created_at": created_at,
        }
        return (event,), (finding,)

    def publish(self, watchlist_manifest, collection_results, *,
                decision_cutoff, collection_started_at,
                collection_completed_at, interval_minutes=1):
        trading_session = int(watchlist_manifest["trading_session"])
        expected = {item["symbol"] for item in watchlist_manifest["symbols"]}
        actual = set(collection_results)
        if actual != expected:
            raise IncompleteMinuteCollection(
                "collection results must terminate every watchlist symbol")
        symbols = {}
        partition_rows = []
        selection_rows = []
        missing = []
        stale = []
        quality_codes = set()
        for symbol in sorted(expected):
            result = collection_results[symbol]
            status = result.get("terminal_status")
            if status not in TERMINAL_STATUSES:
                raise IncompleteMinuteCollection(
                    "symbol {} has no valid terminal status".format(symbol))
            quality_codes.update(result.get("quality_codes", ()))
            if status in ("AVAILABLE", "STALE"):
                selected = self.store.select_from_manifest(
                    symbol, trading_session, decision_cutoff,
                    interval_minutes=interval_minutes,
                    source=result.get("source"),
                    manifest_sha256=result.get("partition_manifest_sha256"))
                if selected is None or not selected["selected_bars"]:
                    raise IncompleteMinuteCollection(
                        "available symbol has no selected bars: {}".format(symbol))
                symbols[symbol] = {
                    "partition_manifest_sha256":
                        selected["partition_manifest_sha256"],
                    "selected_bar_set_sha256":
                        selected["selected_bar_set_sha256"],
                    "terminal_status": status,
                    "latest_available_at": selected["latest_available_at"],
                    "selected_bars": selected["selected_bars"],
                }
                partition_rows.append({
                    "partition_key": symbol,
                    "manifest_path": selected["partition_manifest_path"],
                    "manifest_sha256": selected["partition_manifest_sha256"],
                    "terminal_status": status,
                    "latest_available_at": selected["latest_available_at"],
                })
                selection_rows.extend(dict(item, symbol=symbol)
                                      for item in selected["selected_bars"])
                if status == "STALE":
                    stale.append(symbol)
            else:
                symbols[symbol] = {
                    "partition_manifest_sha256": None,
                    "selected_bar_set_sha256": None,
                    "terminal_status": status,
                    "latest_available_at": None,
                    "selected_bars": [],
                }
                missing.append(symbol)
        stream_id = "market-minute:{}:{}".format(
            trading_session, int(interval_minutes))
        committed = [item for item in self.catalog.list_committed(
            "MINUTE", trading_session=trading_session)
            if item["stream_id"] == stream_id]
        previous_row = None if not committed else committed[-1]
        previous = (None if previous_row is None else
                    self.catalog.manifest(previous_row["snapshot_id"]))
        sequence = 1 if previous is None else int(previous["sequence_no"]) + 1
        manifest = {
            "schema_version": "minute_snapshot_v1",
            "stream_id": stream_id, "sequence_no": sequence,
            "previous_snapshot_id": (
                None if previous is None else previous["snapshot_id"]),
            "trading_session": trading_session,
            "interval_minutes": int(interval_minutes),
            "watchlist_id": watchlist_manifest["watchlist_id"],
            "watchlist_version": int(watchlist_manifest["watchlist_version"]),
            "decision_cutoff": decision_cutoff,
            "collection_started_at": collection_started_at,
            "collection_completed_at": collection_completed_at,
            "created_at": collection_completed_at,
            "provider_policy_version": self.provider_policy_version,
            "bar_selection_policy_version": BAR_SELECTION_POLICY_VERSION,
            "symbols": symbols, "missing_symbols": sorted(missing),
            "stale_symbols": sorted(stale),
            "quality_codes": sorted(quality_codes),
        }
        extra_events, findings = self._revision_audit(
            previous, symbols, collection_completed_at, trading_session,
            interval_minutes)
        row, event, created = self.catalog.publish(
            "MINUTE", manifest, source_service="minute-market-hub",
            event_type="MinuteSnapshotCommitted",
            partition_rows=partition_rows, selection_rows=selection_rows,
            additional_events=extra_events, audit_findings=findings)
        document = self.catalog.manifest(row["snapshot_id"])
        return {
            "snapshot": row, "event": event, "created": created,
            "manifest": document, "metrics": minute_snapshot_metrics(document),
        }


def minute_snapshot_metrics(manifest):
    total = len(manifest["symbols"])
    available = sum(
        item["terminal_status"] == "AVAILABLE"
        for item in manifest["symbols"].values())
    selected = [
        item for record in manifest["symbols"].values()
        for item in record.get("selected_bars", ())]
    latencies = []
    revisions = 0
    gap_count = 0
    for record in manifest["symbols"].values():
        ends = sorted(pd.Timestamp(item["business_bar_key"].split(":", 2)[2])
                      for item in record.get("selected_bars", ()))
        for left, right in zip(ends, ends[1:]):
            delta = (right - left).total_seconds() / 60.0
            scheduled = (
                (left.strftime("%H:%M") == "11:30" and
                 right.strftime("%H:%M") == "13:01") or
                (left.strftime("%H:%M") == "14:57" and
                 right.strftime("%H:%M") == "15:00"))
            if delta > int(manifest["interval_minutes"]) and not scheduled:
                gap_count += 1
    for item in selected:
        parts = item["business_bar_key"].split(":", 2)
        bar_end = pd.Timestamp(parts[2])
        available_at = pd.Timestamp(item["available_at"])
        latencies.append(max(0.0, (available_at - bar_end).total_seconds() * 1000))
        revisions += int(item["revision"] > 1)
    series = pd.Series(latencies, dtype=float)
    return {
        "schema_version": "minute_snapshot_metrics_v1",
        "snapshot_id": manifest["snapshot_id"],
        "symbol_count": total, "available_symbol_count": available,
        "coverage": (available / total if total else 0.0),
        "selected_bar_count": len(selected), "revised_bar_count": revisions,
        "gap_count": gap_count,
        "provider_switch_observed": (
            "PROVIDER_SWITCHED" in manifest["quality_codes"]),
        "p95_latency_ms": (0.0 if series.empty else float(series.quantile(.95))),
        "p99_latency_ms": (0.0 if series.empty else float(series.quantile(.99))),
        "missing_symbol_count": len(manifest["missing_symbols"]),
        "stale_symbol_count": len(manifest["stale_symbols"]),
        "quality_codes": list(manifest["quality_codes"]),
    }


class MinuteSnapshotConsumer(object):
    """Strict incremental consumer; handlers never reopen CURRENT manifests."""

    def __init__(self, operational_store, snapshot_catalog, minute_store):
        self.store = operational_store
        self.catalog = snapshot_catalog
        self.minute_store = (minute_store if isinstance(minute_store, MinuteBarStore)
                             else MinuteBarStore(minute_store))
        self.events = DomainEventStore(operational_store)
        self.dispatcher = EventDispatcher(operational_store)

    def register(self, consumer_id, stream_id, created_at, *, required=True):
        return self.events.register_consumer(
            consumer_id, stream_id, 1, required=required,
            retention_class="PERMANENT_AUDIT", created_at=created_at)

    def consume_next(self, consumer_id, handler, now,
                     *, gap_timeout_reached=False):
        def consume(event, connection):
            if event["event_type"] != "MinuteSnapshotCommitted":
                raise ValueError("minute consumer received non-minute event")
            manifest = self.catalog.manifest(event["snapshot_id"])
            consumed_keys = set()
            prior = connection.execute(
                "SELECT de.snapshot_id FROM event_consumptions ec "
                "JOIN domain_events de ON de.event_id=ec.event_id "
                "WHERE ec.consumer_id=? AND de.stream_id=? "
                "AND de.event_type='MinuteSnapshotCommitted'",
                (consumer_id, manifest["stream_id"])).fetchall()
            for row in prior:
                prior_manifest = self.catalog.manifest(row["snapshot_id"])
                consumed_keys.update(
                    (symbol, selected["business_bar_key"])
                    for symbol, record in prior_manifest["symbols"].items()
                    for selected in record.get("selected_bars", ()))
            events_by_symbol = {}
            for symbol, record in manifest["symbols"].items():
                if record["terminal_status"] not in ("AVAILABLE", "STALE"):
                    continue
                events = self.minute_store.read_selected(record)
                fresh = tuple(
                    item for item, selected in zip(events, record["selected_bars"])
                    if (symbol, selected["business_bar_key"]) not in consumed_keys)
                if fresh:
                    events_by_symbol[symbol] = fresh
            batch = MinuteSnapshotBatch(
                snapshot_id=manifest["snapshot_id"],
                stream_id=manifest["stream_id"],
                sequence_no=int(manifest["sequence_no"]),
                previous_snapshot_id=manifest["previous_snapshot_id"],
                decision_cutoff=manifest["decision_cutoff"],
                watchlist_id=manifest["watchlist_id"],
                watchlist_version=int(manifest["watchlist_version"]),
                manifest=manifest, events_by_symbol=events_by_symbol)
            return handler(batch, connection)
        return self.dispatcher.dispatch_next(
            consumer_id, consume, now,
            gap_timeout_reached=gap_timeout_reached)
