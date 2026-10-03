# -*- encoding: utf-8 -*-
"""Point-in-time price-limit reference records and sidecar loading.

The raw OHLC cache deliberately does not imply a price-limit reference from
the previous observed close.  A reference is usable only when its source and
availability are explicit in this sidecar.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np
import pandas as pd


LIMIT_REFERENCE_SCHEMA_VERSION = "limit_reference_v1"
REFERENCE_REASON_CODES = (
    "UNKNOWN_REFERENCE_PRICE",
    "DIRECT_EXCHANGE_LIMIT",
    "PROVIDER_PRE_CLOSE",
    "CORPORATE_ACTION_RECONSTRUCTION",
    "REFERENCE_SOURCE_CONFLICT",
)


def _positive_or_none(value):
    if value is None or pd.isna(value):
        return None
    value = float(value)
    return value if np.isfinite(value) and value > 0 else None


def _reason_tuple(value) -> tuple[str, ...]:
    if value is None or (not isinstance(value, (tuple, list, set)) and pd.isna(value)):
        return ()
    if isinstance(value, str):
        return tuple(item for item in value.split("|") if item)
    return tuple(str(item) for item in value)


@dataclass(frozen=True)
class LimitReference:
    trade_date: int
    symbol: str
    previous_raw_close: float | None = None
    limit_reference_price_raw: float | None = None
    exchange_upper_limit_raw: float | None = None
    exchange_lower_limit_raw: float | None = None
    source: str = "unknown"
    quality: str = "unknown"
    effective_at: str = ""
    available_at: str = ""
    availability_evidence: str = ""
    reason_codes: tuple[str, ...] = ("UNKNOWN_REFERENCE_PRICE",)
    schema_version: str = LIMIT_REFERENCE_SCHEMA_VERSION

    def __post_init__(self):
        if int(self.trade_date) <= 0 or not str(self.symbol):
            raise ValueError("trade_date and symbol are required")
        if self.schema_version != LIMIT_REFERENCE_SCHEMA_VERSION:
            raise ValueError("unsupported limit-reference schema")
        known_reference = _positive_or_none(self.limit_reference_price_raw)
        direct_upper = _positive_or_none(self.exchange_upper_limit_raw)
        direct_lower = _positive_or_none(self.exchange_lower_limit_raw)
        if self.quality == "known" and known_reference is None and \
                direct_upper is None and direct_lower is None:
            raise ValueError("known record requires a reference or direct limits")
        if self.quality not in ("known", "reconstructed", "conflict", "unknown"):
            raise ValueError("invalid reference quality")

    @property
    def has_reference(self):
        return _positive_or_none(self.limit_reference_price_raw) is not None

    @property
    def has_direct_limits(self):
        return (_positive_or_none(self.exchange_upper_limit_raw) is not None and
                _positive_or_none(self.exchange_lower_limit_raw) is not None)

    def to_record(self):
        record = asdict(self)
        record["reason_codes"] = "|".join(self.reason_codes)
        return record

    @classmethod
    def unknown(cls, trade_date, symbol, previous_raw_close=None,
                reason_codes=("UNKNOWN_REFERENCE_PRICE",)):
        return cls(
            trade_date=int(trade_date), symbol=str(symbol),
            previous_raw_close=_positive_or_none(previous_raw_close),
            source="unknown", quality="unknown",
            reason_codes=_reason_tuple(reason_codes),
        )

    @classmethod
    def from_mapping(cls, row: Mapping):
        payload = dict(row)
        payload["trade_date"] = int(payload["trade_date"])
        payload["symbol"] = str(payload["symbol"])
        for field in ("previous_raw_close", "limit_reference_price_raw",
                      "exchange_upper_limit_raw", "exchange_lower_limit_raw"):
            payload[field] = _positive_or_none(payload.get(field))
        payload["reason_codes"] = _reason_tuple(payload.get("reason_codes"))
        for field in ("source", "quality", "effective_at", "available_at",
                      "availability_evidence", "schema_version"):
            if field in payload and pd.isna(payload[field]):
                payload[field] = ""
        payload.setdefault("schema_version", LIMIT_REFERENCE_SCHEMA_VERSION)
        return cls(**payload)


def reconstruct_corporate_action_reference(previous_raw_close, cash_per_share=0.0,
                                           stock_per_share=0.0,
                                           rights_per_share=0.0,
                                           rights_price=0.0):
    """Apply the exchange ex-right/ex-dividend reference formula.

    Values must already be expressed per share.  The caller remains
    responsible for proving that the corporate-action record is complete.
    """
    previous = _positive_or_none(previous_raw_close)
    if previous is None:
        return None
    cash = float(cash_per_share or 0.0)
    stock = float(stock_per_share or 0.0)
    rights = float(rights_per_share or 0.0)
    rights_price = float(rights_price or 0.0)
    denominator = 1.0 + stock + rights
    numerator = previous - cash + rights * rights_price
    if denominator <= 0 or numerator <= 0:
        return None
    return numerator / denominator


def reference_from_provider_row(trade_date, symbol, row: Mapping, *,
                                source, available_at,
                                availability_evidence="") -> LimitReference:
    """Normalize an explicitly supplied provider reference or direct limits."""
    previous = _positive_or_none(row.get("previous_raw_close"))
    reference = _positive_or_none(
        row.get("limit_reference_price_raw", row.get("pre_close"))
    )
    upper = _positive_or_none(
        row.get("exchange_upper_limit_raw", row.get("upper_limit"))
    )
    lower = _positive_or_none(
        row.get("exchange_lower_limit_raw", row.get("lower_limit"))
    )
    reasons = []
    if upper is not None and lower is not None:
        reasons.append("DIRECT_EXCHANGE_LIMIT")
    if reference is not None:
        reasons.append("PROVIDER_PRE_CLOSE")
    if not reasons:
        return LimitReference.unknown(trade_date, symbol, previous)
    return LimitReference(
        trade_date=int(trade_date), symbol=str(symbol),
        previous_raw_close=previous,
        limit_reference_price_raw=reference,
        exchange_upper_limit_raw=upper,
        exchange_lower_limit_raw=lower,
        source=str(source), quality="known",
        effective_at=str(trade_date), available_at=str(available_at),
        availability_evidence=str(availability_evidence),
        reason_codes=tuple(reasons),
    )


class LimitReferenceStore(object):
    """Immutable lookup facade for a versioned limit-reference sidecar."""

    REQUIRED_COLUMNS = (
        "trade_date", "symbol", "previous_raw_close",
        "limit_reference_price_raw", "exchange_upper_limit_raw",
        "exchange_lower_limit_raw", "source", "quality", "effective_at",
        "available_at", "availability_evidence", "reason_codes",
        "schema_version",
    )

    def __init__(self, records: Iterable[LimitReference] = ()):
        self._records = {}
        for record in records:
            key = (int(record.trade_date), str(record.symbol))
            if key in self._records:
                raise ValueError("duplicate limit reference: {}".format(key))
            self._records[key] = record

    def get(self, trade_date, symbol, *, as_of=None):
        record = self._records.get((int(trade_date), str(symbol)))
        if record is None:
            return LimitReference.unknown(trade_date, symbol)
        if as_of and record.available_at:
            available = pd.Timestamp(record.available_at)
            requested = pd.Timestamp(as_of)
            if available.tzinfo is None and requested.tzinfo is not None:
                available = available.tz_localize(requested.tzinfo)
            elif available.tzinfo is not None and requested.tzinfo is None:
                requested = requested.tz_localize(available.tzinfo)
            if available > requested:
                return LimitReference.unknown(
                    trade_date, symbol, record.previous_raw_close,
                    ("UNKNOWN_REFERENCE_PRICE",),
                )
        return record

    def to_frame(self):
        rows = [item.to_record() for item in self._records.values()]
        return pd.DataFrame(rows, columns=self.REQUIRED_COLUMNS).sort_values(
            ["trade_date", "symbol"], ignore_index=True
        ) if rows else pd.DataFrame(columns=self.REQUIRED_COLUMNS)

    def write(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.to_frame().to_csv(path, index=False)

    @classmethod
    def read(cls, path):
        frame = pd.read_csv(path, dtype={"symbol": str})
        missing = set(cls.REQUIRED_COLUMNS) - set(frame.columns)
        if missing:
            raise ValueError("limit reference missing columns: {}".format(
                ", ".join(sorted(missing))))
        return cls(LimitReference.from_mapping(row)
                   for row in frame[list(cls.REQUIRED_COLUMNS)].to_dict("records"))

