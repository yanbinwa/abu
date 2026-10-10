from __future__ import absolute_import

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

from ..MarketBu.ABuRealtimeMarket import normalize_cn_symbol
from .ABuContentStore import ContentAddressedStore
from .ABuDailyDataCenter import ProviderRateLimiter


SHANGHAI = ZoneInfo("Asia/Shanghai")
TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
SINA_QUOTE_URL = "https://hq.sinajs.cn/list="


class QuoteSnapshotError(RuntimeError):
    pass


def _chunks(values, size):
    values = tuple(values)
    for start in range(0, len(values), int(size)):
        yield values[start:start + int(size)]


def _safe_float(value):
    try:
        result = float(value)
        return result if np.isfinite(result) else np.nan
    except (TypeError, ValueError):
        return np.nan


class WholeMarketQuoteClient(object):
    """Bounded Tencent/Sina snapshot client used only for research context."""

    def __init__(self, provider, *, session=None, timeout_seconds=10.0,
                 rate_limiter=None, raw_archive=None):
        if provider not in ("tencent", "sina"):
            raise ValueError("provider must be tencent or sina")
        self.provider = provider
        self.session = session or requests.Session()
        self.timeout_seconds = float(timeout_seconds)
        self.rate_limiter = rate_limiter or (lambda: None)
        self.raw_archive = raw_archive

    def _request(self, symbols, batch_index):
        self.rate_limiter()
        url = (TENCENT_QUOTE_URL if self.provider == "tencent"
               else SINA_QUOTE_URL) + ",".join(symbols)
        headers = {"User-Agent": "Mozilla/5.0"}
        if self.provider == "sina":
            headers["Referer"] = "https://finance.sina.com.cn/"
        response = self.session.get(url, headers=headers, timeout=self.timeout_seconds)
        if response.status_code in (403, 429):
            raise QuoteSnapshotError(
                "{} quote HTTP {}".format(self.provider, response.status_code))
        response.raise_for_status()
        text = response.content.decode("gb18030", errors="replace")
        if self.raw_archive is not None:
            self.raw_archive(
                provider=self.provider, batch_index=int(batch_index),
                symbols=symbols, content=response.content)
        return text

    @staticmethod
    def _parse_tencent(text):
        rows = []
        for match in re.finditer(r'v_([a-z]{2}\d+)="([^\"]*)"', text):
            fields = match.group(2).split("~")
            if len(fields) <= 49:
                continue
            timestamp = pd.to_datetime(
                fields[30], format="%Y%m%d%H%M%S", errors="coerce")
            rows.append({
                "symbol": normalize_cn_symbol(match.group(1)),
                "name": fields[1], "last": _safe_float(fields[3]),
                "pre_close": _safe_float(fields[4]),
                "open": _safe_float(fields[5]),
                "volume": _safe_float(fields[6]) * 100.0,
                "provider_time": timestamp,
                "change_pct": _safe_float(fields[32]) / 100.0,
                "high": _safe_float(fields[33]), "low": _safe_float(fields[34]),
                "amount": _safe_float(fields[37]) * 10000.0,
                "turnover_rate": _safe_float(fields[38]) / 100.0,
                "pe": _safe_float(fields[39]), "pb": _safe_float(fields[46]),
                "limit_up": _safe_float(fields[47]),
                "limit_down": _safe_float(fields[48]),
                "volume_ratio": _safe_float(fields[49]),
                "source": "tencent_market_snapshot",
            })
        return rows

    @staticmethod
    def _parse_sina(text):
        rows = []
        for match in re.finditer(r'var hq_str_([a-z]{2}\d+)="([^\"]*)"', text):
            fields = match.group(2).split(",")
            if len(fields) < 32 or not fields[0]:
                continue
            timestamp = pd.to_datetime(
                "{} {}".format(fields[30], fields[31]), errors="coerce")
            pre_close = _safe_float(fields[2])
            last = _safe_float(fields[3])
            rows.append({
                "symbol": normalize_cn_symbol(match.group(1)),
                "name": fields[0], "last": last, "pre_close": pre_close,
                "open": _safe_float(fields[1]), "high": _safe_float(fields[4]),
                "low": _safe_float(fields[5]), "volume": _safe_float(fields[8]),
                "amount": _safe_float(fields[9]), "provider_time": timestamp,
                "change_pct": ((last / pre_close - 1.0)
                               if pre_close > 0 and np.isfinite(last) else np.nan),
                "turnover_rate": np.nan, "pe": np.nan, "pb": np.nan,
                "limit_up": np.nan, "limit_down": np.nan,
                "volume_ratio": np.nan,
                "source": "sina_market_snapshot",
            })
        return rows

    def snapshot(self, symbols):
        symbols = tuple(sorted({normalize_cn_symbol(value) for value in symbols}))
        batch_size = 60 if self.provider == "tencent" else 800
        rows = []
        for index, batch in enumerate(_chunks(symbols, batch_size)):
            text = self._request(batch, index)
            rows.extend(self._parse_tencent(text) if self.provider == "tencent"
                        else self._parse_sina(text))
        frame = pd.DataFrame(rows)
        if frame.empty:
            raise QuoteSnapshotError(
                "{} returned no valid whole-market quotes".format(self.provider))
        frame.drop_duplicates("symbol", keep="last", inplace=True)
        frame.sort_values("symbol", inplace=True)
        return frame.reset_index(drop=True)


