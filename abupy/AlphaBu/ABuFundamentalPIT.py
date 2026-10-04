# -*- encoding: utf-8 -*-
"""Point-in-time fundamental raw storage and normalization primitives."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


RAW_OUTCOMES = {
    "SUCCESS_NONEMPTY", "SUCCESS_EMPTY", "NO_DISCLOSURE",
    "SOURCE_UNSUPPORTED", "NETWORK_FAILURE", "HTTP_FAILURE",
    "PARSE_FAILURE",
}
SENSITIVE_KEY_PATTERN = re.compile(
    r"(token|secret|password|passwd|authorization|api[_-]?key|cookie)",
    re.IGNORECASE,
)
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
REPORT_KINDS = {"Q1", "H1", "Q3", "FY"}
STATEMENT_TYPES = {"income", "balance", "cashflow", "shares"}
VALUE_KINDS = {"cumulative", "single_period", "instant"}


def stable_json(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )


def sha256_bytes(payload):
    return hashlib.sha256(payload).hexdigest()


def redact_sensitive(payload):
    """Recursively redact credentials before requests enter an audit file."""
    if isinstance(payload, dict):
        return {
            str(key): (
                "<REDACTED>" if SENSITIVE_KEY_PATTERN.search(str(key))
                else redact_sensitive(value)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [redact_sensitive(value) for value in payload]
    return payload


class ImmutableFundamentalRawStore(object):
    """Content-addressed payloads plus append-only capture batch metadata."""

    def __init__(self, root, clock=None, batch_id_factory=None):
        self.root = Path(root)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.batch_id_factory = batch_id_factory or (
            lambda: uuid.uuid4().hex)

    def capture(self, source, dataset, request, payload, outcome,
                source_version, http_status=None, record_count=None,
                currency=None, unit=None, response_headers=None, error=None,
                symbol=None):
        if outcome not in RAW_OUTCOMES:
            raise ValueError("unknown raw capture outcome")
        if not isinstance(payload, bytes):
            raise TypeError("raw payload must be bytes")
        retrieved = self.clock()
        if retrieved.tzinfo is None or retrieved.utcoffset() is None:
            raise ValueError("raw capture clock must be timezone-aware")
        digest = sha256_bytes(payload)
        blob = self.root / "blobs" / digest[:2] / (digest + ".bin")
        blob.parent.mkdir(parents=True, exist_ok=True)
        if blob.exists():
            if sha256_bytes(blob.read_bytes()) != digest:
                raise ValueError("existing raw blob hash mismatch")
        else:
            temporary = blob.with_suffix(".bin.tmp-" + uuid.uuid4().hex)
            temporary.write_bytes(payload)
            try:
                os.link(temporary, blob)
            except FileExistsError:
                pass
            finally:
                temporary.unlink(missing_ok=True)

        batch_id = str(self.batch_id_factory())
        safe_source = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(source))
        safe_dataset = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(dataset))
        timestamp = retrieved.astimezone(timezone.utc).strftime(
            "%Y%m%dT%H%M%S.%fZ")
        metadata_path = (
            self.root / "batches" / timestamp[:8] / safe_source /
            safe_dataset / (timestamp + "-" + batch_id + ".json")
        )
        metadata = {
            "batch_id": batch_id,
            "source": str(source),
            "source_version": str(source_version),
            "dataset": str(dataset),
            "symbol": None if symbol is None else str(symbol),
            "retrieved_at_utc": retrieved.astimezone(timezone.utc).isoformat(),
            "request": redact_sensitive(request),
            "http_status": http_status,
            "outcome": outcome,
            "record_count": record_count,
            "currency": currency,
            "unit": unit,
            "response_headers": redact_sensitive(response_headers or {}),
            "error": None if error is None else {
                "type": type(error).__name__,
                "message": str(error),
            },
            "payload_sha256": digest,
            "payload_bytes": len(payload),
            "blob_path": str(blob.relative_to(self.root)),
        }
        encoded = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with metadata_path.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
        except FileExistsError as error_:
            raise ValueError("raw capture batch id collision") from error_
        return metadata_path, metadata

    def read_payload(self, metadata_or_path):
        metadata = (
            json.loads(Path(metadata_or_path).read_text(encoding="utf-8"))
            if isinstance(metadata_or_path, (str, Path))
            else metadata_or_path
        )
        path = self.root / metadata["blob_path"]
        payload = path.read_bytes()
        if sha256_bytes(payload) != metadata["payload_sha256"]:
            raise ValueError("raw fundamental payload hash mismatch")
        return payload


def fundamental_source_conflicts(primary_records, auxiliary_records,
                                 key_fields, value_fields):
    """Return deterministic explicit conflicts; never overwrite a source."""
    def index(records):
        result = {}
        for record in records:
            key = tuple(record.get(field) for field in key_fields)
            if key in result:
                raise ValueError("duplicate fundamental source key")
            result[key] = record
        return result

    primary = index(primary_records)
    auxiliary = index(auxiliary_records)
    conflicts = []
    for key in sorted(set(primary) & set(auxiliary), key=lambda item: repr(item)):
        differences = {}
        for field in value_fields:
            left, right = primary[key].get(field), auxiliary[key].get(field)
            if left != right:
                differences[field] = {"primary": left, "auxiliary": right}
        if differences:
            conflicts.append({
                "key": dict(zip(key_fields, key)),
                "differences": differences,
            })
    return conflicts


def _aware_datetime(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed


def conservative_available_at(announcement, trading_sessions):
    """Map date-only announcements to the next session's open in Shanghai."""
    if isinstance(announcement, date) and not isinstance(announcement, datetime):
        announcement_date = announcement
    elif isinstance(announcement, str) and re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", announcement):
        announcement_date = date.fromisoformat(announcement)
    else:
        return _aware_datetime(announcement).astimezone(SHANGHAI_TZ)
    sessions = sorted(
        value if isinstance(value, date) else date.fromisoformat(str(value))
        for value in trading_sessions
    )
    future = [value for value in sessions if value > announcement_date]
    if not future:
        raise ValueError("no next trading session for date-only announcement")
    return datetime.combine(future[0], time(9, 25), tzinfo=SHANGHAI_TZ)


