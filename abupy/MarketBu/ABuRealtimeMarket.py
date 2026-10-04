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
)


class RealtimeMarketDataError(RuntimeError):
    """Raised when a real-time provider cannot return a trustworthy result."""


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

    @abstractmethod
    def snapshot(self, symbols=None, strict=True):
        """Return normalized current quotes."""

    @abstractmethod
    def minute_bars(self, symbol, period="1", start=None, end=None, adjust=""):
        """Return normalized historical/current minute bars."""

    def _success(self, provider, started, count, warning=None):
        with self._health_lock:
            self._last_provider = provider
            self._last_success_at = _as_shanghai_timestamp(self._now())
            self._last_error = None
            self._last_warning = warning
            self._consecutive_failures = 0
            self._last_latency_ms = round((time.monotonic() - started) * 1000, 3)
            self._last_record_count = int(count)

    def _failure(self, error, started):
        with self._health_lock:
            self._last_error = "{}: {}".format(type(error).__name__, error)
            self._last_warning = None
            self._consecutive_failures += 1
            self._last_latency_ms = round((time.monotonic() - started) * 1000, 3)

    def health(self):
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
                 spot_volume_multiplier=100.0, minute_volume_multiplier=100.0):
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
        received_at = _as_shanghai_timestamp(self._now())
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
    def _normalize_minute_bars(raw, symbol, period, received_at,
                               volume_multiplier, provider):
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
        # Providers label bars by their period endpoint. Exclude the timestamp
        # matching the local current minute because it can still be updating.
        frame["bar_complete"] = frame.timestamp < received_at.floor("min")
        frame["source"] = provider
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
        warning = None
        try:
            try:
                raw = self._request(lambda: self.ak.stock_zh_a_hist_min_em(
                    symbol=normalized_symbol[2:], start_date=str(start),
                    end_date=str(end), period=period, adjust=adjust))
                provider = "akshare_eastmoney_minute"
                frame = self._normalize_minute_bars(
                    raw, normalized_symbol, period, now,
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
                frame = self._normalize_minute_bars(
                    raw, normalized_symbol, period, now,
                    volume_multiplier=1.0, provider=provider)

            start_at = _as_shanghai_timestamp(start)
            end_at = _as_shanghai_timestamp(end)
            frame = frame[
                frame.timestamp.ge(start_at) & frame.timestamp.le(end_at)
            ].reset_index(drop=True)
            self._success(provider, started, len(frame), warning=warning)
            return frame
        except Exception as error:
            self._failure(error, started)
            if isinstance(error, RealtimeMarketDataError):
                raise
            raise RealtimeMarketDataError(
                "AKShare minute bars failed for {}".format(normalized_symbol)) from error
