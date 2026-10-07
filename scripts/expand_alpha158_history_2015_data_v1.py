#!/usr/bin/env python3
"""Build a resumable 2011-warmup dataset for Alpha158 2015+ validation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.download_selection_research_data import _metadata_one  # noqa: E402


DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_history_2015_data_v1.json"
RAW_COLUMNS = (
    "date", "open", "high", "low", "close", "volume", "amount",
    "outstanding_share", "turnover",
)


def write_json(path, payload):
    Path(path).write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def validate_config(config):
    if config["dataset_id"] != "alpha158_history_2015_data_v1":
        raise ValueError("unexpected dataset id")
    if config["evaluation_start_date"] != "2015-01-01":
        raise ValueError("evaluation start is frozen to 2015-01-01")
    if config["panel_start_date"] > "2011-01-01":
        raise ValueError("panel warmup must begin no later than 2011-01-01")
    if config["prefix_end_date"] != "2019-12-31":
        raise ValueError("prefix boundary must match the existing 2020 dataset")
    if config["source_adjustflag_raw"] != "3":
        raise ValueError("execution prices must be unadjusted")
    if config["source_adjustflag_signal_fallback"] != "2":
        raise ValueError("missing delisted signals require forward adjustment")
    if config["overwrite_existing"] is not False:
        raise ValueError("the expansion may not overwrite completed files")


def filter_universe(frame, config):
    work = frame.copy()
    work["code"] = work.code.astype(str).str.lower()
    work = work[work.type.astype(str).eq("1")]
    work = work[work.code.str.startswith("sz.")]
    prefixes = tuple(config["allowed_prefixes"])
    work = work[work.code.str[3:].str.startswith(prefixes)]
    cutoff = config["evaluation_start_date"]
    active = work.status.astype(str).eq("1")
    delisted_in_scope = (
        work.outDate.fillna("").astype(str).ge(cutoff) &
        work.outDate.fillna("").astype(str).ne(""))
    work = work[active | delisted_in_scope].copy()
    work["symbol"] = work.code.str.replace(".", "", regex=False)
    work["status_label"] = np.where(active.loc[work.index], "listed", "delisted")
    work.sort_values("symbol", inplace=True)
    work.drop_duplicates("symbol", keep="first", inplace=True)
    return work.reset_index(drop=True)


def security_master(universe):
    return pd.DataFrame({
        "code": universe.code.str[3:],
        "name": universe.code_name,
        "list_date": universe.ipoDate,
        "delist_date": universe.outDate.replace("", np.nan),
        "exchange": "sz",
        "symbol": universe.symbol,
        "status": universe.status_label,
    })


def normalize_raw(rows, fields):
    if not rows:
        return pd.DataFrame(columns=RAW_COLUMNS)
    frame = pd.DataFrame(rows, columns=fields)
    result = pd.DataFrame()
    result["date"] = pd.to_datetime(
        frame.date, errors="coerce").dt.strftime("%Y%m%d")
    for column in ("open", "high", "low", "close", "volume", "amount"):
        result[column] = pd.to_numeric(frame[column], errors="coerce")
    turn = pd.to_numeric(frame["turn"], errors="coerce")
    fraction = turn / 100.0
    result["outstanding_share"] = result.volume / fraction.where(fraction > 0)
    result["turnover"] = fraction
    result["date"] = pd.to_numeric(result.date, errors="coerce")
    result.dropna(subset=["date"], inplace=True)
    result["date"] = result.date.astype(np.int64)
    result.sort_values("date", inplace=True)
    result.drop_duplicates("date", keep="last", inplace=True)
    return result[list(RAW_COLUMNS)].reset_index(drop=True)


def normalize_signal(rows, fields):
    columns = ("date", "open", "high", "low", "close", "volume")
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(rows, columns=fields)
    result = pd.DataFrame()
    result["date"] = pd.to_datetime(
        frame.date, errors="coerce").dt.strftime("%Y%m%d")
    for column in columns[1:]:
        result[column] = pd.to_numeric(frame[column], errors="coerce")
    result["date"] = pd.to_numeric(result.date, errors="coerce")
    result.dropna(subset=["date"], inplace=True)
    result["date"] = result.date.astype(np.int64)
    result.sort_values("date", inplace=True)
    result.drop_duplicates("date", keep="last", inplace=True)
    return result[list(columns)].reset_index(drop=True)


def merge_raw(prefix, existing):
    frames = [item for item in (prefix, existing)
              if item is not None and not item.empty]
    if not frames:
        return pd.DataFrame(columns=RAW_COLUMNS)
    result = pd.concat(frames, ignore_index=True)
    for column in RAW_COLUMNS:
        if column not in result:
            result[column] = np.nan
    result = result[list(RAW_COLUMNS)]
    result.sort_values("date", inplace=True)
    result.drop_duplicates("date", keep="last", inplace=True)
    return result.reset_index(drop=True)


def atomic_csv(frame, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, path)


def link_or_copy(source, destination):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


class BaoStockClient(object):
    def __init__(self, attempts=3):
        import baostock as bs
        self.bs = bs
        self.attempts = int(attempts)
        self._login()

    def _login(self):
        result = self.bs.login()
        if result.error_code != "0":
            raise RuntimeError("BAOSTOCK_LOGIN_FAILED: " + result.error_msg)

    def close(self):
        self.bs.logout()

    def query(self, code, fields, start_date, end_date, adjustflag):
        last_error = None
        for attempt in range(self.attempts):
            try:
                result = self.bs.query_history_k_data_plus(
                    code, ",".join(fields), start_date=start_date,
                    end_date=end_date, frequency="d", adjustflag=adjustflag)
                if result.error_code != "0":
                    raise RuntimeError(result.error_msg)
                rows = []
                while result.next():
                    rows.append(result.get_row_data())
                return rows
            except Exception as error:
                last_error = error
                if attempt + 1 < self.attempts:
                    try:
                        self.bs.logout()
                    except Exception:
                        pass
                    time.sleep(1 + attempt)
                    self._login()
        raise RuntimeError("BAOSTOCK_QUERY_FAILED: {}".format(last_error))


def query_basic():
    import baostock as bs
    result = bs.login()
    if result.error_code != "0":
        raise RuntimeError("BAOSTOCK_LOGIN_FAILED: " + result.error_msg)
    try:
        query = bs.query_stock_basic()
        if query.error_code != "0":
            raise RuntimeError("BAOSTOCK_BASIC_FAILED: " + query.error_msg)
        rows = []
        while query.next():
            rows.append(query.get_row_data())
        return pd.DataFrame(rows, columns=query.fields)
    finally:
        bs.logout()


def prepare(config):
    output = Path(config["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    for name in ("raw", "signal_extra", "status"):
        (output / name).mkdir(exist_ok=True)
    universe_path = output / "frozen_universe.csv"
    if universe_path.exists():
        raise FileExistsError("frozen universe already exists")
    universe = filter_universe(query_basic(), config)
    universe.to_csv(universe_path, index=False)
    security_master(universe).to_csv(output / "security_master.csv", index=False)

    existing = Path(config["existing_research_dir"])
    for name in ("corporate_actions.csv", "industry_changes.csv",
                 "sz_name_changes.csv"):
        shutil.copy2(existing / name, output / name)
    for path in (existing / "signal_extra").glob("sz*.csv"):
        link_or_copy(path, output / "signal_extra" / path.name)
    write_json(output / "registration.json", {
        "dataset_id": config["dataset_id"],
        "registered_at": datetime.now().astimezone().isoformat(),
        "config": config,
        "config_sha256": hashlib.sha256(json.dumps(
            config, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "universe_rows": len(universe),
        "listed": int(universe.status_label.eq("listed").sum()),
        "delisted": int(universe.status_label.eq("delisted").sum()),
        "registered_before_collection": True,
    })
    print(json.dumps({
        "status": "PREPARED", "universe": len(universe),
        "output": str(output)}, ensure_ascii=False))


def signal_exists(symbol, config, output):
    signal_dir = Path(config["existing_signal_dir"])
    return (any(signal_dir.glob(symbol + "_*")) or
            (output / "signal_extra" / (symbol + ".csv")).exists())


def collect_symbol(row, config, output, client):
    symbol = str(row.symbol)
    marker = output / "status" / (symbol + ".json")
    destination = output / "raw" / (symbol + ".csv")
    if marker.exists() and destination.exists():
        payload = json.loads(marker.read_text(encoding="utf-8"))
        if payload.get("status") == "COMPLETE" and \
                payload.get("sha256") == digest(destination):
            return "cached"

    existing_path = (Path(config["existing_research_dir"]) /
                     "raw" / (symbol + ".csv"))
    existing = pd.read_csv(existing_path) if existing_path.exists() else None
    ipo = str(row.ipoDate or config["panel_start_date"])
    start = max(config["panel_start_date"], ipo)
    end = config["prefix_end_date"]
    prefix = pd.DataFrame(columns=RAW_COLUMNS)
    if start <= end:
        rows = client.query(
            str(row.code), config["raw_fields"], start, end,
            config["source_adjustflag_raw"])
        prefix = normalize_raw(rows, config["raw_fields"])
    merged = merge_raw(prefix, existing)
    if merged.empty:
        raise ValueError("NO_RAW_ROWS")
    if merged.date.duplicated().any() or not merged.date.is_monotonic_increasing:
        raise ValueError("INVALID_RAW_DATE_AXIS")
    atomic_csv(merged, destination)

    if not signal_exists(symbol, config, output):
        signal_rows = client.query(
            str(row.code), config["signal_fallback_fields"], start,
            str(row.outDate or end),
            config["source_adjustflag_signal_fallback"])
        signal = normalize_signal(
            signal_rows, config["signal_fallback_fields"])
        if signal.empty:
            raise ValueError("NO_ADJUSTED_SIGNAL_ROWS")
        atomic_csv(signal, output / "signal_extra" / (symbol + ".csv"))

    write_json(marker, {
        "status": "COMPLETE", "symbol": symbol,
        "rows": len(merged), "first_date": int(merged.date.min()),
        "last_date": int(merged.date.max()),
        "prefix_rows": len(prefix),
        "existing_rows": 0 if existing is None else len(existing),
        "sha256": digest(destination),
    })
    return "collected"


def collect_shard(config, shard_index, shard_count):
    output = Path(config["output_dir"])
    universe = pd.read_csv(
        output / "frozen_universe.csv", dtype=str).fillna("")
    selected = universe.iloc[int(shard_index)::int(shard_count)]
    client = BaoStockClient()
    counts = {"collected": 0, "cached": 0, "failed": 0}
    failures = []
    started = time.time()
    try:
        for position, row in enumerate(selected.itertuples(index=False), 1):
            try:
                status = collect_symbol(row, config, output, client)
                counts[status] += 1
            except Exception as error:
                counts["failed"] += 1
                failures.append({
                    "symbol": str(row.symbol),
                    "error_type": type(error).__name__, "error": str(error)})
            if position % 25 == 0 or position == len(selected):
                print(json.dumps({
                    "shard": shard_index, "processed": position,
                    "total": len(selected), **counts,
                    "elapsed_seconds": round(time.time() - started, 1),
                }), flush=True)
    finally:
        client.close()
    write_json(output / "status" / "shard-{:02d}.json".format(shard_index), {
        "status": "COMPLETE" if not failures else "COMPLETE_WITH_FAILURES",
        "shard_index": int(shard_index), "shard_count": int(shard_count),
        "counts": counts, "failures": failures,
        "completed_at": datetime.now().astimezone().isoformat(),
    })
    return 1 if failures else 0


def append_missing_metadata(config, universe):
    output = Path(config["output_dir"])
    existing = Path(config["existing_research_dir"])
    old_master = pd.read_csv(existing / "security_master.csv", dtype=str)
    missing = sorted(set(universe.symbol) - set(old_master.symbol))
    if not missing:
        return {"symbols": 0, "errors": {}}
    actions, industries, errors = [], [], {}
    # AKShare's CNINFO adapter initializes py_mini_racer global state and can
    # abort the entire interpreter when several first calls race.  This path
    # only covers the small set of pre-2020 delistings, so keep it sequential.
    for symbol in missing:
        try:
            symbol_actions, symbol_industries, symbol_errors = _metadata_one(
                symbol, "19900101",
                config["latest_complete_session"].replace("-", ""))
            actions.extend(symbol_actions)
            industries.extend(symbol_industries)
            if symbol_errors:
                errors[symbol] = symbol_errors
        except Exception as error:
            errors[symbol] = [type(error).__name__]
    for name, rows in (("corporate_actions.csv", actions),
                       ("industry_changes.csv", industries)):
        if not rows:
            continue
        frame = pd.read_csv(output / name)
        frame = pd.concat([frame, pd.DataFrame(rows)], ignore_index=True)
        frame.drop_duplicates(inplace=True)
        frame.to_csv(output / name, index=False)
    return {"symbols": len(missing), "errors": errors,
            "action_rows": len(actions), "industry_rows": len(industries)}


def status(config):
    output = Path(config["output_dir"])
    universe_path = output / "frozen_universe.csv"
    if not universe_path.exists():
        return {"status": "NOT_PREPARED"}
    universe = pd.read_csv(universe_path, dtype=str)
    complete = 0
    failures = []
    for symbol in universe.symbol:
        marker = output / "status" / (symbol + ".json")
        if marker.exists() and (output / "raw" / (symbol + ".csv")).exists():
            complete += 1
    for path in sorted((output / "status").glob("shard-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        failures.extend(payload.get("failures", []))
    return {
        "status": "COLLECTING" if complete < len(universe) else "COLLECTED",
        "universe": len(universe), "complete": complete,
        "remaining": len(universe) - complete, "reported_failures": failures,
    }


def finalize(config):
    output = Path(config["output_dir"])
    state = status(config)
    if state["remaining"]:
        raise RuntimeError("collection incomplete: {}".format(state))
    universe = pd.read_csv(output / "frozen_universe.csv", dtype=str)
    missing_signals = [
        symbol for symbol in universe.symbol
        if not signal_exists(symbol, config, output)]
    metadata = append_missing_metadata(config, universe)
    report = {
        "status": "COMPLETE" if not missing_signals and
        not metadata["errors"] else "BLOCKED_INCOMPLETE",
        "dataset_id": config["dataset_id"],
        "universe": len(universe),
        "raw_files": len(list((output / "raw").glob("*.csv"))),
        "missing_signals": missing_signals,
        "metadata_extension": metadata,
        "panel_start_date": config["panel_start_date"],
        "evaluation_start_date": config["evaluation_start_date"],
        "latest_complete_session": config["latest_complete_session"],
        "strict_pit_claim": False,
        "research_only": True,
        "limitations": [
            "Historical market data are vendor backfills, not archived same-day snapshots.",
            "The strategy universe remains Shenzhen A shares to preserve the current strict ST-history policy.",
            "2011-2014 are warmup and model-training history; reported performance begins in 2015."
        ],
    }
    write_json(output / "dataset_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "COMPLETE" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "collect", "status", "finalize"))
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    validate_config(config)
    if args.shard_count <= 0 or not 0 <= args.shard_index < args.shard_count:
        raise ValueError("invalid shard coordinates")
    if args.mode == "prepare":
        prepare(config)
        return 0
    if args.mode == "collect":
        return collect_shard(config, args.shard_index, args.shard_count)
    if args.mode == "status":
        print(json.dumps(status(config), ensure_ascii=False, indent=2))
        return 0
    return finalize(config)


if __name__ == "__main__":
    raise SystemExit(main())
