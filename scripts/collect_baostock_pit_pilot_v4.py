#!/usr/bin/env python3
"""Collect an immutable Baostock share/ST auxiliary pilot."""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    ImmutableFundamentalRawStore, stable_json,
)


DEFAULT_CONFIG = (
    ROOT / "configs/selection/baostock_structured_pilot_v4.json"
)


def _number(value):
    if value in (None, ""):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def normalize_rows(rows, symbol, payload_sha256,
                   source_role="auxiliary_audit_only",
                   derive_float_shares=True):
    output = []
    excluded = {}
    expected_code = symbol[:2] + "." + symbol[2:]
    for row in rows:
        try:
            day = date.fromisoformat(str(row["date"]))
            if str(row["code"]).lower() != expected_code:
                raise ValueError("SYMBOL_MISMATCH")
            trade_status = int(row["tradestatus"])
            is_st = int(row["isST"])
            if trade_status not in (0, 1) or is_st not in (0, 1):
                raise ValueError("INVALID_BOOLEAN_STATE")
            close = _number(row.get("close"))
            volume = _number(row.get("volume"))
            turn_percent = _number(row.get("turn"))
            derived_float = None
            if derive_float_shares and volume is not None and volume > 0 and \
                    turn_percent is not None and turn_percent > 0:
                derived_float = volume / (turn_percent / 100.0)
                if not math.isfinite(derived_float) or derived_float <= 0:
                    raise ValueError("INVALID_DERIVED_FLOAT_SHARES")
        except (KeyError, TypeError, ValueError) as error:
            reason = str(error)
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        output.append({
            "symbol": symbol,
            "date": int(day.strftime("%Y%m%d")),
            "raw_close": close,
            "volume": volume,
            "turn_percent": turn_percent,
            "derived_float_shares": derived_float,
            "trade_status": trade_status,
            "is_st": is_st,
            "source": "baostock_structured",
            "source_role": source_role,
            "historical_revision_complete": False,
            "raw_payload_sha256": payload_sha256,
        })
    return output, {
        "input_rows": len(rows),
        "normalized_rows": len(output),
        "derived_float_rows": sum(
            item["derived_float_shares"] is not None for item in output),
        "st_rows": len(output),
        "excluded_rows": sum(excluded.values()),
        "exclusion_reasons": dict(sorted(excluded.items())),
    }


def load_fetcher(fields):
    import baostock as bs

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError("BAOSTOCK_LOGIN_FAILED: " + login.error_msg)

    def fetch(symbol, start_date, end_date):
        code = symbol[:2] + "." + symbol[2:]
        result = bs.query_history_k_data_plus(
            code, ",".join(fields), start_date=start_date,
            end_date=end_date, frequency="d", adjustflag="3")
        if result.error_code != "0":
            raise RuntimeError(
                "BAOSTOCK_QUERY_FAILED: " + result.error_msg)
        rows = []
        while result.next():
            rows.append(dict(zip(fields, result.get_row_data())))
        return rows

    def close():
        bs.logout()

    return fetch, close


def collect(config, pilot_dir, output_dir, fetcher, close=None,
            raw_root=None, only_symbols=None):
    pilot_dir = Path(pilot_dir)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite Baostock pilot output")
    output_dir.mkdir(parents=True)
    universe = json.loads((pilot_dir / "pilot_symbols.json").read_text(
        encoding="utf-8"))
    symbols = universe["symbols"]
    symbol_ranges = universe.get("symbol_ranges", {})
    if only_symbols:
        requested = [str(symbol).lower() for symbol in only_symbols]
        unknown = set(requested) - set(symbols)
        if unknown:
            raise ValueError("symbols outside frozen pilot: " +
                             ",".join(sorted(unknown)))
        symbols = requested
    store = ImmutableFundamentalRawStore(raw_root or config["raw_root"])
    records = []
    audits = []
    failures = []
    try:
        for symbol in symbols:
            date_range = symbol_ranges.get(symbol, {})
            start_date = config.get("symbol_start_overrides", {}).get(
                symbol, date_range.get(
                    "start_date", config["history_start_date"]))
            end_date = date_range.get(
                "end_date", config["history_end_date"])
            request = {
                "adapter": "query_history_k_data_plus",
                "symbol": symbol[:2] + "." + symbol[2:],
                "fields": config["fields"],
                "start_date": start_date,
                "end_date": end_date,
                "frequency": "d",
                "adjustflag": "3",
            }
            try:
                rows = fetcher(
                    symbol, start_date, end_date)
                payload = stable_json({
                    "columns": config["fields"], "records": rows,
                }).encode("utf-8")
                outcome = "SUCCESS_NONEMPTY" if rows else "SUCCESS_EMPTY"
                _, metadata = store.capture(
                    "baostock_structured", "daily_share_st", request,
                    payload, outcome, config["source_version"],
                    record_count=len(rows), symbol=symbol,
                    unit="volume=shares;turn=percent")
                normalized, audit = normalize_rows(
                    rows, symbol, metadata["payload_sha256"],
                    source_role=config["source_role"],
                    derive_float_shares=config.get(
                        "derive_float_shares", True))
                records.extend(normalized)
                audits.append({"symbol": symbol, **audit,
                               "batch_id": metadata["batch_id"]})
            except Exception as error:
                _, metadata = store.capture(
                    "baostock_structured", "daily_share_st", request, b"",
                    "NETWORK_FAILURE", config["source_version"],
                    error=error, symbol=symbol)
                failures.append({
                    "symbol": symbol, "error_type": type(error).__name__,
                    "error": str(error), "batch_id": metadata["batch_id"],
                })
    finally:
        if close is not None:
            close()

    records.sort(key=lambda item: (item["symbol"], item["date"]))
    paths = {
        "data": output_dir / "baostock_daily.jsonl",
        "report": output_dir / "collection_report.json",
    }
    paths["data"].write_text("".join(
        stable_json(item) + "\n" for item in records), encoding="utf-8")
    covered = {record["symbol"] for record in records}
    report = {
        "config_version": config["config_version"],
        "source_version": config["source_version"],
        "source_role": config["source_role"],
        "research_label": config["research_label"],
        "symbol_count": len(symbols),
        "symbols_with_rows": len(covered),
        "symbols_without_rows": sorted(set(symbols) - covered),
        "normalized_security_days": len(records),
        "derived_float_rows": sum(
            record["derived_float_shares"] is not None for record in records),
        "st_rows": len(records),
        "dataset_audits": audits,
        "failures": failures,
        "status": config.get(
            "success_status", "COLLECTED_AUXILIARY_PILOT") if not failures
        else config.get("failure_status", "BLOCKED_AUXILIARY_PILOT"),
    }
    paths["report"].write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pilot_dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--symbols", nargs="*")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    fetcher, close = load_fetcher(config["fields"])
    report, paths = collect(
        config, args.pilot_dir, args.output_dir, fetcher, close=close,
        raw_root=args.raw_root, only_symbols=args.symbols)
    console = {key: value for key, value in report.items()
               if key != "dataset_audits"}
    print(json.dumps({**console, "outputs": {
        key: str(value) for key, value in paths.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
