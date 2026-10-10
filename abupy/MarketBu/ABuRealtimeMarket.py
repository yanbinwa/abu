# -*- encoding: utf-8 -*-
"""Normalized real-time quote and minute-bar adapters.

The first implementation deliberately uses AKShare because it is already a
frozen dependency of this project.  AKShare exposes HTTP snapshots rather than
an exchange-grade streaming feed, so ``poll_snapshots`` is named and documented
as polling.  Strategy code consumes the normalized contract and can later swap
in a push provider without depending on AKShare column names.
"""
from __future__ import annotations

import threading
import time
from abc import ABCMeta, abstractmethod
from dataclasses import asdict, dataclass
from datetime import timedelta

import numpy as np
import pandas as pd
import requests


SHANGHAI_TZ = "Asia/Shanghai"
SNAPSHOT_COLUMNS = (
    "symbol", "code", "name", "provider_time", "received_at",
    "timestamp_quality", "last", "open", "high", "low", "pre_close",
    "volume", "amount", "change_pct", "turnover_rate", "bid1", "ask1",
    "quote_valid", "source",
)
MINUTE_BAR_COLUMNS = (
    "symbol", "timestamp", "received_at", "interval_minutes", "open",
    "high", "low", "close", "volume", "amount", "average",
    "change_pct", "turnover_rate", "bar_complete", "source",
    "source_timestamp", "bar_start", "bar_end", "request_started_at",
    "available_at", "open_raw", "high_raw", "low_raw", "close_raw",
    "volume_shares", "amount_raw", "revision", "is_complete",
    "quality_codes",
)

MINUTE_TIMESTAMP_SEMANTICS = {
    "akshare_eastmoney_minute": "bar_end",
    "akshare_sina_minute": "bar_end",
    "tencent_mkline_minute": "bar_end",
}


class RealtimeMarketDataError(RuntimeError):
    """Raised when a real-time provider cannot return a trustworthy result."""


def _market_data_error(message, reason_code="PROVIDER_ERROR"):
    error = RealtimeMarketDataError(message)
    error.reason_code = reason_code
    return error


@dataclass(frozen=True)
class MarketDataHealth:
    source: str
    connected: bool
    last_provider: str | None
    last_success_at: str | None
    last_error: str | None
    last_warning: str | None
    consecutive_failures: int
    last_latency_ms: float | None
    last_record_count: int
    transport_ok: bool = False
    data_present: bool = False
    data_fresh: bool = False
    fields_valid: bool = False
    reason_codes: tuple[str, ...] = ()

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class MinuteBarEvent:
    """Provider-neutral completed or in-progress minute bar event."""
    symbol: str
    interval_minutes: int
    source_timestamp: str
    bar_start: str
    bar_end: str
    request_started_at: str
    received_at: str
    available_at: str
    open_raw: float
    high_raw: float
    low_raw: float
    close_raw: float
    volume_shares: float
    amount_raw: float | None
    source: str
    revision: int = 1
    is_complete: bool = True
    quality_codes: tuple[str, ...] = ()

    def __post_init__(self):
        if self.interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        timestamps = [
            _as_shanghai_timestamp(value) for value in (
                self.source_timestamp, self.bar_start, self.bar_end,
                self.request_started_at, self.received_at, self.available_at,
            )
        ]
        _, bar_start, bar_end, request_started, received, available = timestamps
        if not bar_start < bar_end:
            raise ValueError("bar_start must be before bar_end")
        if request_started > received or received > available:
            raise ValueError("request/receive/available timestamps are reversed")
        values = (self.open_raw, self.high_raw, self.low_raw, self.close_raw)
        if not all(np.isfinite(value) and value > 0 for value in values):
            raise ValueError("OHLC must be finite and positive")
        if self.low_raw > min(self.open_raw, self.close_raw) or \
                self.high_raw < max(self.open_raw, self.close_raw) or \
                self.low_raw > self.high_raw:
            raise ValueError("invalid OHLC envelope")
        if not np.isfinite(self.volume_shares) or self.volume_shares < 0:
            raise ValueError("volume_shares must be finite and non-negative")
        if self.amount_raw is not None and (
                not np.isfinite(self.amount_raw) or self.amount_raw < 0):
            raise ValueError("amount_raw must be non-negative when present")

    def to_dict(self):
        return asdict(self)


def normalize_cn_symbol(value):
    """Return an exchange-prefixed lower-case A-share symbol."""
    text = str(value).strip().lower()
    if not text:
        raise ValueError("empty security symbol")
    if "." in text:
        left, right = text.split(".", 1)
        if right in ("sh", "sz", "bj") and left.isdigit():
            text = right + left.zfill(6)
    if text[:2] in ("sh", "sz", "bj") and text[2:].isdigit():
        code = text[2:].zfill(6)
        return text[:2] + code
    if not text.isdigit() or len(text) > 6:
        raise ValueError("unsupported A-share symbol: {}".format(value))
    code = text.zfill(6)
    if code.startswith(("4", "8", "92")):
        exchange = "bj"
    elif code.startswith(("5", "6", "9")):
        exchange = "sh"
    else:
        exchange = "sz"
    return exchange + code