def report_kind(report_period):
    period = date.fromisoformat(str(report_period))
    mapping = {(3, 31): "Q1", (6, 30): "H1",
               (9, 30): "Q3", (12, 31): "FY"}
    try:
        return mapping[(period.month, period.day)]
    except KeyError as error:
        raise ValueError("unsupported fiscal report period") from error


@dataclass(frozen=True)
class FundamentalFact(object):
    symbol: str
    statement_type: str
    report_period: str
    report_kind: str
    announcement_date: str
    available_at: str
    revision_id: str
    is_restated: bool
    source: str
    source_key: str
    ingested_at: str
    currency: str
    unit_scale: float
    field: str
    value: float
    value_kind: str
    raw_field: str
    raw_value: float
    mapping_version: str
    raw_payload_sha256: str
    validation_status: str = "VALID"
    missing_reason: str | None = None
    revision_history_complete: bool = True

    def __post_init__(self):
        if not re.fullmatch(r"(sh|sz)\d{6}", self.symbol):
            raise ValueError("invalid A-share symbol")
        if self.statement_type not in STATEMENT_TYPES:
            raise ValueError("invalid statement type")
        if self.report_kind not in REPORT_KINDS:
            raise ValueError("invalid report kind")
        if self.report_kind != report_kind(self.report_period):
            raise ValueError("report kind and period disagree")
        if self.value_kind not in VALUE_KINDS:
            raise ValueError("invalid value kind")
        _aware_datetime(self.available_at)
        _aware_datetime(self.ingested_at)
        if not self.revision_id or not self.source_key or not self.field:
            raise ValueError("fact identity fields are required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_payload_sha256):
            raise ValueError("invalid raw payload hash")
        if self.currency != "CNY":
            raise ValueError("only explicit CNY facts are supported")
        if not np.isfinite(self.unit_scale) or self.unit_scale <= 0:
            raise ValueError("unit scale must be positive")
        if self.validation_status == "VALID" and not np.isfinite(self.value):
            raise ValueError("valid fact value must be finite")

    def record(self):
        return asdict(self)


@dataclass(frozen=True)
class DerivedFundamentalValue(object):
    value: float | None
    missing_reason: str | None
    source_keys: tuple[str, ...]


class FundamentalFactStore(object):
    """Version-aware facts queried only with an explicit timestamp."""

    def __init__(self, facts):
        self.facts = tuple(
            fact if isinstance(fact, FundamentalFact)
            else FundamentalFact(**fact)
            for fact in facts
        )
        identities = set()
        for fact in self.facts:
            identity = (
                fact.symbol, fact.statement_type, fact.report_period,
                fact.field, fact.revision_id,
            )
            if identity in identities:
                raise ValueError("duplicate fundamental fact revision")
            identities.add(identity)

    def facts_asof(self, symbol, asof_timestamp, field=None,
                   statement_type=None):
        asof = _aware_datetime(asof_timestamp)
        eligible = [fact for fact in self.facts
                    if fact.symbol == symbol and
                    _aware_datetime(fact.available_at) <= asof and
                    (field is None or fact.field == field) and
                    (statement_type is None or
                     fact.statement_type == statement_type)]
        latest = {}
        for fact in sorted(
                eligible, key=lambda item: (
                    _aware_datetime(item.available_at), item.revision_id,
                    item.source_key)):
            latest[(fact.statement_type, fact.report_period,
                    fact.field)] = fact
        return tuple(sorted(latest.values(), key=lambda item: (
            item.report_period, item.statement_type, item.field)))

    def fact_asof(self, symbol, statement_type, report_period, field,
                  asof_timestamp):
        matches = [fact for fact in self.facts_asof(
            symbol, asof_timestamp, field=field,
            statement_type=statement_type)
            if fact.report_period == str(report_period)]
        return matches[-1] if matches else None

    def single_quarters_asof(self, symbol, statement_type, field,
                             asof_timestamp):
        if statement_type not in {"income", "cashflow"}:
            raise ValueError("single-quarter derivation requires flow statement")
        facts = [fact for fact in self.facts_asof(
            symbol, asof_timestamp, field=field,
            statement_type=statement_type) if fact.value_kind in {
                "cumulative", "single_period"}]
        by_year = {}
        for fact in facts:
            by_year.setdefault(date.fromisoformat(fact.report_period).year, {})[
                fact.report_kind] = fact
        result = {}
        for year, values in sorted(by_year.items()):
            prior_value = 0.0
            prior_sources = ()
            for quarter, kind in enumerate(("Q1", "H1", "Q3", "FY"), 1):
                fact = values.get(kind)
                ordinal = year * 4 + quarter - 1
                if fact is None:
                    result[ordinal] = DerivedFundamentalValue(
                        None, "MISSING_CUMULATIVE_PERIOD", ())
                    prior_value = None
                    prior_sources = ()
                    continue
                if fact.value_kind == "single_period":
                    result[ordinal] = DerivedFundamentalValue(
                        fact.value, None, (fact.source_key,))
                    continue
                if quarter == 1:
                    value = fact.value
                    sources = (fact.source_key,)
                elif prior_value is None:
                    result[ordinal] = DerivedFundamentalValue(
                        None, "MISSING_PRIOR_CUMULATIVE_PERIOD",
                        (fact.source_key,))
                    prior_value = fact.value
                    prior_sources = (fact.source_key,)
                    continue
                else:
                    value = fact.value - prior_value
                    sources = prior_sources + (fact.source_key,)
                result[ordinal] = DerivedFundamentalValue(value, None, sources)
                prior_value = fact.value
                prior_sources = (fact.source_key,)
        return result

    def ttm_asof(self, symbol, statement_type, field, asof_timestamp,
                 end_report_period=None):
        quarters = self.single_quarters_asof(
            symbol, statement_type, field, asof_timestamp)
        if end_report_period is None:
            available = [key for key, value in quarters.items()
                         if value.value is not None]
            if not available:
                return DerivedFundamentalValue(None, "NO_QUARTERS", ())
            end = max(available)
        else:
            period = date.fromisoformat(str(end_report_period))
            kind = report_kind(period.isoformat())
            end = period.year * 4 + {"Q1": 0, "H1": 1,
                                     "Q3": 2, "FY": 3}[kind]
        window = [quarters.get(key) for key in range(end - 3, end + 1)]
        if any(value is None or value.value is None for value in window):
            reasons = tuple(
                "MISSING_QUARTER" if value is None else value.missing_reason
                for value in window
                if value is None or value.value is None)
            return DerivedFundamentalValue(
                None, "TTM_INCOMPLETE:" + ",".join(reasons), ())
        return DerivedFundamentalValue(
            float(sum(value.value for value in window)), None,
            tuple(source for value in window for source in value.source_keys),
        )

    def average_instant_asof(self, symbol, field, start_period, end_period,
                             asof_timestamp):
        start = self.fact_asof(symbol, "balance", start_period, field,
                               asof_timestamp)
        end = self.fact_asof(symbol, "balance", end_period, field,
                             asof_timestamp)
        if start is None or end is None:
            return DerivedFundamentalValue(
                None, "MISSING_AVERAGE_BALANCE_ENDPOINT", ())
        return DerivedFundamentalValue(
            (start.value + end.value) / 2.0, None,
            (start.source_key, end.source_key))


def normalize_extracted_facts(records, mapping, trading_sessions,
                              mapping_version):
    """Normalize explicitly extracted report rows; never infer provenance."""
    output = []
    fields = mapping["fields"]
    allowed_scales = set(float(value) for value in mapping["allowed_unit_scales"])
    for record in records:
        raw_field = str(record["raw_field"])
        if raw_field not in fields:
            continue
        definition = fields[raw_field]
        if record["statement_type"] not in definition["statement_types"]:
            raise ValueError("raw field appears in an invalid statement type")
        scale = float(record["unit_scale"])
        if scale not in allowed_scales:
            raise ValueError("unrecognized fundamental unit scale")
        raw_value = float(record["raw_value"])
        if not np.isfinite(raw_value):
            raise ValueError("raw fundamental value is not finite")
        available = conservative_available_at(
            record["announcement"], trading_sessions)
        ingested = _aware_datetime(record["ingested_at"])
        fact = FundamentalFact(
            symbol=record["symbol"],
            statement_type=record["statement_type"],
            report_period=str(record["report_period"]),
            report_kind=report_kind(record["report_period"]),
            announcement_date=str(record["announcement"])[:10],
            available_at=available.isoformat(),
            revision_id=str(record["revision_id"]),
            is_restated=bool(record["is_restated"]),
            source=str(record["source"]),
            source_key=str(record["source_key"]),
            ingested_at=ingested.isoformat(),
            currency=str(record["currency"]), unit_scale=scale,
            field=definition["standard_field"],
            value=raw_value * scale,
            value_kind=definition["value_kind"],
            raw_field=raw_field, raw_value=raw_value,
            mapping_version=mapping_version,
            raw_payload_sha256=str(record["raw_payload_sha256"]),
            revision_history_complete=bool(record.get(
                "revision_history_complete", True)),
        )
        output.append(fact)
    return output


def _structured_date(value, field):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        raise ValueError("missing structured " + field)
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ValueError("invalid structured " + field) from error


def structured_statement_records(rows, statement_type, symbol, mapping,
                                 raw_payload_sha256, ingested_at):
    """Convert a current Eastmoney statement snapshot without backfilling.

    The endpoint generally exposes one current row per report period rather
    than every historical revision.  Therefore the current value is only
    eligible from the later of NOTICE_DATE and UPDATE_DATE and every emitted
    record explicitly carries ``revision_history_complete=False``.
    """
    if statement_type not in {"income", "balance", "cashflow"}:
        raise ValueError("invalid structured statement type")
    if not re.fullmatch(r"(sh|sz)\d{6}", str(symbol)):
        raise ValueError("invalid structured statement symbol")
    if not re.fullmatch(r"[0-9a-f]{64}", str(raw_payload_sha256)):
        raise ValueError("invalid structured payload hash")
    _aware_datetime(ingested_at)
    definitions = mapping["fields"]
    output = []
    excluded = {}
    seen_periods = set()
    eligible_rows = 0
    restated_rows = 0
    for row in rows:
        try:
            period = _structured_date(row.get("REPORT_DATE"), "REPORT_DATE")
            report_kind(period.isoformat())
            notice = _structured_date(row.get("NOTICE_DATE"), "NOTICE_DATE")
            update_value = row.get("UPDATE_DATE")
            update = notice if update_value in (None, "") or pd.isna(
                update_value) else _structured_date(update_value, "UPDATE_DATE")
            if update < notice:
                raise ValueError("UPDATE_DATE_BEFORE_NOTICE_DATE")
            if str(row.get("CURRENCY") or "").upper() != "CNY":
                raise ValueError("NON_CNY")
        except ValueError as error:
            reason = str(error)
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        period_text = period.isoformat()
        if period_text in seen_periods:
            raise ValueError("duplicate structured statement report period")
        seen_periods.add(period_text)
        eligible_rows += 1
        is_restated = update > notice
        restated_rows += int(is_restated)
        available_date = max(notice, update).isoformat()
        revision_basis = {
            "symbol": symbol, "statement_type": statement_type,
            "report_period": period_text,
            "notice_date": notice.isoformat(),
            "update_date": update.isoformat(),
            "payload_sha256": raw_payload_sha256,
        }
        revision_id = sha256_bytes(stable_json(revision_basis).encode(
            "utf-8"))
        for raw_field, definition in definitions.items():
            if statement_type not in definition["statement_types"]:
                continue
            raw_value = row.get(raw_field)
            if raw_value is None or pd.isna(raw_value):
                continue
            try:
                numeric = float(raw_value)
            except (TypeError, ValueError):
                continue
            if not np.isfinite(numeric):
                continue
            output.append({
                "symbol": symbol,
                "statement_type": statement_type,
                "report_period": period_text,
                "announcement": available_date,
                "notice_date": notice.isoformat(),
                "update_date": update.isoformat(),
                "revision_id": revision_id,
                "is_restated": is_restated,
                "revision_history_complete": False,
                "source": "eastmoney_structured",
                "source_key": ":".join((
                    "eastmoney", symbol, statement_type, period_text,
                    revision_id, raw_field,
                )),
                "ingested_at": _aware_datetime(ingested_at).isoformat(),
                "currency": "CNY",
                "unit_scale": 1.0,
                "raw_field": raw_field,
                "raw_value": numeric,
                "raw_payload_sha256": raw_payload_sha256,
            })
    return output, {
        "input_rows": len(rows),
        "eligible_rows": eligible_rows,
        "emitted_values": len(output),
        "restated_rows": restated_rows,
        "revision_history_complete": False,
        "excluded_rows": sum(excluded.values()),
        "exclusion_reasons": dict(sorted(excluded.items())),
    }


@dataclass(frozen=True)
class ShareCapitalEvent(object):
    symbol: str
    effective_at: str
    total_shares: float
    float_shares: float
    source_key: str
    raw_payload_sha256: str
    event_date: str | None = None
    announcement_date: str | None = None
    float_share_basis: str = "circulating_a_shares"

    def __post_init__(self):
        _aware_datetime(self.effective_at)
        if not re.fullmatch(r"(sh|sz)\d{6}", self.symbol):
            raise ValueError("invalid share-capital symbol")
        if not np.isfinite(self.total_shares) or self.total_shares <= 0:
            raise ValueError("total shares must be positive")
        if not np.isfinite(self.float_shares) or self.float_shares <= 0:
            raise ValueError("float shares must be positive")
        if self.float_shares > self.total_shares:
            raise ValueError("float shares cannot exceed total shares")
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_payload_sha256):
            raise ValueError("invalid share-capital payload hash")
        if self.event_date:
            date.fromisoformat(self.event_date)
        if self.announcement_date:
            date.fromisoformat(self.announcement_date)
        if self.float_share_basis != "circulating_a_shares":
            raise ValueError("unsupported float-share basis")


