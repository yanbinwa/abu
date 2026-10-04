#!/usr/bin/env python3
"""Append one fail-closed A-share close snapshot to the paper-trading data."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuLimitReference import (  # noqa: E402
    LimitReference, LimitReferenceStore,
)
from abupy.AlphaBu.ABuShortLineEvents import (  # noqa: E402
    json_safe_records, stable_payload_hash, stable_schema_hash,
)


INDEX_SYMBOLS = ("sh000001", "sh000300", "sz399001", "sz399006")
SH_PREFIXES = ("600", "601", "603", "605", "688", "689")
SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")


def _symbol(code):
    code = str(code).zfill(6)
    if code.startswith(SH_PREFIXES):
        return "sh" + code
    if code.startswith(SZ_PREFIXES):
        return "sz" + code
    return None


def normalize_spot(frame, trade_date, volume_multiplier=100.0):
    """Normalize Eastmoney's all-market close snapshot; volume is in lots."""
    required = {"代码", "名称", "最新价", "今开", "最高", "最低", "昨收",
                "成交量", "成交额"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError("spot fields missing: {}".format(sorted(missing)))
    work = frame.copy()
    work["symbol"] = work["代码"].map(_symbol)
    for optional in ("换手率", "流通市值"):
        if optional not in work:
            work[optional] = pd.NA
    for column in (required | {"换手率", "流通市值"}) - {"代码", "名称"}:
        work[column] = pd.to_numeric(work[column], errors="coerce")
    work = work.dropna(subset=["symbol", "最新价", "今开", "最高", "最低", "昨收"])
    valid = ((work["最新价"] > 0) & (work["今开"] > 0) &
             (work["最高"] >= work[["今开", "最新价", "最低"]].max(axis=1)) &
             (work["最低"] <= work[["今开", "最新价", "最高"]].min(axis=1)) &
             (work["昨收"] > 0) & (work["成交量"] > 0))
    work = work[valid].copy()
    work["date"] = int(trade_date)
    work["open"] = work["今开"]
    work["high"] = work["最高"]
    work["low"] = work["最低"]
    work["close"] = work["最新价"]
    work["pre_close"] = work["昨收"]
    work["volume"] = work["成交量"] * float(volume_multiplier)
    work["amount"] = work["成交额"]
    work["turnover"] = work["换手率"] / 100.0
    work["outstanding_share"] = work["流通市值"] / work["close"]
    result = work[["date", "symbol", "名称", "open", "high", "low", "close",
                   "pre_close", "volume", "amount", "outstanding_share", "turnover"]]
    return result.sort_values("symbol").reset_index(drop=True)


def write_limit_reference_snapshot(spot, snapshot_dir, provider,
                                   collected_at=None):
    """Persist explicit spot ``pre_close`` values as an immutable sidecar."""
    snapshot_dir = Path(snapshot_dir)
    collected_at = collected_at or datetime.now(timezone.utc).astimezone().isoformat()
    records = []
    for row in spot.itertuples(index=False):
        records.append(LimitReference(
            trade_date=int(row.date), symbol=str(row.symbol),
            limit_reference_price_raw=float(row.pre_close),
            source="{}_spot_snapshot".format(provider), quality="known",
            effective_at=str(int(row.date)), available_at=str(collected_at),
            availability_evidence="{}/stock_spot.csv".format(snapshot_dir),
            reason_codes=("PROVIDER_PRE_CLOSE",),
        ))
    output = snapshot_dir / "limit_reference.csv"
    if output.exists():
        existing_frame = LimitReferenceStore.read(output).to_frame()
        candidate_frame = LimitReferenceStore(records).to_frame()
        compare = [
            "trade_date", "symbol", "limit_reference_price_raw", "source",
            "quality", "effective_at", "availability_evidence", "reason_codes",
        ]
        existing_compare = existing_frame[compare].copy()
        candidate_compare = candidate_frame[compare].copy()
        for column in compare:
            if column not in ("trade_date", "limit_reference_price_raw"):
                existing_compare[column] = existing_compare[column].astype(str)
                candidate_compare[column] = candidate_compare[column].astype(str)
        existing_compare["trade_date"] = pd.to_numeric(
            existing_compare["trade_date"], errors="raise").astype(int)
        candidate_compare["trade_date"] = pd.to_numeric(
            candidate_compare["trade_date"], errors="raise").astype(int)
        existing_compare = existing_compare.sort_values(
            ["trade_date", "symbol"]).reset_index(drop=True)
        candidate_compare = candidate_compare.sort_values(
            ["trade_date", "symbol"]).reset_index(drop=True)
        if not existing_compare.equals(candidate_compare):
            raise RuntimeError("immutable limit-reference snapshot conflict")
        existing = output.read_bytes()
        digest = hashlib.sha256(existing).hexdigest()
        return output, digest
    LimitReferenceStore(records).write(output)
    return output, hashlib.sha256(output.read_bytes()).hexdigest()


def write_immutable_frame(frame, path):
    """Publish one canonical CSV without replacing an earlier snapshot."""
    path = Path(path)
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    payload = buffer.getvalue().encode("utf-8")
    if path.exists():
        existing = path.read_bytes()
        if existing != payload:
            raise RuntimeError("immutable market snapshot conflict: {}".format(path))
        return hashlib.sha256(existing).hexdigest()
    with path.open("xb") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    return hashlib.sha256(payload).hexdigest()


def write_market_provider_capture(frame, snapshot_dir, provider, trade_date,
                                  collected_at=None, nonce=None):
    """Archive every decoded provider response in its own immutable batch."""
    collected_at = collected_at or datetime.now(timezone.utc).astimezone().isoformat()
    nonce = nonce or uuid.uuid4().hex[:8]
    stamp = "".join(item for item in collected_at if item.isdigit())[:20]
    batch = Path(snapshot_dir) / "raw_batches" / (stamp + "_" + nonce)
    batch.mkdir(parents=True, exist_ok=False)
    records = json_safe_records(frame)
    raw = {
        "columns": [str(item) for item in frame.columns], "records": records,
    }
    raw_path = batch / "provider_frame.json"
    raw_path.write_text(json.dumps(
        raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8")
    metadata = {
        "capture_version": "paper_market_provider_capture_v1",
        "provider": provider, "trade_date": int(trade_date),
        "collected_at": collected_at, "row_count": len(records),
        "schema_sha256": stable_schema_hash(frame.columns),
        "payload_sha256": stable_payload_hash(records),
        "raw_path": str(raw_path),
    }
    (batch / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return metadata


def _csv_header_and_last(path):
    path = Path(path)
    with path.open("r", encoding="utf-8", newline="") as source:
        header = next(csv.reader(source))
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        end = source.tell()
        position = max(0, end - 8192)
        source.seek(position)
        lines = source.read().decode("utf-8").splitlines()
    data_lines = [line for line in lines if line.strip()]
    if not data_lines:
        raise ValueError("empty csv: {}".format(path))
    last_values = next(csv.reader([data_lines[-1]]))
    return header, dict(zip(header, last_values))


def _append_record(path, header, record):
    with Path(path).open("a", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=header, extrasaction="ignore")
        writer.writerow({name: record.get(name, "") for name in header})
        output.flush()
        os.fsync(output.fileno())


def _signal_path(signal_dir, symbol):
    matches = list(Path(signal_dir).glob(symbol + "_*"))
    if not matches:
        return None
    return max(matches, key=lambda item: item.stat().st_mtime)


def build_append_records(spot, signal_dir, raw_dir, trade_date):
    """Build validated append operations without mutating the price cache."""
    operations = []
    skipped = {"missing_files": 0, "already_present": 0, "bad_tail": 0}
    day_timestamp = pd.to_datetime(str(int(trade_date)), format="%Y%m%d")
    day_text = day_timestamp.strftime("%Y-%m-%d")
    weekday = day_timestamp.weekday()
    for row in spot.itertuples(index=False):
        signal_path = _signal_path(signal_dir, row.symbol)
        raw_path = Path(raw_dir) / (row.symbol + ".csv")
        if signal_path is None or not raw_path.exists():
            skipped["missing_files"] += 1
            continue
        signal_header, signal_last = _csv_header_and_last(signal_path)
        raw_header, raw_last = _csv_header_and_last(raw_path)
        signal_last_date = int(float(signal_last.get("date", 0)))
        raw_last_date = int(float(raw_last.get("date", 0)))
        if signal_last_date > int(trade_date) or raw_last_date > int(trade_date):
            raise ValueError("cache contains a future row for {}".format(row.symbol))
        if signal_last_date < int(trade_date):
            adjusted_previous = float(signal_last["close"])
            scale = adjusted_previous / float(row.pre_close)
            if not 0 < scale < 1e6:
                skipped["bad_tail"] += 1
                continue
            adjusted_close = float(row.close) * scale
            signal_record = {
                "date_time": day_text, "date": int(trade_date),
                "open": float(row.open) * scale, "high": float(row.high) * scale,
                "low": float(row.low) * scale, "close": adjusted_close,
                "pre_close": adjusted_previous, "volume": float(row.volume),
                "p_change": (adjusted_close / adjusted_previous - 1) * 100,
                "date_week": weekday,
                "key": int(float(signal_last.get("key", -1))) + 1,
            }
            operations.append((signal_path, signal_header, signal_record))
        if raw_last_date < int(trade_date):
            outstanding = float(row.outstanding_share)
            if not np.isfinite(outstanding) or outstanding <= 0:
                outstanding = float(raw_last.get("outstanding_share", "nan"))
            turnover = float(row.turnover)
            if (not np.isfinite(turnover) and np.isfinite(outstanding) and
                    outstanding > 0):
                turnover = float(row.volume) / outstanding
            raw_record = {
                "date": int(trade_date), "open": float(row.open),
                "high": float(row.high), "low": float(row.low),
                "close": float(row.close), "volume": float(row.volume),
                "amount": float(row.amount),
                "outstanding_share": outstanding, "turnover": turnover,
            }
            operations.append((raw_path, raw_header, raw_record))
        if signal_last_date == int(trade_date) and raw_last_date == int(trade_date):
            skipped["already_present"] += 1
    return operations, skipped


def _retry(call, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            return call()
        except Exception as error:
            last = error
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
    raise last


def _index_rows(ak, today):
    start = (today - timedelta(days=20)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    frames = {}
    for symbol in INDEX_SYMBOLS:
        try:
            frame = _retry(lambda: ak.stock_zh_index_daily_em(
                symbol=symbol, start_date=start, end_date=end))
        except Exception:
            frame = _retry(lambda: ak.stock_zh_index_daily(symbol=symbol))
            lower = pd.to_datetime(start, format="%Y%m%d")
            upper = pd.to_datetime(end, format="%Y%m%d")
            frame = frame[
                (pd.to_datetime(frame.date) >= lower) &
                (pd.to_datetime(frame.date) <= upper)]
        if frame is None or frame.empty:
            raise RuntimeError("empty index data: {}".format(symbol))
        frame = frame.copy()
        frame["date_number"] = pd.to_datetime(frame.date).dt.strftime("%Y%m%d").astype(int)
        frames[symbol] = frame
    return frames


def update_market_data(signal_dir, raw_dir, paper_dir, today=None, dry_run=False,
                       required_trade_date=None):
    import akshare as ak

    today = today or date.today()
    signal_dir, raw_dir, paper_dir = Path(signal_dir), Path(raw_dir), Path(paper_dir)
    paper_dir.mkdir(parents=True, exist_ok=True)
    if today.weekday() >= 5:
        result = {"status": "no_new_session", "reason": "weekend",
                  "checked_on": today.isoformat()}
        (paper_dir / "last_market_update.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    index_frames = _index_rows(ak, today)
    benchmark_path = _signal_path(signal_dir, "sh000300")
    if benchmark_path is None:
        raise FileNotFoundError("benchmark cache sh000300 is missing")
    _, benchmark_last = _csv_header_and_last(benchmark_path)
    cached_date = int(float(benchmark_last["date"]))
    available_dates = sorted(int(value) for value in
                             index_frames["sh000300"].date_number.unique()
                             if int(value) > cached_date)
    if not available_dates:
        result = {"status": "no_new_session", "cached_date": cached_date}
        (paper_dir / "last_market_update.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    if len(available_dates) != 1:
        raise RuntimeError(
            "{} missing trading sessions require historical backfill: {}".format(
                len(available_dates), available_dates))
    trade_date = available_dates[0]
    if required_trade_date is not None and trade_date != int(required_trade_date):
        raise RuntimeError('prospective collection requires a same-day index session')
    try:
        spot_raw = _retry(ak.stock_zh_a_spot_em)
        spot = normalize_spot(spot_raw, trade_date, volume_multiplier=100.0)
        spot_provider = "eastmoney"
    except Exception:
        spot_raw = _retry(ak.stock_zh_a_spot)
        spot = normalize_spot(spot_raw, trade_date, volume_multiplier=1.0)
        spot_provider = "sina"
    if len(spot) < 3000:
        raise RuntimeError("incomplete market snapshot: {} valid rows".format(len(spot)))
    snapshot_dir = paper_dir / "market_snapshots" / str(trade_date)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    collected_at = datetime.now(timezone.utc).astimezone().isoformat()
    provider_capture = write_market_provider_capture(
        spot_raw, snapshot_dir, spot_provider, trade_date,
        collected_at=collected_at)
    snapshot_path = snapshot_dir / "stock_spot.csv"
    digest = write_immutable_frame(spot, snapshot_path)
    reference_path, reference_digest = write_limit_reference_snapshot(
        spot, snapshot_dir, spot_provider, collected_at=collected_at)
    operations, skipped = build_append_records(
        spot, signal_dir, raw_dir, trade_date)
    if not dry_run:
        for path, header, record in operations:
            _append_record(path, header, record)
        # The benchmark is committed last.  If stock appends are interrupted,
        # rerunning remains safe because each stock row is date-idempotent.
        for symbol, frame in index_frames.items():
            row = frame[frame.date_number.eq(trade_date)]
            if row.empty:
                raise RuntimeError("index {} missing {}".format(symbol, trade_date))
            path = _signal_path(signal_dir, symbol)
            header, last = _csv_header_and_last(path)
            if int(float(last["date"])) < trade_date:
                item = row.iloc[-1]
                previous = float(last["close"])
                record = {
                    "date_time": pd.Timestamp(item.date).strftime("%Y-%m-%d"),
                    "date": trade_date, "open": float(item.open),
                    "high": float(item.high), "low": float(item.low),
                    "close": float(item.close), "pre_close": previous,
                    "volume": float(item.volume),
                    "p_change": (float(item.close) / previous - 1) * 100,
                    "date_week": pd.Timestamp(item.date).weekday(),
                    "key": int(float(last.get("key", -1))) + 1,
                }
                _append_record(path, header, record)
    result = {
        "status": "dry_run" if dry_run else "updated",
        "spot_provider": spot_provider,
        "trade_date": trade_date, "valid_spot_rows": len(spot),
        "append_operations": len(operations), "skipped": skipped,
        "snapshot": str(snapshot_path), "snapshot_sha256": digest,
        "provider_capture": provider_capture,
        "limit_reference_snapshot": str(reference_path),
        "limit_reference_sha256": reference_digest,
    }
    (paper_dir / "last_market_update.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    return result


def refresh_held_actions(paper_dir, research_dir):
    """Refresh company actions only for positions and pending buy orders."""
    paper_dir, research_dir = Path(paper_dir), Path(research_dir)
    state_path = paper_dir / "state.json"
    if not state_path.exists():
        return {"status": "paper_not_initialized", "symbols": 0}
    state = json.loads(state_path.read_text(encoding="utf-8"))
    symbols = set(state["active"]["positions"])
    symbols.update(order["symbol"] for order in state["active"]["orders"]
                   if order["side"] == "buy")
    if not symbols:
        return {"status": "no_exposure", "symbols": 0}
    import akshare as ak
    recovered, failed = [], {}
    for symbol in sorted(symbols):
        try:
            frame = _retry(lambda: ak.stock_dividend_cninfo(symbol=symbol[2:]))
            if frame is not None and not frame.empty:
                frame = frame.copy()
                frame.insert(0, "symbol", symbol)
                recovered.append(frame)
        except Exception as error:
            failed[symbol] = type(error).__name__
    if failed:
        raise RuntimeError("held-symbol corporate actions unavailable: {}".format(failed))
    action_path = research_dir / "corporate_actions.csv"
    existing = pd.read_csv(action_path, dtype={"symbol": str})
    if recovered:
        combined = pd.concat([existing] + recovered, ignore_index=True)
        combined.drop_duplicates(inplace=True)
        temporary = action_path.with_suffix(".csv.tmp")
        combined.to_csv(temporary, index=False)
        os.replace(temporary, action_path)
    return {"status": "updated", "symbols": len(symbols),
            "rows_received": sum(len(frame) for frame in recovered)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--raw-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research/raw"))
    parser.add_argument("--paper-dir", type=Path,
                        default=Path("/Users/wjy/abu/paper/vcp_residual_v2"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    args = parser.parse_args()
    result = update_market_data(
        args.signal_dir, args.raw_dir, args.paper_dir, dry_run=args.dry_run)
    if not args.dry_run:
        result["held_actions"] = refresh_held_actions(
            args.paper_dir, args.research_dir)
        (args.paper_dir / "last_market_update.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
