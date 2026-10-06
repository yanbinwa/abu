#!/usr/bin/env python3
"""Capture append-only forward short-line event snapshots.

The collector intentionally keeps provider frames separate from normalized
research rows.  A capture can be retried or backfilled, but only a successful
same-day batch is marked eligible for point-in-time shadow features.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuShortLineEvents import (  # noqa: E402
    ImmutableShortLineSnapshotStore, establish_forward_anchor,
    load_shortline_forward_policy, read_forward_anchor,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE = "akshare_eastmoney"
ADAPTER_VERSION = "akshare_shortline_forward_v1"
ELTDX_SOURCE = "eltdx_tdx"
ELTDX_ADAPTER_VERSION = "eltdx_shortline_forward_v1"
ELTDX_DATASET = "eltdx_limit_up_down_list"
ELTDX_REQUIRED_COLUMNS = (
    "rqex", "ZQDM", "SC", "ZQJC", "ztlb", "lbts", "zglb", "ztyy",
    "fde", "ztsj", "kbcs", "sshy",
)
DEFAULT_FORWARD_CONFIG = (
    ROOT / "configs/selection/shortline_forward_v1.json")


DATASETS = {
    "stock_zt_pool_em": {
        "phase": "close_pools", "event_type": "CLOSED_UPPER",
        "required": ("代码", "名称", "最新价", "成交额", "流通市值",
                     "首次封板时间", "最后封板时间", "炸板次数", "连板数"),
    },
    "stock_zt_pool_dtgc_em": {
        "phase": "close_pools", "event_type": "CLOSED_LOWER",
        "required": ("代码", "名称", "最新价", "成交额", "流通市值",
                     "最后封板时间", "连续跌停", "开板次数"),
    },
    "stock_zt_pool_zbgc_em": {
        "phase": "close_pools", "event_type": "FAILED_UPPER_CLOSE",
        "required": ("代码", "名称", "最新价", "涨停价", "成交额",
                     "流通市值", "首次封板时间", "炸板次数"),
    },
    "stock_zt_pool_previous_em": {
        "phase": "close_metadata", "event_type": "PREVIOUS_LIMIT_UP_CONTEXT",
        "required": ("代码", "名称", "最新价", "成交额", "流通市值",
                     "昨日封板时间", "昨日连板数"),
    },
    "stock_zt_pool_strong_em": {
        "phase": "close_metadata", "event_type": "STRONG_POOL_CONTEXT",
        "required": ("代码", "名称", "最新价", "成交额", "流通市值",
                     "涨停统计", "入选理由"),
    },
}


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    os.replace(temporary, path)


def _symbol(code):
    value = str(code).strip().split(".")[0].zfill(6)
    if value.startswith(("600", "601", "603", "605", "688", "689")):
        return "sh" + value
    if value.startswith(("000", "001", "002", "003", "300", "301", "302")):
        return "sz" + value
    if value.startswith(("4", "8", "92")):
        return "bj" + value
    return ""


def _value(row, name):
    value = row.get(name)
    return None if pd.isna(value) else value


def normalize_event_pool(frame, dataset, trade_date, ingested_at):
    """Map provider fields without manufacturing missing event semantics."""
    spec = DATASETS[dataset]
    rows = []
    for _, row in frame.iterrows():
        rows.append({
            "trade_date": int(trade_date),
            "symbol": _symbol(_value(row, "代码")),
            "security_name": _value(row, "名称"),
            "event_type": spec["event_type"],
            "last_price": _value(row, "最新价"),
            "limit_price": _value(row, "涨停价"),
            "amount": _value(row, "成交额"),
            "turnover_pct": _value(row, "换手率"),
            "free_float_market_cap": _value(row, "流通市值"),
            "seal_amount": (_value(row, "封板资金") or
                            _value(row, "封单资金")),
            "first_limit_time": (_value(row, "首次封板时间") or
                                 _value(row, "昨日封板时间")),
            "last_limit_time": _value(row, "最后封板时间"),
            "open_break_count": (_value(row, "炸板次数") if
                                 _value(row, "炸板次数") is not None else
                                 _value(row, "开板次数")),
            "provider_streak": (_value(row, "连板数") if
                                _value(row, "连板数") is not None else
                                (_value(row, "连续跌停") if
                                 _value(row, "连续跌停") is not None else
                                 _value(row, "昨日连板数"))),
            "provider_limit_stat": _value(row, "涨停统计"),
            "source_category_raw": _value(row, "所属行业"),
            "selection_reason_raw": _value(row, "入选理由"),
            "source": SOURCE,
            "source_dataset": dataset,
            "effective_at": str(int(trade_date)),
            "available_at": ingested_at,
            "ingested_at": ingested_at,
            "availability_evidence": "FORWARD_CAPTURE" if
            int(datetime.fromisoformat(ingested_at).strftime("%Y%m%d")) ==
            int(trade_date) else "BACKFILLED_QUERY",
            "adapter_version": ADAPTER_VERSION,
        })
    return pd.DataFrame(rows)


def eltdx_provider_frame(result):
    """Preserve the provider rows without treating parsed fields as raw facts."""
    return pd.DataFrame([dict(getattr(row, "raw", {}) or {})
                         for row in getattr(result, "rows", ())])


def normalize_eltdx_limit_events(result, trade_date, ingested_at):
    """Normalize eltdx events while keeping reasons separate from selections."""
    event_types = {
        "limit_up": "CLOSED_UPPER",
        "broken": "FAILED_UPPER_CLOSE",
        "limit_down": "CLOSED_LOWER",
    }
    rows = []
    for row in getattr(result, "rows", ()):
        status = str(getattr(row, "status", ""))
        rows.append({
            "trade_date": int(trade_date),
            "symbol": _symbol(getattr(row, "code", "")),
            "security_name": getattr(row, "name", None),
            "event_type": event_types.get(status, "UNKNOWN_PROVIDER_EVENT"),
            "last_price": None,
            "limit_price": None,
            "amount": None,
            "turnover_pct": None,
            "free_float_market_cap": None,
            "seal_amount": getattr(row, "seal_amount", None),
            "first_limit_time": None,
            "last_limit_time": None,
            "provider_limit_time": getattr(row, "limit_time", None),
            "open_break_count": getattr(row, "broken_count", None),
            "provider_streak": getattr(row, "board_level", None),
            "provider_highest_streak": getattr(
                row, "highest_board_level", None),
            "provider_limit_stat": None,
            "source_category_raw": getattr(row, "industry", None),
            "selection_reason_raw": None,
            "limit_reason_raw": getattr(row, "limit_reason", None),
            "limit_reason_extra_raw": getattr(
                row, "limit_reason_extra", None),
            "source": ELTDX_SOURCE,
            "source_dataset": ELTDX_DATASET,
            "effective_at": str(int(trade_date)),
            "available_at": ingested_at,
            "ingested_at": ingested_at,
            "availability_evidence": "FORWARD_CAPTURE" if
            int(datetime.fromisoformat(ingested_at).strftime("%Y%m%d")) ==
            int(trade_date) else "BACKFILLED_QUERY",
            "adapter_version": ELTDX_ADAPTER_VERSION,
        })
    return pd.DataFrame(rows)


def compare_close_event_sets(ak_frames, eltdx_result):
    """Return audit-only set differences without resolving either source."""
    status_by_dataset = {
        "stock_zt_pool_em": "limit_up",
        "stock_zt_pool_zbgc_em": "broken",
        "stock_zt_pool_dtgc_em": "limit_down",
    }
    eltdx_sets = {}
    for row in getattr(eltdx_result, "rows", ()):
        status = str(getattr(row, "status", ""))
        code = str(getattr(row, "code", "") or "").zfill(6)
        if code:
            eltdx_sets.setdefault(status, set()).add(code)
    comparisons = []
    for dataset, status in status_by_dataset.items():
        frame = ak_frames.get(dataset)
        if frame is None or frame.empty or "代码" not in frame:
            continue
        left = eltdx_sets.get(status, set())
        right = set(frame["代码"].astype(str).str.split(".").str[0].str.zfill(6))
        union = left | right
        comparisons.append({
            "event_status": status,
            "eltdx_count": len(left),
            "akshare_count": len(right),
            "intersection_count": len(left & right),
            "jaccard": float(len(left & right) / len(union)) if union else 1.0,
            "only_eltdx": sorted(left - right),
            "only_akshare": sorted(right - left),
        })
    return comparisons


def normalize_auction_quote(frame, trade_date, ingested_at):
    """Store a 09:26 quote proxy; it is never labelled as exact auction data."""
    aliases = {
        "代码": "code", "名称": "security_name", "最新价": "last_price",
        "今开": "open", "昨收": "pre_close", "成交量": "volume",
        "成交额": "amount", "换手率": "turnover_pct",
    }
    work = frame.rename(columns=aliases).copy()
    rows = []
    for _, row in work.iterrows():
        rows.append({
            "trade_date": int(trade_date), "symbol": _symbol(row.get("code")),
            "security_name": _value(row, "security_name"),
            "last_price": _value(row, "last_price"),
            "open": _value(row, "open"), "pre_close": _value(row, "pre_close"),
            "volume": _value(row, "volume"), "amount": _value(row, "amount"),
            "turnover_pct": _value(row, "turnover_pct"),
            "available_at": ingested_at, "ingested_at": ingested_at,
            "source": SOURCE, "source_dataset": "auction_quote_snapshot",
            "source_semantics": "09:26_full_market_quote_proxy",
            "adapter_version": ADAPTER_VERSION,
        })
    return pd.DataFrame(rows)


def selected_datasets(phase):
    if phase == "close":
        return list(DATASETS)
    return [name for name, spec in DATASETS.items() if spec["phase"] == phase]


def capture_time_gate(phase, now):
    minute = now.hour * 60 + now.minute
    if phase == "auction":
        return (9 * 60 + 26 <= minute <= 9 * 60 + 35,
                "auction capture requires 09:26-09:35 Asia/Shanghai")
    return (minute >= 15 * 60 + 20,
            "close pools require 15:20 or later Asia/Shanghai")


def _retry(call, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            return call()
        except Exception as error:  # provider exceptions vary by release
            last = error
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise last


def _market_snapshot_evidence(paper_dir, trade_date):
    directory = Path(paper_dir) / "market_snapshots" / str(int(trade_date))
    evidence = {}
    for name in ("stock_spot.csv", "limit_reference.csv"):
        path = directory / name
        if path.exists():
            evidence[name] = {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
    return evidence


def collect(*, trade_date, phase, output_dir, paper_dir, now=None,
            ak_module=None, calendar_dates=None, forward_config=None,
            enable_eltdx_shadow=False, eltdx_client=None):
    now = now or datetime.now(SHANGHAI)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    now = now.astimezone(SHANGHAI)
    trade_date = int(trade_date)
    policy = load_shortline_forward_policy(
        forward_config or DEFAULT_FORWARD_CONFIG)
    import akshare as ak
    ak = ak_module or ak
    if calendar_dates is None:
        calendar = _retry(ak.tool_trade_date_hist_sina)
        calendar_dates = set(pd.to_datetime(
            calendar["trade_date"]).dt.strftime("%Y%m%d").astype(int))
    is_session = trade_date in set(int(item) for item in calendar_dates)
    run_id = "{}_{}".format(
        now.strftime("%Y%m%dT%H%M%S%f%z"), uuid.uuid4().hex[:8])
    run = {
        "collector_version": ADAPTER_VERSION, "run_id": run_id,
        "trade_date": trade_date, "phase": phase,
        "started_at": now.isoformat(), "timezone": "Asia/Shanghai",
        "market_snapshot_evidence": _market_snapshot_evidence(
            paper_dir, trade_date),
        "captures": [],
        "forward_policy_version": policy.policy_version,
        "forward_policy_sha256": policy.sha256,
        "forward_nominal_start_date": policy.nominal_start_date,
        "forward_start_definition": policy.start_definition,
        "feature_mode": policy.feature_mode,
        "order_mutation_allowed": policy.order_mutation_allowed,
        "paper_order_effect": "none",
        "theme_reason_status": "UNAVAILABLE_FROM_CURRENT_AKSHARE_ENDPOINTS",
        "optional_sources": {
            "eltdx": "enabled_shadow" if enable_eltdx_shadow else "disabled",
        },
    }
    run_dir = Path(output_dir) / "_runs" / str(trade_date)
    run_path = run_dir / (run_id + ".json")
    if not is_session:
        run.update({"status": "skipped_non_trading_day",
                    "reason": "official trade calendar has no session"})
        _atomic_json(run_path, run)
        return run
    if phase == "auction" and trade_date != int(now.strftime("%Y%m%d")):
        run.update({"status": "skipped_invalid_auction_backfill",
                    "reason": "09:26 quote proxy cannot be reconstructed later"})
        _atomic_json(run_path, run)
        return run
    gate_phase = "auction" if phase == "auction" else "close"
    allowed, reason = capture_time_gate(gate_phase, now)
    if trade_date == int(now.strftime("%Y%m%d")) and not allowed:
        run.update({"status": "skipped_too_early", "reason": reason})
        _atomic_json(run_path, run)
        return run

    store = ImmutableShortLineSnapshotStore(output_dir)
    ingested_at = now.isoformat()
    if phase == "auction":
        dataset = "auction_quote_snapshot"
        try:
            frame = _retry(ak.stock_zh_a_spot_em)
            completeness_error = None
            if frame is not None and 0 < len(frame) < 3000:
                completeness_error = RuntimeError(
                    "incomplete all-market quote snapshot: {} rows".format(len(frame)))
            normalized = normalize_auction_quote(
                frame, trade_date, ingested_at) if completeness_error is None else None
            meta = store.write(
                frame, source=SOURCE, dataset=dataset, trade_date=trade_date,
                ingested_at=ingested_at, nonce=run_id,
                required_columns=("代码", "名称", "最新价", "今开", "昨收",
                                  "成交量", "成交额"),
                normalized=normalized, phase="auction",
                source_semantics="09:26_full_market_quote_proxy",
                quality_codes=("PROXY_NOT_EXACT_AUCTION_FEED",),
                strategy_feature_allowed=False,
                error=completeness_error)
        except Exception as error:
            meta = store.write(
                pd.DataFrame(), source=SOURCE, dataset=dataset,
                trade_date=trade_date, ingested_at=ingested_at, nonce=run_id,
                phase="auction",
                source_semantics="09:26_full_market_quote_proxy",
                quality_codes=("PROXY_NOT_EXACT_AUCTION_FEED",),
                strategy_feature_allowed=False, error=error)
        run["captures"].append(meta)
    else:
        ak_frames = {}
        for index, dataset in enumerate(selected_datasets(phase)):
            spec = DATASETS[dataset]
            try:
                frame = _retry(lambda dataset=dataset: getattr(
                    ak, dataset)(date=str(trade_date)))
                normalized = normalize_event_pool(
                    frame, dataset, trade_date, ingested_at)
                ak_frames[dataset] = frame
                meta = store.write(
                    frame, source=SOURCE, dataset=dataset,
                    trade_date=trade_date, ingested_at=ingested_at,
                    nonce="{}_{}".format(run_id, index),
                    required_columns=spec["required"], normalized=normalized,
                    phase=spec["phase"],
                    source_semantics="provider_event_pool",
                    quality_codes=("SHADOW_ONLY_FORWARD_SAMPLE",),
                    strategy_feature_allowed=False)
            except Exception as error:
                meta = store.write(
                    pd.DataFrame(), source=SOURCE, dataset=dataset,
                    trade_date=trade_date, ingested_at=ingested_at,
                    nonce="{}_{}".format(run_id, index),
                    required_columns=spec["required"], phase=spec["phase"],
                    source_semantics="provider_event_pool",
                    quality_codes=("SHADOW_ONLY_FORWARD_SAMPLE",),
                    strategy_feature_allowed=False, error=error)
            run["captures"].append(meta)

        if enable_eltdx_shadow:
            eltdx_result = None
            try:
                if eltdx_client is None:
                    from eltdx import F10Client
                    eltdx_client = F10Client(timeout=15.0)
                eltdx_result = _retry(
                    lambda: eltdx_client.limit_up_down_list(str(trade_date)))
                frame = eltdx_provider_frame(eltdx_result)
                normalized = normalize_eltdx_limit_events(
                    eltdx_result, trade_date, ingested_at)
                meta = store.write(
                    frame, source=ELTDX_SOURCE, dataset=ELTDX_DATASET,
                    trade_date=trade_date, ingested_at=ingested_at,
                    nonce="{}_eltdx".format(run_id),
                    required_columns=ELTDX_REQUIRED_COLUMNS,
                    normalized=normalized, phase="close_pools",
                    source_semantics="tdx_f10_limit_up_down_list",
                    quality_codes=("SHADOW_ONLY_FORWARD_SAMPLE",
                                   "SECONDARY_SOURCE_UNVALIDATED"),
                    strategy_feature_allowed=False)
            except Exception as error:
                meta = store.write(
                    pd.DataFrame(), source=ELTDX_SOURCE,
                    dataset=ELTDX_DATASET, trade_date=trade_date,
                    ingested_at=ingested_at,
                    nonce="{}_eltdx".format(run_id),
                    required_columns=ELTDX_REQUIRED_COLUMNS,
                    phase="close_pools",
                    source_semantics="tdx_f10_limit_up_down_list",
                    quality_codes=("SHADOW_ONLY_FORWARD_SAMPLE",
                                   "SECONDARY_SOURCE_UNVALIDATED"),
                    strategy_feature_allowed=False, error=error)
            run["captures"].append(meta)
            run["optional_sources"]["eltdx"] = meta["status"]
            run["theme_reason_status"] = (
                "ELTDX_LIMIT_REASON_SHADOW_UNVALIDATED" if
                meta["status"] == "success" else
                "ELTDX_LIMIT_REASON_UNAVAILABLE")
            run["cross_source_comparisons"] = (
                compare_close_event_sets(ak_frames, eltdx_result)
                if eltdx_result is not None else [])

    statuses = pd.Series([item["status"] for item in run["captures"]]).value_counts()
    run["status_counts"] = {str(key): int(value) for key, value in statuses.items()}
    run["asof_eligible_count"] = sum(
        bool(item["asof_feature_allowed"]) for item in run["captures"])
    run["strategy_feature_eligible_count"] = sum(
        bool(item["strategy_feature_allowed"]) for item in run["captures"])
    required_captures = [
        item for item in run["captures"]
        if item.get("dataset") in policy.required_datasets]
    optional_captures = [
        item for item in run["captures"]
        if item.get("dataset") not in policy.required_datasets]
    required_eligible = sum(
        bool(item["asof_feature_allowed"]) for item in required_captures)
    optional_eligible = sum(
        bool(item["asof_feature_allowed"]) for item in optional_captures)
    run["required_capture_count"] = len(required_captures)
    run["required_asof_eligible_count"] = required_eligible
    run["optional_capture_count"] = len(optional_captures)
    run["optional_asof_eligible_count"] = optional_eligible
    run["optional_capture_status"] = (
        "not_enabled" if not optional_captures else
        ("captured" if all(
            item.get("status") == "success" for item in optional_captures) else
         "captured_partial_or_failed"))
    status_captures = (run["captures"] if phase == "auction" else
                       required_captures)
    status_eligible = sum(
        bool(item["asof_feature_allowed"]) for item in status_captures)
    if status_eligible == len(status_captures) and status_captures:
        run["status"] = "captured"
    elif status_eligible:
        run["status"] = "captured_partial"
    else:
        run["status"] = "captured_no_asof_eligible_data"
    archive_complete = policy.archive_complete(run["captures"])
    anchor = establish_forward_anchor(
        output_dir, policy, trade_date=trade_date, run_id=run_id,
        created_at=now.isoformat(), captures=run["captures"])
    if anchor is None:
        anchor = read_forward_anchor(output_dir, policy)
    anchor_date = (int(anchor["anchor_trade_date"])
                   if anchor is not None else None)
    run["forward_archive_complete"] = archive_complete
    run["forward_anchor_trade_date"] = anchor_date
    run["forward_sample_eligible"] = bool(
        archive_complete and anchor_date is not None and
        trade_date >= anchor_date)
    if trade_date < policy.nominal_start_date:
        run["forward_sample_status"] = "prestart_shadow"
    elif run["forward_sample_eligible"]:
        run["forward_sample_status"] = "eligible_forward_shadow"
    else:
        run["forward_sample_status"] = "awaiting_successful_archive"
    run["completed_at"] = datetime.now(SHANGHAI).isoformat()
    _atomic_json(run_path, run)
    return run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trade-date", type=int)
    parser.add_argument("--phase", choices=(
        "auction", "close_pools", "close_metadata", "close"), default="close")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/shortline_forward"))
    parser.add_argument("--paper-dir", type=Path, default=Path(
        "/Users/wjy/abu/paper/vcp_residual_v2"))
    parser.add_argument("--forward-config", type=Path,
                        default=DEFAULT_FORWARD_CONFIG)
    parser.add_argument("--now", help="test/recovery clock with timezone")
    parser.add_argument("--enable-eltdx-shadow", action="store_true")
    args = parser.parse_args()
    now = (datetime.fromisoformat(args.now).astimezone(SHANGHAI) if args.now
           else datetime.now(SHANGHAI))
    trade_date = args.trade_date or int(now.strftime("%Y%m%d"))
    result = collect(
        trade_date=trade_date, phase=args.phase, output_dir=args.output_dir,
        paper_dir=args.paper_dir, now=now, forward_config=args.forward_config,
        enable_eltdx_shadow=args.enable_eltdx_shadow)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