def structured_share_events(rows, symbol, raw_payload_sha256,
                            trading_sessions, unit_scale=10000.0):
    """Normalize CNINFO p_stock2215 rows using their documented ten-thousand
    share scale and a conservative information-availability timestamp.

    F021N is all circulated shares and can include B/H shares.  The local
    raw A-share close therefore uses F022N (circulating RMB ordinary shares)
    for its float-market-cap denominator.
    """
    if not re.fullmatch(r"(sh|sz)\d{6}", str(symbol)):
        raise ValueError("invalid structured share symbol")
    if not re.fullmatch(r"[0-9a-f]{64}", str(raw_payload_sha256)):
        raise ValueError("invalid structured share payload hash")
    scale = float(unit_scale)
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("share unit scale must be positive")
    events = []
    excluded = {}
    seen = set()
    for row in rows:
        try:
            announced = _structured_date(
                row.get("DECLAREDATE"), "DECLAREDATE")
            changed = _structured_date(row.get("VARYDATE"), "VARYDATE")
            total = float(row.get("F003N")) * scale
            floated = float(row.get("F022N")) * scale
            if not np.isfinite(total) or not np.isfinite(floated):
                raise ValueError("NON_FINITE_SHARE_CAPITAL")
            if total <= 0 or floated <= 0 or floated > total:
                raise ValueError("INVALID_SHARE_CAPITAL")
            available_date = max(announced, changed)
            effective = conservative_available_at(
                available_date.isoformat(), trading_sessions)
        except (TypeError, ValueError) as error:
            reason = str(error)
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        identity = (changed.isoformat(), announced.isoformat(), total, floated)
        if identity in seen:
            continue
        seen.add(identity)
        source_key = "cninfo_share:{}:{}:{}:{}".format(
            symbol, changed.isoformat(), announced.isoformat(),
            sha256_bytes(stable_json(identity).encode("utf-8")),
        )
        events.append(ShareCapitalEvent(
            symbol=symbol, effective_at=effective.isoformat(),
            total_shares=total, float_shares=floated,
            source_key=source_key,
            raw_payload_sha256=raw_payload_sha256,
            event_date=changed.isoformat(),
            announcement_date=announced.isoformat(),
        ))
    events.sort(key=lambda item: (
        _aware_datetime(item.effective_at), item.source_key))
    return events, {
        "input_rows": len(rows), "normalized_events": len(events),
        "unit_scale": scale, "float_share_field": "F022N",
        "float_share_basis": "circulating_a_shares",
        "excluded_rows": sum(excluded.values()),
        "exclusion_reasons": dict(sorted(excluded.items())),
    }