def _as_shanghai_timestamp(value):
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(SHANGHAI_TZ)
    return timestamp.tz_convert(SHANGHAI_TZ)


def _provider_timestamp(value):
    if value is None or pd.isna(value):
        return pd.NaT
    try:
        if isinstance(value, (int, float, np.integer, np.floating)):
            unit = "ms" if float(value) > 10_000_000_000 else "s"
            return pd.Timestamp(value, unit=unit, tz="UTC").tz_convert(SHANGHAI_TZ)
        return _as_shanghai_timestamp(value)
    except (TypeError, ValueError, OverflowError):
        return pd.NaT


class RealtimeMarketDataAdapter(object, metaclass=ABCMeta):
    """Provider-neutral synchronous market-data contract."""

    def __init__(self, source, stale_after_seconds=30.0, now=None):
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.source = str(source)
        self.stale_after_seconds = float(stale_after_seconds)
        self._now = now or (lambda: pd.Timestamp.now(tz=SHANGHAI_TZ))
        self._health_lock = threading.Lock()
        self._last_provider = None
        self._last_success_at = None
        self._last_error = None
        self._last_warning = None
        self._consecutive_failures = 0
        self._last_latency_ms = None
        self._last_record_count = 0
        self._transport_ok = False
        self._data_present = False
        self._data_fresh = False
        self._fields_valid = False
        self._reason_codes = ()
        self._symbol_health = {}

    @abstractmethod
    def snapshot(self, symbols=None, strict=True):
        """Return normalized current quotes."""

    @abstractmethod
    def minute_bars(self, symbol, period="1", start=None, end=None, adjust=""):
        """Return normalized historical/current minute bars."""

    def _success(self, provider, started, count, warning=None,
                 data_fresh=True, fields_valid=True, reason_codes=()):
        with self._health_lock:
            self._last_provider = provider
            self._last_success_at = _as_shanghai_timestamp(self._now())
            self._last_error = None
            self._last_warning = warning
            self._consecutive_failures = 0
            self._last_latency_ms = round((time.monotonic() - started) * 1000, 3)
            self._last_record_count = int(count)
            self._transport_ok = True
            self._data_present = bool(count)
            self._data_fresh = bool(data_fresh and count)
            self._fields_valid = bool(fields_valid and count)
            self._reason_codes = tuple(reason_codes)

    def _failure(self, error, started):
        with self._health_lock:
            self._last_error = "{}: {}".format(type(error).__name__, error)
            self._last_warning = None
            self._consecutive_failures += 1
            self._last_latency_ms = round((time.monotonic() - started) * 1000, 3)
            self._transport_ok = not isinstance(error, (OSError, TimeoutError))
            self._data_present = False
            self._data_fresh = False
            self._fields_valid = False
            code = getattr(error, "reason_code", None)
            self._reason_codes = (code or "PROVIDER_ERROR",)

    def health(self, symbol=None):
        if symbol is not None:
            normalized = normalize_cn_symbol(symbol)
            with self._health_lock:
                return self._symbol_health.get(normalized)
        now = _as_shanghai_timestamp(self._now())
        with self._health_lock:
            age = ((now - self._last_success_at).total_seconds()
                   if self._last_success_at is not None else np.inf)
            return MarketDataHealth(
                source=self.source,
                connected=(self._consecutive_failures == 0 and
                           age <= self.stale_after_seconds),
                last_provider=self._last_provider,
                last_success_at=(self._last_success_at.isoformat()
                                 if self._last_success_at is not None else None),
                last_error=self._last_error,
                last_warning=self._last_warning,
                consecutive_failures=self._consecutive_failures,
                last_latency_ms=self._last_latency_ms,
                last_record_count=self._last_record_count,
                transport_ok=self._transport_ok,
                data_present=self._data_present,
                data_fresh=self._data_fresh,
                fields_valid=self._fields_valid,
                reason_codes=self._reason_codes,
            )

    def poll_snapshots(self, callback, symbols=None, interval_seconds=5.0,
                       stop_event=None, max_polls=None, strict=True):
        """Poll snapshots and synchronously pass each frame to ``callback``."""
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if max_polls is not None and max_polls <= 0:
            raise ValueError("max_polls must be positive")
        stop_event = stop_event or threading.Event()
        polls = 0
        while not stop_event.is_set():
            started = time.monotonic()
            callback(self.snapshot(symbols=symbols, strict=strict))
            polls += 1
            if max_polls is not None and polls >= max_polls:
                break
            remaining = max(0.0, interval_seconds - (time.monotonic() - started))
            if stop_event.wait(remaining):
                break
        return polls


