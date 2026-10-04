# -*- encoding: utf-8 -*-
"""Immutable minute-bar batches with versioned manifests and as-of reads."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

from .ABuRealtimeMarket import MinuteBarEvent, _as_shanghai_timestamp


STORE_SCHEMA_VERSION = "minute_bar_store_v1"
MANIFEST_SCHEMA_VERSION = "minute_bar_manifest_v1"


def _canonical_json(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _event_payload(event):
    payload = asdict(event)
    payload["quality_codes"] = list(payload["quality_codes"])
    return payload


def _event_from_payload(payload):
    item = dict(payload)
    item["quality_codes"] = tuple(item.get("quality_codes", ()))
    return MinuteBarEvent(**item)


def _market_identity(event):
    payload = _event_payload(event)
    for key in ("revision", "request_started_at", "received_at", "available_at",
                "quality_codes"):
        payload.pop(key, None)
    return _sha256_text(_canonical_json(payload))


def _business_key(event):
    return (event.symbol, int(event.interval_minutes), event.bar_end, event.source)


def _partition_key(event):
    date = _as_shanghai_timestamp(event.bar_end).strftime("%Y%m%d")
    return date, event.source, int(event.interval_minutes), event.symbol


class MinuteBarStore(object):
    """Append-only store.

    A partition never rewrites historical batch files. Writers serialize the
    manifest pointer update with an advisory file lock. Readers bind to the
    manifest named by ``CURRENT`` and therefore never observe a partial batch.
    """

    def __init__(self, root):
        self.root = Path(root)

    def _partition(self, key):
        date, source, interval, symbol = key
        return (self.root / "trading_date={}".format(date) /
                "provider={}".format(source) /
                "interval={}".format(interval) /
                "symbol={}".format(symbol))

    def append_raw_response(self, raw, provider, symbol, interval_minutes,
                            request_started_at, received_at):
        """Archive one provider response immutably before normalization."""
        if hasattr(raw, "to_json"):
            records = json.loads(raw.to_json(
                orient="records", date_format="iso", force_ascii=False))
        else:
            records = raw
        received = _as_shanghai_timestamp(received_at)
        payload = {
            "schema_version": "minute_raw_response_v1",
            "provider": str(provider), "symbol": str(symbol),
            "interval_minutes": int(interval_minutes),
            "request_started_at": _as_shanghai_timestamp(
                request_started_at).isoformat(),
            "received_at": received.isoformat(),
            "records": records,
        }
        content = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False) + "\n"
        digest = _sha256_text(content)
        path = (self.root / "raw" /
                "trading_date={}".format(received.strftime("%Y%m%d")) /
                "provider={}".format(provider) /
                "interval={}".format(int(interval_minutes)) /
                "symbol={}".format(symbol) /
                "{}.json".format(digest))
        if not path.exists():
            self._atomic_text(path, content)
        return {"path": str(path), "sha256": digest}

    @contextlib.contextmanager
    def _locked(self, partition):
        partition.mkdir(parents=True, exist_ok=True)
        lock_path = partition / ".write.lock"
        stream = lock_path.open("a+")
        try:
            try:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            except ImportError as error:  # pragma: no cover - non-POSIX safety.
                raise RuntimeError("minute store requires file locking") from error
            yield
        finally:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    @staticmethod
    def _atomic_text(path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("{}.{}.tmp".format(path.name, os.getpid()))
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)

    def _current_manifest(self, partition):
        pointer = partition / "CURRENT"
        if not pointer.exists():
            return None
        name = pointer.read_text(encoding="utf-8").strip()
        if not name or Path(name).name != name:
            raise ValueError("invalid minute store CURRENT pointer")
        path = partition / "manifests" / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        expected = payload.pop("manifest_sha256", None)
        actual = _sha256_text(_canonical_json(payload))
        if expected != actual or expected not in name:
            raise ValueError("minute manifest hash mismatch")
        payload["manifest_sha256"] = expected
        return payload

    def _records(self, partition, manifest=None):
        manifest = manifest or self._current_manifest(partition)
        if manifest is None:
            return []
        records = []
        for batch in manifest["batches"]:
            path = partition / "batches" / batch["file"]
            content = path.read_text(encoding="utf-8")
            if _sha256_text(content) != batch["sha256"]:
                raise ValueError("minute batch hash mismatch")
            for line in content.splitlines():
                if line:
                    records.append(_event_from_payload(json.loads(line)))
        return records

    def append(self, events, raw_reference=None):
        events = list(events)
        if not events:
            raise ValueError("cannot append an empty minute event batch")
        groups = {}
        for event in events:
            if not isinstance(event, MinuteBarEvent):
                raise TypeError("events must be MinuteBarEvent instances")
            groups.setdefault(_partition_key(event), []).append(event)
        results = []
        for key in sorted(groups):
            results.append(self._append_partition(
                key, groups[key], raw_reference=raw_reference))
        return results

    def _append_partition(self, key, events, raw_reference=None):
        partition = self._partition(key)
        with self._locked(partition):
            previous = self._current_manifest(partition)
            existing = self._records(partition, previous)
            by_key = {}
            for item in existing:
                by_key.setdefault(_business_key(item), []).append(item)
            additions = []
            for event in sorted(events, key=lambda item: (
                    item.bar_end, item.available_at, item.revision)):
                versions = by_key.get(_business_key(event), [])
                identity = _market_identity(event)
                if any(_market_identity(item) == identity for item in versions):
                    continue
                revision = max([item.revision for item in versions] or [0]) + 1
                normalized = replace(event, revision=revision)
                additions.append(normalized)
                by_key.setdefault(_business_key(normalized), []).append(normalized)
            if not additions:
                return {
                    "partition": str(partition), "appended": 0,
                    "manifest_sha256": (previous or {}).get("manifest_sha256"),
                }
            lines = "".join(
                _canonical_json(_event_payload(item)) + "\n" for item in additions)
            batch_sha = _sha256_text(lines)
            batch_name = "{}.jsonl".format(batch_sha)
            batch_path = partition / "batches" / batch_name
            if not batch_path.exists():
                self._atomic_text(batch_path, lines)
            batches = list((previous or {}).get("batches", []))
            batches.append({
                "file": batch_name, "sha256": batch_sha,
                "row_count": len(additions),
                "raw_reference": raw_reference,
            })
            payload = {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "store_schema_version": STORE_SCHEMA_VERSION,
                "partition": {
                    "trading_date": key[0], "provider": key[1],
                    "interval_minutes": key[2], "symbol": key[3],
                },
                "previous_manifest_sha256": (
                    (previous or {}).get("manifest_sha256")),
                "batches": batches,
                "row_count": sum(item["row_count"] for item in batches),
            }
            manifest_sha = _sha256_text(_canonical_json(payload))
            payload["manifest_sha256"] = manifest_sha
            manifest_name = "{}.json".format(manifest_sha)
            manifest_path = partition / "manifests" / manifest_name
            if not manifest_path.exists():
                self._atomic_text(
                    manifest_path,
                    json.dumps(payload, ensure_ascii=False,
                               sort_keys=True, indent=2) + "\n")
            self._atomic_text(partition / "CURRENT", manifest_name + "\n")
            return {
                "partition": str(partition), "appended": len(additions),
                "manifest_sha256": manifest_sha,
            }

    def read(self, symbol, trade_date, interval_minutes=1, source=None,
             as_of=None, latest_revision=True):
        sources = [source] if source else self._sources(
            symbol, trade_date, interval_minutes)
        events = []
        for provider in sources:
            key = (str(trade_date).replace("-", ""), provider,
                   int(interval_minutes), symbol)
            partition = self._partition(key)
            if partition.exists():
                events.extend(self._records(partition))
        if as_of is not None:
            cutoff = _as_shanghai_timestamp(as_of)
            events = [item for item in events
                      if _as_shanghai_timestamp(item.available_at) <= cutoff]
        if latest_revision:
            latest = {}
            for item in events:
                key = _business_key(item)
                previous = latest.get(key)
                if previous is None or item.revision > previous.revision:
                    latest[key] = item
            events = list(latest.values())
        return sorted(events, key=lambda item: (
            item.bar_end, item.source, item.revision))

    def _sources(self, symbol, trade_date, interval_minutes):
        date_root = self.root / "trading_date={}".format(
            str(trade_date).replace("-", ""))
        if not date_root.exists():
            return []
        sources = []
        for provider_path in date_root.glob("provider=*"):
            partition = (provider_path /
                         "interval={}".format(int(interval_minutes)) /
                         "symbol={}".format(symbol))
            if partition.exists():
                sources.append(provider_path.name.split("=", 1)[1])
        return sorted(sources)

    def audit(self, symbol, trade_date, interval_minutes=1, source=None):
        events = self.read(
            symbol, trade_date, interval_minutes, source=source,
            latest_revision=True)
        complete = [item for item in events if item.is_complete]
        ends = [_as_shanghai_timestamp(item.bar_end) for item in complete]
        duplicates = len(ends) - len(set(ends))
        gaps = []
        for left, right in zip(sorted(set(ends)), sorted(set(ends))[1:]):
            delta = (right - left).total_seconds() / 60.0
            if delta > interval_minutes and not (
                    left.strftime("%H:%M") == "11:30" and
                    right.strftime("%H:%M") == "13:01"):
                gaps.append({"after": left.isoformat(),
                             "before": right.isoformat(), "minutes": delta})
        return {
            "schema_version": "minute_bar_audit_v1",
            "symbol": symbol,
            "trade_date": str(trade_date).replace("-", ""),
            "interval_minutes": int(interval_minutes),
            "event_count": len(events),
            "complete_count": len(complete),
            "duplicate_bar_end_count": duplicates,
            "gap_count": len(gaps),
            "gaps": gaps,
            "amount_missing_count": sum(
                item.amount_raw is None for item in events),
            "volume_missing_count": sum(
                item.volume_shares is None for item in events),
            "quality_codes": sorted({
                code for item in events for code in item.quality_codes}),
            "healthy": bool(events) and duplicates == 0 and not gaps,
        }


def minute_events_from_frame(frame):
    """Convert the normalized adapter frame into validated events."""
    result = []
    for row in frame.itertuples(index=False):
        result.append(MinuteBarEvent(
            symbol=row.symbol, interval_minutes=int(row.interval_minutes),
            source_timestamp=row.source_timestamp.isoformat(),
            bar_start=row.bar_start.isoformat(), bar_end=row.bar_end.isoformat(),
            request_started_at=row.request_started_at.isoformat(),
            received_at=row.received_at.isoformat(),
            available_at=row.available_at.isoformat(),
            open_raw=float(row.open_raw), high_raw=float(row.high_raw),
            low_raw=float(row.low_raw), close_raw=float(row.close_raw),
            volume_shares=float(row.volume_shares),
            amount_raw=(None if pd.isna(row.amount_raw)
                        else float(row.amount_raw)),
            source=row.source, revision=int(row.revision),
            is_complete=bool(row.is_complete),
            quality_codes=tuple(row.quality_codes),
        ))
    return result