class ShareCapitalStore(object):
    def __init__(self, events):
        self.events = tuple(
            event if isinstance(event, ShareCapitalEvent)
            else ShareCapitalEvent(**event) for event in events)

    def asof(self, symbol, asof_timestamp):
        asof = _aware_datetime(asof_timestamp)
        eligible = [event for event in self.events
                    if event.symbol == symbol and
                    _aware_datetime(event.effective_at) <= asof]
        if not eligible:
            return None
        # The interface mixes true change events with periodic snapshots.
        # Availability time controls whether a row is knowable; economic time
        # controls which already-known state is current.  A later-published,
        # older periodic snapshot must not roll back a newer change event.
        def state_key(event):
            economic_date = (
                date.fromisoformat(event.event_date)
                if event.event_date else
                _aware_datetime(event.effective_at).date()
            )
            return (economic_date, _aware_datetime(event.effective_at),
                    event.source_key)
        return sorted(eligible, key=state_key)[-1]


@dataclass(frozen=True)
class TotalShareEvent(object):
    symbol: str
    effective_at: str
    total_shares: float
    source_key: str
    raw_payload_sha256: str
    source: str = "eastmoney_balance_share_capital"
    report_period: str | None = None

    def __post_init__(self):
        _aware_datetime(self.effective_at)
        if not re.fullmatch(r"(sh|sz)\d{6}", self.symbol):
            raise ValueError("invalid total-share symbol")
        if not np.isfinite(self.total_shares) or self.total_shares <= 0:
            raise ValueError("total shares must be positive")
        if not self.source_key or not self.source:
            raise ValueError("total-share provenance is required")
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_payload_sha256):
            raise ValueError("invalid total-share payload hash")
        if self.report_period:
            date.fromisoformat(self.report_period)