class AKShareRealtimeMarketData(RealtimeMarketDataAdapter):
    """AKShare-backed A-share snapshots and minute bars.

    This adapter polls Eastmoney and optionally falls back to Sina for market
    snapshots and minute bars.  It is intended for research, shadow operation
    and minute-level execution studies, not latency-sensitive or
    exchange-certified use.
    """

    PERIODS = frozenset(("1", "5", "15", "30", "60"))

    def __init__(self, ak_module=None, retries=3, retry_wait=1.0,
                 fallback=True, stale_after_seconds=30.0, now=None,
                 spot_volume_multiplier=100.0, minute_volume_multiplier=100.0,
                 raw_archive=None):
        super(AKShareRealtimeMarketData, self).__init__(
            source="akshare", stale_after_seconds=stale_after_seconds, now=now)
        if retries <= 0 or retry_wait < 0:
            raise ValueError("invalid retry configuration")
        self._ak_module = ak_module
        self.retries = int(retries)
        self.retry_wait = float(retry_wait)
        self.fallback = bool(fallback)
        self.spot_volume_multiplier = float(spot_volume_multiplier)
        self.minute_volume_multiplier = float(minute_volume_multiplier)
        self.raw_archive = raw_archive

    @property
    def ak(self):
        if self._ak_module is None:
            try:
                import akshare as ak
            except ImportError as error:
                raise ImportError("AKShare real-time adapter requires akshare") from error
            self._ak_module = ak
        return self._ak_module

    def _request(self, call):
        last_error = None
        for attempt in range(self.retries):
            try:
                return call()
            except Exception as error:  # Provider exceptions vary by release.
                last_error = error
                if attempt + 1 < self.retries:
                    time.sleep(self.retry_wait * (2 ** attempt))
        raise last_error

    @staticmethod
    def _normalize_snapshot(raw, provider, received_at, volume_multiplier):
        if raw is None or raw.empty:
            raise RealtimeMarketDataError("{} returned an empty snapshot".format(provider))
        aliases = {
            "代码": "code", "名称": "name", "最新价": "last",
            "今开": "open", "最高": "high", "最低": "low",
            "昨收": "pre_close", "成交量": "volume", "成交额": "amount",
            "涨跌幅": "change_pct", "换手率": "turnover_pct",
            "买入": "bid1", "卖出": "ask1", "时间戳": "provider_time",
        }
        frame = raw.rename(columns=aliases).copy()
        required = ("code", "name", "last", "open", "high", "low",
                    "pre_close", "volume", "amount")
        missing = [column for column in required if column not in frame]
        if missing:
            raise RealtimeMarketDataError(
                "{} snapshot missing fields: {}".format(provider, ", ".join(missing)))
        for column in ("change_pct", "turnover_pct", "bid1", "ask1"):
            if column not in frame:
                frame[column] = np.nan
        numeric = ("last", "open", "high", "low", "pre_close", "volume",
                   "amount", "change_pct", "turnover_pct", "bid1", "ask1")
        for column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame["code"] = frame.code.astype(str).str.extract(r"(\d+)", expand=False).str.zfill(6)
        frame.dropna(subset=["code"], inplace=True)
        frame["symbol"] = frame.code.map(normalize_cn_symbol)
        frame["volume"] = frame.volume * float(volume_multiplier)
        frame["turnover_rate"] = frame.turnover_pct / 100.0
        if "provider_time" in frame:
            provider_times = frame.provider_time.map(_provider_timestamp)
            quality = np.where(provider_times.notna(), "provider", "received_at_proxy")
            frame["provider_time"] = provider_times.where(
                provider_times.notna(), received_at)
        else:
            frame["provider_time"] = received_at
            quality = np.full(len(frame), "received_at_proxy", dtype=object)
        frame["received_at"] = received_at
        frame["timestamp_quality"] = quality
        frame["quote_valid"] = (
            frame["last"].gt(0) & frame["pre_close"].gt(0) &
            frame["high"].ge(frame[["open", "low", "last"]].max(axis=1)) &
            frame["low"].le(frame[["open", "high", "last"]].min(axis=1))
        )
        frame["source"] = provider
        frame = frame[list(SNAPSHOT_COLUMNS)]
        frame.sort_values("symbol", inplace=True)
        frame.drop_duplicates("symbol", keep="last", inplace=True)
        return frame.reset_index(drop=True)

    def snapshot(self, symbols=None, strict=True):
        started = time.monotonic()
        warning = None
        try:
            try:
                raw = self._request(self.ak.stock_zh_a_spot_em)
                provider = "akshare_eastmoney"
                multiplier = self.spot_volume_multiplier
            except Exception as primary_error:
                if not self.fallback:
                    raise
                warning = "eastmoney_failed: {}".format(type(primary_error).__name__)
                raw = self._request(self.ak.stock_zh_a_spot)
                provider = "akshare_sina"
                multiplier = 1.0
            received_at = _as_shanghai_timestamp(self._now())
            frame = self._normalize_snapshot(
                raw, provider, received_at, volume_multiplier=multiplier)
            if symbols is not None:
                requested = {normalize_cn_symbol(item) for item in symbols}
                frame = frame[frame.symbol.isin(requested)].reset_index(drop=True)
                missing = sorted(requested - set(frame.symbol))
                if strict and missing:
                    raise RealtimeMarketDataError(
                        "snapshot missing requested symbols: {}".format(", ".join(missing)))
            self._success(provider, started, len(frame), warning=warning)
            return frame
        except Exception as error:
            self._failure(error, started)
            if isinstance(error, RealtimeMarketDataError):
                raise
            raise RealtimeMarketDataError("AKShare snapshot failed") from error

    @staticmethod
    def _normalize_minute_bars(raw, symbol, period, request_started_at,
                               received_at, volume_multiplier, provider):
        if raw is None or raw.empty:
            raise RealtimeMarketDataError("minute-bar response is empty")
        aliases = {
            "时间": "timestamp", "开盘": "open", "收盘": "close",
            "最高": "high", "最低": "low", "成交量": "volume",
            "成交额": "amount", "均价": "average", "涨跌幅": "change_pct",
            "换手率": "turnover_pct", "day": "timestamp",
        }
        frame = raw.rename(columns=aliases).copy()
        required = ("timestamp", "open", "close", "high", "low", "volume")
        missing = [column for column in required if column not in frame]
        if missing:
            raise RealtimeMarketDataError(
                "minute bars missing fields: {}".format(", ".join(missing)))
        for column in ("amount", "average", "change_pct", "turnover_pct"):
            if column not in frame:
                frame[column] = np.nan
        parsed = pd.to_datetime(frame.timestamp, errors="coerce")
        if getattr(parsed.dt, "tz", None) is None:
            parsed = parsed.dt.tz_localize(SHANGHAI_TZ)
        else:
            parsed = parsed.dt.tz_convert(SHANGHAI_TZ)
        frame["timestamp"] = parsed
        for column in ("open", "close", "high", "low", "volume", "amount",
                       "average", "change_pct", "turnover_pct"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.dropna(subset=["timestamp", "open", "close", "high", "low"], inplace=True)
        frame["symbol"] = normalize_cn_symbol(symbol)
        frame["received_at"] = received_at
        frame["interval_minutes"] = int(period)
        frame["volume"] = frame.volume * float(volume_multiplier)
        frame["turnover_rate"] = frame.turnover_pct / 100.0
        semantics = MINUTE_TIMESTAMP_SEMANTICS.get(provider)
        if semantics != "bar_end":
            raise RealtimeMarketDataError(
                "unknown minute timestamp semantics for {}".format(provider))
        frame["source_timestamp"] = frame.timestamp
        frame["bar_end"] = frame.timestamp
        frame["bar_start"] = frame.bar_end - pd.to_timedelta(
            int(period), unit="min")
        frame["request_started_at"] = request_started_at
        frame["available_at"] = received_at
        frame["bar_complete"] = frame.bar_end.le(received_at.floor("min"))
        frame["source"] = provider
        frame["open_raw"] = frame.open
        frame["high_raw"] = frame.high
        frame["low_raw"] = frame.low
        frame["close_raw"] = frame.close
        frame["volume_shares"] = frame.volume
        frame["amount_raw"] = frame.amount
        frame["revision"] = 1
        frame["is_complete"] = frame.bar_complete
        frame["quality_codes"] = frame.apply(
            lambda row: tuple(
                code for code, present in (
                    ("AMOUNT_MISSING", pd.isna(row.amount_raw)),
                    ("BAR_INCOMPLETE", not bool(row.is_complete)),
                ) if present
            ), axis=1)
        for row in frame.itertuples(index=False):
            MinuteBarEvent(
                symbol=row.symbol, interval_minutes=row.interval_minutes,
                source_timestamp=row.source_timestamp.isoformat(),
                bar_start=row.bar_start.isoformat(),
                bar_end=row.bar_end.isoformat(),
                request_started_at=row.request_started_at.isoformat(),
                received_at=row.received_at.isoformat(),
                available_at=row.available_at.isoformat(),
                open_raw=row.open_raw, high_raw=row.high_raw,
                low_raw=row.low_raw, close_raw=row.close_raw,
                volume_shares=row.volume_shares,
                amount_raw=(None if pd.isna(row.amount_raw) else row.amount_raw),
                source=row.source, revision=row.revision,
                is_complete=row.is_complete,
                quality_codes=row.quality_codes,
            )
        frame = frame[list(MINUTE_BAR_COLUMNS)]
        frame.sort_values("timestamp", inplace=True)
        frame.drop_duplicates("timestamp", keep="last", inplace=True)
        return frame.reset_index(drop=True)

    def minute_bars(self, symbol, period="1", start=None, end=None, adjust=""):
        period = str(period)
        if period not in self.PERIODS:
            raise ValueError("period must be one of {}".format(sorted(self.PERIODS)))
        if adjust not in ("", "qfq", "hfq"):
            raise ValueError("adjust must be '', qfq or hfq")
        normalized_symbol = normalize_cn_symbol(symbol)
        now = _as_shanghai_timestamp(self._now())
        if end is None:
            end = now.strftime("%Y-%m-%d %H:%M:%S")
        if start is None:
            days = 5 if period == "1" else 120
            start = (now - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        started = time.monotonic()
        request_started_at = _as_shanghai_timestamp(self._now())
        warning = None
        try:
            try:
                raw = self._request(lambda: self.ak.stock_zh_a_hist_min_em(
                    symbol=normalized_symbol[2:], start_date=str(start),
                    end_date=str(end), period=period, adjust=adjust))
                provider = "akshare_eastmoney_minute"
                received_at = _as_shanghai_timestamp(self._now())
                if self.raw_archive is not None:
                    self.raw_archive(
                        raw=raw, provider=provider, symbol=normalized_symbol,
                        interval_minutes=int(period),
                        request_started_at=request_started_at,
                        received_at=received_at)
                frame = self._normalize_minute_bars(
                    raw, normalized_symbol, period, request_started_at,
                    received_at,
                    volume_multiplier=self.minute_volume_multiplier,
                    provider=provider)
            except Exception as primary_error:
                if not self.fallback:
                    raise
                warning = "eastmoney_failed: {}".format(
                    type(primary_error).__name__)
                raw = self._request(lambda: self.ak.stock_zh_a_minute(
                    symbol=normalized_symbol, period=period, adjust=adjust))
                provider = "akshare_sina_minute"
                received_at = _as_shanghai_timestamp(self._now())
                if self.raw_archive is not None:
                    self.raw_archive(
                        raw=raw, provider=provider, symbol=normalized_symbol,
                        interval_minutes=int(period),
                        request_started_at=request_started_at,
                        received_at=received_at)
                frame = self._normalize_minute_bars(
                    raw, normalized_symbol, period, request_started_at,
                    received_at,
                    volume_multiplier=1.0, provider=provider)

            start_at = _as_shanghai_timestamp(start)
            end_at = _as_shanghai_timestamp(end)
            frame = frame[
                frame.timestamp.ge(start_at) & frame.timestamp.le(end_at)
            ].reset_index(drop=True)
            if frame.empty:
                error = RealtimeMarketDataError(
                    "minute-bar response has no rows in requested range")
                error.reason_code = "NO_DATA"
                raise error
            complete = frame[frame.is_complete]
            fresh = bool(len(complete) and
                         complete.bar_end.max() >= received_at.floor("min") -
                         pd.Timedelta(seconds=self.stale_after_seconds))
            reasons = () if fresh else ("STALE_DATA",)
            self._success(
                provider, started, len(frame), warning=warning,
                data_fresh=fresh, fields_valid=True, reason_codes=reasons)
            symbol_health = self.health()
            with self._health_lock:
                self._symbol_health[normalized_symbol] = symbol_health
            return frame
        except Exception as error:
            self._failure(error, started)
            symbol_health = self.health()
            with self._health_lock:
                self._symbol_health[normalized_symbol] = symbol_health
            if isinstance(error, RealtimeMarketDataError):
                raise
            raise RealtimeMarketDataError(
                "AKShare minute bars failed for {}".format(normalized_symbol)) from error


class TencentMinuteMarketData(RealtimeMarketDataAdapter):
    """Tencent ``mkline`` 1-minute adapter with explicit provenance.

    Tencent labels its minute volume in lots.  The normalized contract stores
    shares, therefore the sixth response field is multiplied by 100.  The
    endpoint does not expose a documented traded-amount field; ``amount_raw``
    remains missing instead of inferring it from price and volume.
    """

    ENDPOINT = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"

    def __init__(self, session=None, timeout_seconds=8.0, retries=2,
                 retry_wait=0.5, rows=120, stale_after_seconds=125.0,
                 now=None, raw_archive=None, request_rate_limiter=None):
        super(TencentMinuteMarketData, self).__init__(
            source="tencent", stale_after_seconds=stale_after_seconds, now=now)
        if timeout_seconds <= 0 or retries <= 0 or retry_wait < 0:
            raise ValueError("invalid Tencent request configuration")
        if not 1 <= int(rows) <= 320:
            raise ValueError("Tencent rows must be between 1 and 320")
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.retries = int(retries)
        self.retry_wait = float(retry_wait)
        self.rows = int(rows)
        self.raw_archive = raw_archive
        self.request_rate_limiter = request_rate_limiter or (lambda: None)

    def snapshot(self, symbols=None, strict=True):
        raise NotImplementedError("Tencent minute adapter does not serve snapshots")

    @staticmethod
    def _extract_rows(payload, symbol):
        if not isinstance(payload, dict) or payload.get("code") not in (0, "0", None):
            raise _market_data_error(
                "Tencent minute endpoint returned an error payload",
                "PROVIDER_RESPONSE_ERROR")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise _market_data_error(
                "Tencent minute payload has no data object", "SCHEMA_ERROR")
        node = data.get(symbol)
        if node is None and len(data) == 1:
            node = next(iter(data.values()))
        if not isinstance(node, dict):
            raise _market_data_error(
                "Tencent minute payload has no symbol node", "NO_DATA")
        rows = node.get("m1")
        if rows is None:
            rows = node.get("data")
        if not isinstance(rows, list) or not rows:
            raise _market_data_error(
                "Tencent minute payload has no bars", "NO_DATA")
        return rows

    def _request_rows(self, symbol):
        last_error = None
        for attempt in range(self.retries):
            try:
                self.request_rate_limiter()
                response = self.session.get(
                    self.ENDPOINT,
                    params={"param": "{},m1,,{}".format(symbol, self.rows)},
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=self.timeout_seconds)
                if response.status_code in (403, 429):
                    raise _market_data_error(
                        "Tencent minute HTTP {}".format(response.status_code),
                        "HTTP_{}".format(response.status_code))
                if response.status_code >= 500:
                    raise _market_data_error(
                        "Tencent minute HTTP {}".format(response.status_code),
                        "HTTP_5XX")
                response.raise_for_status()
                return self._extract_rows(response.json(), symbol)
            except Exception as error:
                last_error = error
                reason = getattr(error, "reason_code", "")
                if reason in ("HTTP_403", "HTTP_429"):
                    break
                if attempt + 1 < self.retries:
                    time.sleep(self.retry_wait * (2 ** attempt))
        if isinstance(last_error, RealtimeMarketDataError):
            raise last_error
        raise _market_data_error(
            "Tencent minute request failed: {}".format(type(last_error).__name__),
            "TRANSPORT_ERROR") from last_error

    @staticmethod
    def _raw_frame(rows):
        normalized = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                raise _market_data_error(
                    "Tencent minute row has fewer than six fields", "SCHEMA_ERROR")
            normalized.append({
                "timestamp": row[0], "open": row[1], "close": row[2],
                "high": row[3], "low": row[4], "volume_lots": row[5],
            })
        return pd.DataFrame(normalized)

    @staticmethod
    def _normalize(raw, symbol, request_started_at, received_at):
        frame = raw.copy()
        frame["timestamp"] = pd.to_datetime(
            frame.timestamp.astype(str), format="%Y%m%d%H%M", errors="coerce")
        frame["timestamp"] = frame.timestamp.dt.tz_localize(SHANGHAI_TZ)
        for column in ("open", "close", "high", "low", "volume_lots"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame.dropna(
            subset=["timestamp", "open", "close", "high", "low", "volume_lots"],
            inplace=True)
        if frame.empty:
            raise _market_data_error(
                "Tencent minute response has no valid bars", "SCHEMA_ERROR")
        frame["symbol"] = symbol
        frame["received_at"] = received_at
        frame["interval_minutes"] = 1
        frame["volume"] = frame.volume_lots * 100.0
        frame["amount"] = np.nan
        frame["average"] = np.nan
        frame["change_pct"] = np.nan
        frame["turnover_rate"] = np.nan
        frame["source_timestamp"] = frame.timestamp
        frame["bar_end"] = frame.timestamp
        frame["bar_start"] = frame.bar_end - pd.Timedelta(minutes=1)
        frame["request_started_at"] = request_started_at
        frame["available_at"] = received_at
        frame["bar_complete"] = frame.bar_end.le(received_at.floor("min"))
        frame["source"] = "tencent_mkline_minute"
        frame["open_raw"] = frame.open
        frame["high_raw"] = frame.high
        frame["low_raw"] = frame.low
        frame["close_raw"] = frame.close
        frame["volume_shares"] = frame.volume
        frame["amount_raw"] = np.nan
        frame["revision"] = 1
        frame["is_complete"] = frame.bar_complete
        frame["quality_codes"] = frame.bar_complete.map(
            lambda complete: (("AMOUNT_MISSING",) if complete else
                              ("AMOUNT_MISSING", "BAR_INCOMPLETE")))
        for row in frame.itertuples(index=False):
            MinuteBarEvent(
                symbol=row.symbol, interval_minutes=1,
                source_timestamp=row.source_timestamp.isoformat(),
                bar_start=row.bar_start.isoformat(), bar_end=row.bar_end.isoformat(),
                request_started_at=row.request_started_at.isoformat(),
                received_at=row.received_at.isoformat(),
                available_at=row.available_at.isoformat(),
                open_raw=row.open_raw, high_raw=row.high_raw, low_raw=row.low_raw,
                close_raw=row.close_raw, volume_shares=row.volume_shares,
                amount_raw=None, source=row.source, revision=1,
                is_complete=row.is_complete, quality_codes=row.quality_codes)
        frame = frame[list(MINUTE_BAR_COLUMNS)]
        frame.sort_values("timestamp", inplace=True)
        frame.drop_duplicates("timestamp", keep="last", inplace=True)
        return frame.reset_index(drop=True)

    def minute_bars(self, symbol, period="1", start=None, end=None, adjust=""):
        if str(period) != "1" or adjust != "":
            raise ValueError("Tencent mkline supports only unadjusted 1-minute bars")
        normalized_symbol = normalize_cn_symbol(symbol)
        if normalized_symbol.startswith("bj"):
            raise _market_data_error(
                "Tencent minute source is not admitted for Beijing symbols",
                "UNSUPPORTED_EXCHANGE")
        started = time.monotonic()
        request_started_at = _as_shanghai_timestamp(self._now())
        try:
            rows = self._request_rows(normalized_symbol)
            received_at = _as_shanghai_timestamp(self._now())
            raw = self._raw_frame(rows)
            if self.raw_archive is not None:
                self.raw_archive(
                    raw=raw, provider="tencent_mkline_minute",
                    symbol=normalized_symbol, interval_minutes=1,
                    request_started_at=request_started_at,
                    received_at=received_at)
            frame = self._normalize(
                raw, normalized_symbol, request_started_at, received_at)
            if start is not None:
                frame = frame[frame.timestamp.ge(
                    _as_shanghai_timestamp(start))]
            if end is not None:
                frame = frame[frame.timestamp.le(
                    _as_shanghai_timestamp(end))]
            frame = frame.reset_index(drop=True)
            if frame.empty:
                raise _market_data_error(
                    "Tencent minute response has no rows in requested range", "NO_DATA")
            complete = frame[frame.is_complete]
            fresh = bool(len(complete) and complete.bar_end.max() >=
                         received_at.floor("min") -
                         pd.Timedelta(seconds=self.stale_after_seconds))
            reasons = () if fresh else ("STALE_DATA",)
            self._success(
                "tencent_mkline_minute", started, len(frame),
                data_fresh=fresh, fields_valid=True, reason_codes=reasons)
            symbol_health = self.health()
            with self._health_lock:
                self._symbol_health[normalized_symbol] = symbol_health
            return frame
        except Exception as error:
            self._failure(error, started)
            symbol_health = self.health()
            with self._health_lock:
                self._symbol_health[normalized_symbol] = symbol_health
            if isinstance(error, RealtimeMarketDataError):
                raise
            raise _market_data_error(
                "Tencent minute bars failed for {}".format(normalized_symbol),
                "PROVIDER_ERROR") from error


class SinaMinuteMarketData(AKShareRealtimeMarketData):
    """Sina-only minute fallback; never attempts Eastmoney."""

    def __init__(self, *args, **kwargs):
        kwargs["fallback"] = False
        super(SinaMinuteMarketData, self).__init__(*args, **kwargs)
        self.source = "sina"

    def minute_bars(self, symbol, period="1", start=None, end=None, adjust=""):
        period = str(period)
        if period not in self.PERIODS:
            raise ValueError("period must be one of {}".format(sorted(self.PERIODS)))
        if adjust not in ("", "qfq", "hfq"):
            raise ValueError("adjust must be '', qfq or hfq")
        normalized_symbol = normalize_cn_symbol(symbol)
        now = _as_shanghai_timestamp(self._now())
        start = start or (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        end = end or now.strftime("%Y-%m-%d %H:%M:%S")
        started = time.monotonic()
        request_started_at = _as_shanghai_timestamp(self._now())
        try:
            raw = self._request(lambda: self.ak.stock_zh_a_minute(
                symbol=normalized_symbol, period=period, adjust=adjust))
            received_at = _as_shanghai_timestamp(self._now())
            if self.raw_archive is not None:
                self.raw_archive(
                    raw=raw, provider="akshare_sina_minute",
                    symbol=normalized_symbol, interval_minutes=int(period),
                    request_started_at=request_started_at,
                    received_at=received_at)
            frame = self._normalize_minute_bars(
                raw, normalized_symbol, period, request_started_at, received_at,
                volume_multiplier=1.0, provider="akshare_sina_minute")
            frame = frame[
                frame.timestamp.ge(_as_shanghai_timestamp(start)) &
                frame.timestamp.le(_as_shanghai_timestamp(end))
            ].reset_index(drop=True)
            if frame.empty:
                raise _market_data_error(
                    "Sina minute response has no rows in requested range", "NO_DATA")
            complete = frame[frame.is_complete]
            fresh = bool(len(complete) and complete.bar_end.max() >=
                         received_at.floor("min") -
                         pd.Timedelta(seconds=self.stale_after_seconds))
            reasons = () if fresh else ("STALE_DATA",)
            self._success(
                "akshare_sina_minute", started, len(frame),
                data_fresh=fresh, fields_valid=True, reason_codes=reasons)
            symbol_health = self.health()
            with self._health_lock:
                self._symbol_health[normalized_symbol] = symbol_health
            return frame
        except Exception as error:
            self._failure(error, started)
            symbol_health = self.health()
            with self._health_lock:
                self._symbol_health[normalized_symbol] = symbol_health
            if isinstance(error, RealtimeMarketDataError):
                raise
            raise _market_data_error(
                "Sina minute bars failed for {}".format(normalized_symbol),
                "PROVIDER_ERROR") from error


class FailoverMinuteMarketData(RealtimeMarketDataAdapter):
    """Whole-response primary/fallback router with per-source circuit breakers."""

    def __init__(self, primary, fallback, *, failure_threshold=3,
                 circuit_breaker_seconds=900.0, max_fallback_per_cycle=5,
                 fallback_rate_limiter=None, monotonic=None, now=None):
        super(FailoverMinuteMarketData, self).__init__(
            source="minute_failover", stale_after_seconds=125.0, now=now)
        if failure_threshold <= 0 or circuit_breaker_seconds <= 0:
            raise ValueError("invalid circuit breaker configuration")
        if max_fallback_per_cycle < 0:
            raise ValueError("max_fallback_per_cycle cannot be negative")
        self.primary = primary
        self.fallback = fallback
        self.failure_threshold = int(failure_threshold)
        self.circuit_breaker_seconds = float(circuit_breaker_seconds)
        self.max_fallback_per_cycle = int(max_fallback_per_cycle)
        self.fallback_rate_limiter = fallback_rate_limiter or (lambda: None)
        self._monotonic = monotonic or time.monotonic
        self._failures = {"primary": 0, "fallback": 0}
        self._blocked_until = {"primary": 0.0, "fallback": 0.0}
        self._fallback_count = 0
        self._selected_health = {}

    @property
    def raw_archive(self):
        return getattr(self.primary, "raw_archive", None)

    @raw_archive.setter
    def raw_archive(self, value):
        if hasattr(self.primary, "raw_archive"):
            self.primary.raw_archive = value
        if hasattr(self.fallback, "raw_archive"):
            self.fallback.raw_archive = value

    def begin_cycle(self):
        self._fallback_count = 0

    def snapshot(self, symbols=None, strict=True):
        return self.primary.snapshot(symbols=symbols, strict=strict)

    def _available(self, name):
        return self._monotonic() >= self._blocked_until[name]

    def _failed(self, name, error):
        self._failures[name] += 1
        immediate = getattr(error, "reason_code", None) in ("HTTP_403", "HTTP_429")
        if immediate or self._failures[name] >= self.failure_threshold:
            self._blocked_until[name] = (
                self._monotonic() + self.circuit_breaker_seconds)

    def _succeeded(self, name):
        self._failures[name] = 0

    def minute_bars(self, symbol, period="1", start=None, end=None, adjust=""):
        normalized = normalize_cn_symbol(symbol)
        primary_error = None
        if self._available("primary"):
            try:
                frame = self.primary.minute_bars(
                    normalized, period=period, start=start, end=end, adjust=adjust)
                self._succeeded("primary")
                health = self.primary.health(normalized) or self.primary.health()
                self._selected_health[normalized] = health
                return frame
            except RealtimeMarketDataError as error:
                primary_error = error
                self._failed("primary", error)
        else:
            primary_error = _market_data_error(
                "primary minute source circuit is open", "CIRCUIT_OPEN")

        if (self._fallback_count >= self.max_fallback_per_cycle or
                not self._available("fallback")):
            reason = ("FALLBACK_QUOTA_EXHAUSTED" if
                      self._fallback_count >= self.max_fallback_per_cycle else
                      "CIRCUIT_OPEN")
            raise _market_data_error(
                "minute primary unavailable and fallback not admitted: {}".format(
                    reason), reason) from primary_error
        self._fallback_count += 1
        self.fallback_rate_limiter()
        try:
            frame = self.fallback.minute_bars(
                normalized, period=period, start=start, end=end, adjust=adjust)
            self._succeeded("fallback")
            health = self.fallback.health(normalized) or self.fallback.health()
            warning = "primary_failed:{}".format(
                getattr(primary_error, "reason_code", type(primary_error).__name__))
            if health is not None:
                health = MarketDataHealth(
                    **{**health.to_dict(), "last_warning": warning})
            self._selected_health[normalized] = health
            return frame
        except RealtimeMarketDataError as error:
            self._failed("fallback", error)
            raise _market_data_error(
                "both minute sources failed for {}".format(normalized),
                "ALL_PROVIDERS_FAILED") from error

    def health(self, symbol=None):
        if symbol is not None:
            return self._selected_health.get(normalize_cn_symbol(symbol))
        primary = self.primary.health()
        return primary