def _market_metrics(frame):
    valid = frame[
        frame["last"].gt(0) & frame["pre_close"].gt(0)].copy()
    returns = valid["last"] / valid["pre_close"] - 1.0
    return {
        "quote_count": int(len(frame)), "valid_quote_count": int(len(valid)),
        "advancers": int(returns.gt(0).sum()),
        "decliners": int(returns.lt(0).sum()),
        "unchanged": int(returns.eq(0).sum()),
        "advance_ratio": float(returns.gt(0).mean()) if len(valid) else None,
        "equal_weight_return": float(returns.mean()) if len(valid) else None,
        "median_return": float(returns.median()) if len(valid) else None,
        "total_amount": float(valid.amount.fillna(0).sum()),
        "limit_up_count": int((
            valid.limit_up.gt(0) &
            valid["last"].ge(valid.limit_up * 0.999)).sum()),
        "limit_down_count": int((
            valid.limit_down.gt(0) &
            valid["last"].le(valid.limit_down * 1.001)).sum()),
    }


def _cross_source_metrics(primary, reference):
    joined = primary[["symbol", "last"]].merge(
        reference[["symbol", "last"]], on="symbol", suffixes=("_primary", "_reference"))
    joined = joined[
        joined.last_primary.gt(0) & joined.last_reference.gt(0)].copy()
    difference = (joined.last_primary / joined.last_reference - 1.0).abs()
    return {
        "matched_quote_count": int(len(joined)),
        "primary_coverage_ratio": float(len(joined) / max(1, len(primary))),
        "median_absolute_price_difference": (
            float(difference.median()) if len(joined) else None),
        "p95_absolute_price_difference": (
            float(difference.quantile(.95)) if len(joined) else None),
    }


def _fresh_quotes(frame, now, stale_after_seconds):
    timestamps = pd.to_datetime(frame.provider_time, errors="coerce")
    if getattr(timestamps.dt, "tz", None) is None:
        timestamps = timestamps.dt.tz_localize("Asia/Shanghai")
    else:
        timestamps = timestamps.dt.tz_convert("Asia/Shanghai")
    local_now = pd.Timestamp(now)
    ages = (local_now - timestamps).dt.total_seconds()
    mask = ((timestamps.dt.date == local_now.date()) &
            ages.ge(-60) & ages.le(float(stale_after_seconds)))
    return frame.loc[mask].copy()