class TotalShareStore(object):
    def __init__(self, events):
        self.events = tuple(
            event if isinstance(event, TotalShareEvent)
            else TotalShareEvent(**event) for event in events)

    def asof(self, symbol, asof_timestamp):
        asof = _aware_datetime(asof_timestamp)
        eligible = [event for event in self.events
                    if event.symbol == symbol and
                    _aware_datetime(event.effective_at) <= asof]
        if not eligible:
            return None
        def state_key(event):
            economic_date = (
                date.fromisoformat(event.report_period)
                if event.report_period else
                _aware_datetime(event.effective_at).date()
            )
            return (economic_date, _aware_datetime(event.effective_at),
                    event.source_key)
        return sorted(eligible, key=state_key)[-1]


def structured_balance_total_share_events(records, trading_sessions):
    """Build conservative total-share events from structured balance rows.

    A report known before the local calendar starts is usable at the first
    session; records after the last available session remain unavailable.
    Duplicate rows for the same report/effective time are consolidated, while
    later report periods remain available even when the numeric value is
    unchanged.  This preserves the economic timestamp used by as-of queries.
    """
    sessions = sorted(
        value if isinstance(value, date) else date.fromisoformat(str(value))
        for value in trading_sessions)
    if not sessions:
        raise ValueError("trading sessions are required")
    candidates = []
    excluded = {}
    for record in records:
        if record.get("raw_field") != "SHARE_CAPITAL":
            continue
        try:
            announced = _structured_date(
                record.get("announcement"), "announcement")
            total = float(record.get("raw_value")) * float(
                record.get("unit_scale", 1.0))
            if not np.isfinite(total) or total <= 0:
                raise ValueError("INVALID_TOTAL_SHARE_CAPITAL")
            if announced < sessions[0]:
                effective = datetime.combine(
                    sessions[0], time(9, 25), tzinfo=SHANGHAI_TZ)
            elif announced >= sessions[-1]:
                raise ValueError("NO_FUTURE_TRADING_SESSION")
            else:
                effective = conservative_available_at(
                    announced.isoformat(), sessions)
            candidates.append(TotalShareEvent(
                symbol=str(record["symbol"]),
                effective_at=effective.isoformat(), total_shares=total,
                source_key=str(record["source_key"]),
                raw_payload_sha256=str(record["raw_payload_sha256"]),
                report_period=str(record.get("report_period") or "") or None,
            ))
        except (KeyError, TypeError, ValueError) as error:
            reason = str(error)
            excluded[reason] = excluded.get(reason, 0) + 1
    latest_identity = {}
    for event in sorted(candidates, key=lambda item: (
            item.symbol, _aware_datetime(item.effective_at), item.source_key)):
        identity = (event.symbol, event.report_period, event.effective_at)
        latest_identity[identity] = event
    observations = sorted(latest_identity.values(), key=lambda item: (
        item.symbol, _aware_datetime(item.effective_at), item.source_key))
    return observations, {
        "input_records": len(records),
        "candidate_events": len(candidates),
        "state_transitions": len(observations),
        "excluded_records": sum(excluded.values()),
        "exclusion_reasons": dict(sorted(excluded.items())),
    }


