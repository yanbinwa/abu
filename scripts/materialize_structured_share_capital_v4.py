#!/usr/bin/env python3
"""Materialize conservative daily share capital and market caps for a pilot."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    ShareCapitalStore, TotalShareStore, reconcile_share_capital, stable_json,
    structured_balance_total_share_events,
)
from scripts.collect_structured_fundamental_v4 import trading_sessions  # noqa: E402


DEFAULT_CONFIG = (
    ROOT / "configs/selection/fundamental_structured_pit_v4.json"
)
DEFAULT_RESEARCH = Path("/Users/wjy/abu/data/selection_research")


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(
        encoding="utf-8").splitlines() if line.strip()]


def _finite(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if np.isfinite(numeric) else None


def materialize(pilot_dir, research_dir, output_dir, config,
                start_date=20200101, end_date=20260930,
                share_events_path=None):
    pilot_dir = Path(pilot_dir)
    research_dir = Path(research_dir)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite share materialization")
    output_dir.mkdir(parents=True)
    sessions = trading_sessions(config["trading_sessions_source"])
    facts = read_jsonl(pilot_dir / "structured_extracted.jsonl")
    share_events_path = Path(
        share_events_path or pilot_dir / "share_events.jsonl")
    primary_events = read_jsonl(share_events_path)
    symbols = json.loads((pilot_dir / "pilot_symbols.json").read_text(
        encoding="utf-8"))["symbols"]
    balance_events, balance_audit = structured_balance_total_share_events(
        facts, sessions)
    primary_store = ShareCapitalStore(primary_events)
    balance_store = TotalShareStore(balance_events)
    tolerance = config["strict_rules"][
        "share_reconciliation_relative_tolerance"]
    records = []
    symbol_reports = {}
    missing_price_symbols = []

    for symbol in symbols:
        path = research_dir / "raw" / (symbol + ".csv")
        if not path.is_file():
            missing_price_symbols.append(symbol)
            symbol_reports[symbol] = {
                "price_rows": 0, "total_market_cap_coverage": None,
                "float_market_cap_coverage": None,
                "conflict_rows": 0, "reason": "NO_LOCAL_PRICE_HISTORY",
            }
            continue
        bars = pd.read_csv(path)
        bars = bars[(bars.date >= int(start_date)) &
                    (bars.date <= int(end_date))].copy()
        total_valid = float_valid = conflicts = 0
        statuses = Counter()
        for row in bars.itertuples(index=False):
            day = int(row.date)
            day_text = datetime.strptime(str(day), "%Y%m%d").date().isoformat()
            asof = day_text + "T15:00:00+08:00"
            primary = primary_store.asof(symbol, asof)
            balance = balance_store.asof(symbol, asof)
            floated = _finite(getattr(row, "outstanding_share", None))
            merged = reconcile_share_capital(
                primary, balance, floated, relative_tolerance=tolerance)
            close = _finite(getattr(row, "close", None))
            total_cap = None if close is None or \
                merged["total_shares"] is None else \
                close * merged["total_shares"]
            float_cap = None if close is None or \
                merged["float_shares"] is None else \
                close * merged["float_shares"]
            total_valid += int(total_cap is not None)
            float_valid += int(float_cap is not None)
            conflicts += int(merged["reconciliation_status"] == "CONFLICT")
            statuses[merged["reconciliation_status"]] += 1
            records.append({
                "date": day, "symbol": symbol, "raw_close": close,
                **merged, "total_market_cap": total_cap,
                "float_market_cap": float_cap,
            })
        count = len(bars)
        symbol_reports[symbol] = {
            "price_rows": count,
            "total_market_cap_coverage": total_valid / count if count else None,
            "float_market_cap_coverage": float_valid / count if count else None,
            "conflict_rows": conflicts,
            "status_counts": dict(sorted(statuses.items())),
            "reason": None if count else "NO_ROWS_IN_AUDIT_PERIOD",
        }

    eligible_rows = len(records)
    total_coverage = (
        sum(item["total_market_cap"] is not None for item in records) /
        eligible_rows if eligible_rows else 0.0
    )
    float_coverage = (
        sum(item["float_market_cap"] is not None for item in records) /
        eligible_rows if eligible_rows else 0.0
    )
    conflicts = sum(
        item["reconciliation_status"] == "CONFLICT" for item in records)
    conflict_reasons = dict(sorted(Counter(
        reason for item in records for reason in item["conflict_reasons"]
    ).items()))
    sources = {
        "total": dict(sorted(Counter(
            item["total_share_source"] for item in records
            if item["total_share_source"]).items())),
        "float": dict(sorted(Counter(
            item["float_share_source"] for item in records
            if item["float_share_source"]).items())),
    }
    rules = config["strict_rules"]
    blockers = []
    if total_coverage < rules["total_market_cap_coverage_min"]:
        blockers.append("total_market_cap_coverage")
    if float_coverage < rules["float_market_cap_coverage_min"]:
        blockers.append("float_market_cap_coverage")
    if conflicts:
        blockers.append("share_source_reconciliation_conflicts")
    report = {
        "config_version": config["config_version"],
        "research_label": config["research_label"],
        "share_events_input": str(share_events_path),
        "audit_period": {"start": int(start_date), "end": int(end_date)},
        "pilot_symbol_count": len(symbols),
        "symbols_with_price_history": len(symbols) - len(missing_price_symbols),
        "missing_price_symbols": missing_price_symbols,
        "eligible_security_days": eligible_rows,
        "total_market_cap_coverage": total_coverage,
        "float_market_cap_coverage": float_coverage,
        "conflict_rows": conflicts,
        "conflict_reason_counts": conflict_reasons,
        "source_usage": sources,
        "balance_event_audit": balance_audit,
        "symbol_coverage": symbol_reports,
        "blockers": blockers,
        "gate_status": "PASS_PILOT_SHARE_CAPITAL_GATE" if not blockers
        else "BLOCKED_PILOT_SHARE_CAPITAL_GATE",
    }
    data_path = output_dir / "share_capital_daily.jsonl"
    report_path = output_dir / "share_capital_coverage.json"
    data_path.write_text("".join(
        stable_json(item) + "\n" for item in records), encoding="utf-8")
    report_path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, {"data": data_path, "report": report_path}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pilot_dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-date", type=int, default=20200101)
    parser.add_argument("--end-date", type=int, default=20260930)
    parser.add_argument(
        "--share-events", type=Path,
        help="optional replayed share_events.jsonl; defaults to the pilot file")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report, paths = materialize(
        args.pilot_dir, args.research_dir, args.output_dir, config,
        start_date=args.start_date, end_date=args.end_date,
        share_events_path=args.share_events)
    console_report = {
        key: value for key, value in report.items()
        if key != "symbol_coverage"
    }
    print(json.dumps({**console_report, "outputs": {
        key: str(value) for key, value in paths.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["gate_status"] == \
        "PASS_PILOT_SHARE_CAPITAL_GATE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