def _industry_metrics(frame, mapping):
    joined = frame.merge(mapping, on="symbol", how="left")
    joined = joined[
        joined.industry.notna() & joined["last"].gt(0) &
        joined.pre_close.gt(0)].copy()
    joined["return"] = joined["last"] / joined.pre_close - 1.0
    rows = []
    for industry, group in joined.groupby("industry", sort=True):
        rows.append({
            "industry": str(industry), "quote_count": int(len(group)),
            "advancers": int(group["return"].gt(0).sum()),
            "decliners": int(group["return"].lt(0).sum()),
            "advance_ratio": float(group["return"].gt(0).mean()),
            "equal_weight_return": float(group["return"].mean()),
            "total_amount": float(group.amount.fillna(0).sum()),
            "limit_up_count": int((
                group.limit_up.gt(0) &
                group["last"].ge(group.limit_up * .999)).sum()),
            "limit_down_count": int((
                group.limit_down.gt(0) &
                group["last"].le(group.limit_down * 1.001)).sum()),
        })
    return rows


class IntradaySentimentSnapshotJob(object):
    """Archive all-market quotes and publish derived breadth, with no account writes."""

    def __init__(self, operational_store, content_root, config_path, *,
                 clock=None, primary_client=None, reference_client=None,
                 primary_rate_limiter=None, reference_rate_limiter=None):
        self.store = operational_store
        self.content = ContentAddressedStore(content_root)
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if self.config.get("execution_mode") != "DATA_ONLY":
            raise ValueError("intraday sentiment job must remain DATA_ONLY")
        if float(self.config["tencent_requests_per_second"]) > 2.0:
            raise ValueError("Tencent whole-market source is capped at 2 requests/second")
        if float(self.config["sina_requests_per_second"]) > 0.5:
            raise ValueError("Sina whole-market source is capped at 0.5 requests/second")
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.raw_records = []

        def archive(**item):
            path, digest, unused_created = self.content.write_bytes(
                "intraday_quote_raw", item.pop("content"), suffix=".txt")
            self.raw_records.append({
                **item, "path": str(path), "sha256": digest,
            })

        self.primary = primary_client or WholeMarketQuoteClient(
            "tencent", timeout_seconds=self.config["request_timeout_seconds"],
            rate_limiter=(primary_rate_limiter or ProviderRateLimiter(
                self.config["tencent_requests_per_second"]).wait),
            raw_archive=archive)
        self.reference = reference_client or WholeMarketQuoteClient(
            "sina", timeout_seconds=self.config["request_timeout_seconds"],
            rate_limiter=(reference_rate_limiter or ProviderRateLimiter(
                self.config["sina_requests_per_second"]).wait),
            raw_archive=archive)

    def _symbols(self):
        frame = pd.read_csv(self.config["security_master_path"], dtype=str)
        if "status" in frame:
            frame = frame[frame.status.eq("listed")]
        symbols = sorted({normalize_cn_symbol(value) for value in frame.symbol.dropna()})
        if not symbols:
            raise ValueError("intraday sentiment universe is empty")
        return symbols

    def _industry_mapping(self, session):
        frame = pd.read_csv(self.config["industry_history_path"], dtype=str)
        required = {"symbol", "分类标准", "行业大类", "变更日期"}
        missing = sorted(required - set(frame.columns))
        if missing:
            raise ValueError("industry history missing: {}".format(
                ", ".join(missing)))
        frame = frame[
            frame["分类标准"].eq(
                self.config["industry_classification_standard"])].copy()
        frame["effective_session"] = pd.to_datetime(
            frame["变更日期"], errors="coerce").dt.strftime("%Y%m%d")
        frame["effective_session"] = pd.to_numeric(
            frame.effective_session, errors="coerce")
        frame = frame[frame.effective_session.le(int(session))]
        frame.sort_values(["symbol", "effective_session"], inplace=True)
        frame.drop_duplicates("symbol", keep="last", inplace=True)
        result = frame[["symbol", "行业大类"]].rename(
            columns={"行业大类": "industry"})
        result["symbol"] = result.symbol.map(normalize_cn_symbol)
        return result

    def _previous_manifest(self, session):
        candidates = []
        for path in self.content.iter_files("intraday_sentiment_manifests"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if int(payload.get("trading_session", 0)) == int(session):
                candidates.append((int(payload["sequence_no"]), path, payload))
        return None if not candidates else max(candidates, key=lambda item: item[0])

    def __call__(self):
        now = self.clock().astimezone(SHANGHAI)
        session = int(now.strftime("%Y%m%d"))
        if session < int(self.config["first_eligible_session"]):
            return {"status": "SKIPPED_BEFORE_ELIGIBLE_SESSION",
                    "output_snapshot_ids": []}
        calendar = json.loads(Path(
            self.config["trading_calendar_path"]).read_text(encoding="utf-8"))
        if session not in {int(value) for value in calendar["dates"]}:
            return {"status": "SKIPPED_NON_TRADING_SESSION",
                    "output_snapshot_ids": []}
        clock = now.strftime("%H:%M:%S")
        if not any(item["start"] <= clock <= item["end"]
                   for item in self.config["collection_windows"]):
            return {"status": "SKIPPED_OUTSIDE_COLLECTION_WINDOW",
                    "output_snapshot_ids": []}
        self.raw_records = []
        symbols = self._symbols()
        industry_mapping = self._industry_mapping(session)
        primary = self.primary.snapshot(symbols)
        reference = self.reference.snapshot(symbols)
        minimum = int(len(symbols) * float(self.config["minimum_coverage_ratio"]))
        if len(primary) < minimum or len(reference) < minimum:
            raise QuoteSnapshotError(
                "whole-market quote coverage below threshold")
        fresh_primary = _fresh_quotes(
            primary, now, self.config["stale_after_seconds"])
        fresh_reference = _fresh_quotes(
            reference, now, self.config["stale_after_seconds"])
        fresh_minimum = int(len(symbols) * float(
            self.config["minimum_fresh_ratio"]))
        if len(fresh_primary) < fresh_minimum or len(fresh_reference) < fresh_minimum:
            raise QuoteSnapshotError(
                "whole-market current-session freshness below threshold")
        mapped = fresh_primary[["symbol"]].merge(
            industry_mapping, on="symbol", how="inner")
        if len(mapped) < int(len(fresh_primary) * float(
                self.config["minimum_industry_coverage_ratio"])):
            raise QuoteSnapshotError("industry mapping coverage below threshold")
        primary_payload = json.loads(primary.to_json(
            orient="records", date_format="iso"))
        reference_payload = json.loads(reference.to_json(
            orient="records", date_format="iso"))
        quote_path, quote_sha, unused_created = self.content.write_json(
            "intraday_quote_normalized", {
                "schema_version": "intraday_quote_rows_v1",
                "captured_at": now.isoformat(), "primary": primary_payload,
                "reference": reference_payload,
            })
        previous = self._previous_manifest(session)
        manifest = {
            "schema_version": "intraday_sentiment_manifest_v1",
            "stream_id": "market-intraday-sentiment:{}".format(session),
            "sequence_no": 1 if previous is None else previous[0] + 1,
            "previous_manifest_sha256": (
                None if previous is None else previous[1].stem),
            "trading_session": session, "decision_cutoff": now.isoformat(),
            "created_at": now.isoformat(), "universe_count": len(symbols),
            "universe_source": self.config["security_master_path"],
            "industry_source": self.config["industry_history_path"],
            "industry_classification_standard": self.config[
                "industry_classification_standard"],
            "primary_source": "tencent_market_snapshot",
            "reference_source": "sina_market_snapshot",
            "normalized_quote_path": str(quote_path),
            "normalized_quote_sha256": quote_sha,
            "raw_batches": list(self.raw_records),
            "market_metrics": _market_metrics(fresh_primary),
            "reference_metrics": _market_metrics(fresh_reference),
            "cross_source_metrics": _cross_source_metrics(
                fresh_primary, fresh_reference),
            "industry_metrics": _industry_metrics(
                fresh_primary, industry_mapping),
            "quality_codes": [],
        }
        manifest_path, manifest_sha, unused_created = self.content.write_json(
            "intraday_sentiment_manifests", manifest)
        return {"status": "COMMITTED", "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_sha,
                "metrics": manifest["market_metrics"],
                "output_snapshot_ids": []}