def reconcile_share_capital(primary_event, balance_event,
                            daily_float_shares=None,
                            relative_tolerance=0.005):
    """Merge independently sourced total and float shares without imputation."""
    tolerance = float(relative_tolerance)
    if not np.isfinite(tolerance) or tolerance < 0:
        raise ValueError("invalid share reconciliation tolerance")
    total = None
    floated = None
    total_source = None
    float_source = None
    total_difference = None
    float_difference = None
    primary_total = None
    primary_float = None
    balance_total = None
    observed_daily_float = None
    conflict = False
    conflict_reasons = []
    if primary_event is not None:
        primary_total = float(primary_event.total_shares)
        primary_float = float(primary_event.float_shares)
        total = primary_total
        floated = primary_float
        total_source = "cninfo_share_change"
        float_source = "cninfo_share_change"
    if balance_event is not None:
        balance_total = float(balance_event.total_shares)
        if total is None:
            total = balance_total
            total_source = balance_event.source
        elif (not primary_event.event_date or
              not balance_event.report_period or
              date.fromisoformat(balance_event.report_period) >=
              date.fromisoformat(primary_event.event_date)):
            total_difference = abs(total - balance_total) / max(
                abs(total), abs(balance_total))
            if total_difference > tolerance:
                conflict = True
                conflict_reasons.append("TOTAL_PRIMARY_VS_BALANCE")
    try:
        price_float = float(daily_float_shares)
    except (TypeError, ValueError):
        price_float = np.nan
    if np.isfinite(price_float) and price_float > 0:
        observed_daily_float = price_float
        if floated is None:
            floated = price_float
            float_source = "daily_price_outstanding_share"
        else:
            float_difference = abs(floated - price_float) / max(
                abs(floated), abs(price_float))
            if float_difference > tolerance:
                conflict = True
                conflict_reasons.append("FLOAT_PRIMARY_VS_DAILY")
    missing = []
    if total is None:
        missing.append("MISSING_TOTAL_SHARES")
    if floated is None:
        missing.append("MISSING_FLOAT_SHARES")
    if total is not None and floated is not None and floated > total:
        conflict = True
        conflict_reasons.append("FLOAT_EXCEEDS_TOTAL")
    if missing:
        status = "MISSING"
    elif conflict:
        status = "CONFLICT"
    elif primary_event is not None and balance_event is not None:
        status = "MATCHED"
    elif primary_event is not None:
        status = "PRIMARY_ONLY"
    else:
        status = "STRUCTURED_FALLBACK"
    return {
        "total_shares": total,
        "float_shares": floated,
        "total_share_source": total_source,
        "float_share_source": float_source,
        "primary_total_shares": primary_total,
        "primary_float_shares": primary_float,
        "primary_event_date": (
            primary_event.event_date if primary_event else None),
        "primary_available_at": (
            primary_event.effective_at if primary_event else None),
        "balance_total_shares": balance_total,
        "balance_report_period": (
            balance_event.report_period if balance_event else None),
        "balance_available_at": (
            balance_event.effective_at if balance_event else None),
        "daily_float_shares_observed": observed_daily_float,
        "total_relative_difference": total_difference,
        "float_relative_difference": float_difference,
        "reconciliation_status": status,
        "conflict_reasons": sorted(set(conflict_reasons)),
        "missing_reason": ";".join(missing) if missing else None,
    }


def market_cap_snapshot(raw_close, share_event):
    if share_event is None:
        return {"total_market_cap": None, "float_market_cap": None,
                "missing_reason": "MISSING_SHARE_CAPITAL"}
    price = float(raw_close)
    if not np.isfinite(price) or price <= 0:
        raise ValueError("raw close must be positive")
    return {
        "total_market_cap": price * share_event.total_shares,
        "float_market_cap": price * share_event.float_shares,
        "missing_reason": None,
    }


UNIVERSE_REASON_CODES = (
    "BASE_INELIGIBLE", "MISSING_REQUIRED_THEME",
    "MISSING_TOTAL_MARKET_CAP", "SMALL_CAP_BOTTOM_30",
    "OUTSIDE_TOTAL_MARKET_CAP_TOP_50",
)


class FundamentalPanelSidecar(object):
    """Lazy date-partitioned fundamental panel to avoid dense history copies."""

    def __init__(self, dates, symbols, partition_root):
        self.dates = tuple(int(value) for value in dates)
        self.symbols = tuple(str(value) for value in symbols)
        self.symbol_index = {symbol: index for index, symbol in enumerate(
            self.symbols)}
        self.partition_root = Path(partition_root)
        self._cached_date = None
        self._cached_frame = None

    def snapshot(self, day):
        date_value = self.dates[int(day)]
        if self._cached_date == date_value:
            return self._cached_frame.copy()
        path = self.partition_root / (str(date_value) + ".jsonl")
        if not path.is_file():
            frame = pd.DataFrame({"symbol": self.symbols})
            frame["missing_reason"] = "MISSING_FUNDAMENTAL_PARTITION"
        else:
            records = [json.loads(line) for line in path.read_text(
                encoding="utf-8").splitlines() if line.strip()]
            frame = pd.DataFrame(records)
            if "symbol" not in frame:
                raise ValueError("fundamental sidecar partition requires symbol")
            if frame.symbol.duplicated().any():
                raise ValueError("duplicate symbol in fundamental partition")
            frame = pd.DataFrame({"symbol": self.symbols}).merge(
                frame, on="symbol", how="left", validate="one_to_one")
            if "missing_reason" not in frame:
                frame["missing_reason"] = None
            missing = frame.drop(columns=["symbol", "missing_reason"],
                                 errors="ignore").isna().all(axis=1)
            frame.loc[missing & frame.missing_reason.isna(), "missing_reason"] = \
                "MISSING_FUNDAMENTAL_SYMBOL"
        self._cached_date = date_value
        self._cached_frame = frame
        return frame.copy()


class FrozenUniverseBuilder(object):
    """Deterministic daily v4 size universes with explicit reason codes."""

    def __init__(self, symbols, small_cap_exclusion_fraction=.30,
                 large_mid_fraction=.50):
        self.symbols = np.asarray(symbols, dtype=object)
        if not 0 <= small_cap_exclusion_fraction < 1:
            raise ValueError("small-cap exclusion fraction is invalid")
        if not 0 < large_mid_fraction <= 1:
            raise ValueError("large-mid fraction is invalid")
        if large_mid_fraction > 1 - small_cap_exclusion_fraction:
            raise ValueError("large-mid universe must be inside primary")
        self.small_cap_exclusion_fraction = float(
            small_cap_exclusion_fraction)
        self.large_mid_fraction = float(large_mid_fraction)

    def build(self, base_eligible, total_market_cap, required_theme_available):
        base = np.asarray(base_eligible, dtype=bool)
        cap = np.asarray(total_market_cap, dtype=float)
        themes = np.asarray(required_theme_available, dtype=bool)
        if any(value.shape != self.symbols.shape for value in
               (base, cap, themes)):
            raise ValueError("universe inputs have wrong shape")
        valid_cap = np.isfinite(cap) & (cap > 0)
        all_eligible = base & themes & valid_cap
        eligible_indices = np.flatnonzero(all_eligible)
        ascending = sorted(
            eligible_indices.tolist(), key=lambda index: (
                cap[index], str(self.symbols[index])))

        primary = all_eligible.copy()
        excluded_count = int(np.floor(
            len(ascending) * self.small_cap_exclusion_fraction))
        primary[ascending[:excluded_count]] = False

        large_mid = np.zeros(self.symbols.shape, dtype=bool)
        keep_count = int(np.ceil(len(ascending) * self.large_mid_fraction))
        descending = sorted(
            eligible_indices.tolist(), key=lambda index: (
                -cap[index], str(self.symbols[index])))
        large_mid[descending[:keep_count]] = True
        if np.any(large_mid & ~primary):
            raise AssertionError("large-mid universe escaped primary universe")

        base_reasons = [[] for _ in self.symbols]
        for index in range(len(self.symbols)):
            if not base[index]:
                base_reasons[index].append("BASE_INELIGIBLE")
            if not themes[index]:
                base_reasons[index].append("MISSING_REQUIRED_THEME")
            if not valid_cap[index]:
                base_reasons[index].append("MISSING_TOTAL_MARKET_CAP")
        primary_reasons = [list(values) for values in base_reasons]
        for index in ascending[:excluded_count]:
            primary_reasons[index].append("SMALL_CAP_BOTTOM_30")
        large_reasons = [list(values) for values in base_reasons]
        for index in eligible_indices:
            if not large_mid[index]:
                large_reasons[index].append(
                    "OUTSIDE_TOTAL_MARKET_CAP_TOP_50")
        return {
            "all_eligible_sensitivity": all_eligible,
            "size_controlled_primary": primary,
            "large_mid_sensitivity": large_mid,
            "reasons": {
                "all_eligible_sensitivity": tuple(
                    tuple(value) for value in base_reasons),
                "size_controlled_primary": tuple(
                    tuple(value) for value in primary_reasons),
                "large_mid_sensitivity": tuple(
                    tuple(value) for value in large_reasons),
            },
        }


def build_fundamental_coverage_report(
        frame, themes, lifecycle_st_threshold=.995,
        theme_median_threshold=.80, theme_p10_threshold=.70,
        market_cap_threshold=.995, shanghai_st_complete=False,
        reconciliation_passed=False, pit_tests_passed=False):
    """Evaluate Spec 4.4 gates without filling any missing observation."""
    required = {
        "date", "symbol", "exchange", "industry", "universe_eligible",
        "st_known", "total_market_cap", "float_market_cap", "source",
        "is_restated", *themes,
    }
    if not required.issubset(frame.columns):
        raise ValueError("fundamental coverage frame fields mismatch")
    data = frame.copy()
    denominator = data.universe_eligible.astype(bool)
    if not denominator.any():
        raise ValueError("coverage audit has no eligible security-days")

    def ratio(mask, group=None):
        values = data.loc[denominator].copy()
        values["covered"] = np.asarray(mask)[denominator.to_numpy()]
        if group is None:
            return float(values.covered.mean())
        return values.groupby(group, dropna=False).covered.mean()

    daily_rows = []
    for date_value, group in data.loc[denominator].groupby("date", sort=True):
        row = {"date": int(date_value), "eligible": int(len(group)),
               "st_known_coverage": float(group.st_known.mean()),
               "total_market_cap_coverage": float(
                   group.total_market_cap.notna().mean()),
               "float_market_cap_coverage": float(
                   group.float_market_cap.notna().mean())}
        for theme in themes:
            row[theme + "_coverage"] = float(group[theme].notna().mean())
        daily_rows.append(row)
    daily = pd.DataFrame(daily_rows)
    theme_summary = {}
    blockers = []
    for theme in themes:
        series = daily[theme + "_coverage"]
        median = float(series.median())
        p10 = float(series.quantile(.10))
        passed = median >= theme_median_threshold and p10 >= theme_p10_threshold
        theme_summary[theme] = {"median": median, "p10": p10,
                                "passed": bool(passed)}
        if not passed:
            blockers.append("theme_coverage:" + theme)
    st_ratio = ratio(data.st_known.astype(bool))
    total_cap_ratio = ratio(data.total_market_cap.notna())
    float_cap_ratio = ratio(data.float_market_cap.notna())
    if st_ratio < lifecycle_st_threshold:
        blockers.append("lifecycle_st_coverage")
    if not shanghai_st_complete:
        blockers.append("shanghai_historical_st_incomplete")
    if total_cap_ratio < market_cap_threshold or \
            float_cap_ratio < market_cap_threshold:
        blockers.append("market_cap_coverage")
    if not reconciliation_passed:
        blockers.append("share_market_cap_reconciliation")
    if not pit_tests_passed:
        blockers.append("pit_boundary_revision_ttm_tests")

    exchange = ratio(data[themes[0]].notna(), "exchange").to_dict()
    industry = ratio(data[themes[0]].notna(), "industry").to_dict()
    sources = data.loc[denominator, "source"].fillna("missing").value_counts(
        normalize=True).sort_index().to_dict()
    delisted = data.loc[denominator & data.symbol.astype(str).str.contains(
        "__delisted__", regex=False)]
    return {
        "daily": daily.to_dict("records"),
        "theme_coverage": theme_summary,
        "exchange_first_theme_coverage": exchange,
        "industry_first_theme_coverage": industry,
        "source_share": sources,
        "st_known_coverage": st_ratio,
        "total_market_cap_coverage": total_cap_ratio,
        "float_market_cap_coverage": float_cap_ratio,
        "restated_fact_rows": int(data.loc[denominator, "is_restated"].sum()),
        "delisted_eligible_rows": int(len(delisted)),
        "blockers": sorted(set(blockers)),
        "gate_status": "PASSED" if not blockers else "BLOCKED_DATA_GATE",
    }
