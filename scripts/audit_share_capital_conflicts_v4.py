#!/usr/bin/env python3
"""Summarize share-capital source conflicts into auditable intervals."""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


REASONS = (
    "FLOAT_PRIMARY_VS_DAILY",
    "TOTAL_PRIMARY_VS_BALANCE",
    "FLOAT_EXCEEDS_TOTAL",
)


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(
        encoding="utf-8").splitlines() if line.strip()]


def _difference_key(reason):
    return ("float_relative_difference" if reason.startswith("FLOAT_")
            else "total_relative_difference")


def _summary(values):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return {"median": None, "p90": None, "maximum": None}
    return {
        "median": statistics.median(values),
        "p90": values[int(0.9 * (len(values) - 1))],
        "maximum": values[-1],
    }


def _close_interval(intervals, symbol, reason, rows):
    if not rows:
        return
    differences = [row.get(_difference_key(reason)) for row in rows]
    intervals.append({
        "symbol": symbol,
        "reason": reason,
        "start_date": rows[0]["date"],
        "end_date": rows[-1]["date"],
        "security_days": len(rows),
        "difference": _summary(differences),
        "first_observation": {
            key: rows[0].get(key) for key in (
                "primary_total_shares", "balance_total_shares",
                "primary_float_shares", "daily_float_shares_observed",
                "primary_event_date", "balance_report_period",
            )
        },
        "last_observation": {
            key: rows[-1].get(key) for key in (
                "primary_total_shares", "balance_total_shares",
                "primary_float_shares", "daily_float_shares_observed",
                "primary_event_date", "balance_report_period",
            )
        },
    })


def build_audit(rows):
    ordered = sorted(rows, key=lambda row: (row["symbol"], row["date"]))
    by_symbol = defaultdict(list)
    for row in ordered:
        by_symbol[row["symbol"]].append(row)
    intervals = []
    for symbol, symbol_rows in sorted(by_symbol.items()):
        active = {reason: [] for reason in REASONS}
        for row in symbol_rows:
            present = set(row.get("conflict_reasons") or [])
            for reason in REASONS:
                if reason in present:
                    active[reason].append(row)
                else:
                    _close_interval(
                        intervals, symbol, reason, active[reason])
                    active[reason] = []
        for reason in REASONS:
            _close_interval(intervals, symbol, reason, active[reason])

    reason_reports = {}
    for reason in REASONS:
        conflict_rows = [row for row in ordered
                         if reason in (row.get("conflict_reasons") or [])]
        reason_intervals = [item for item in intervals
                            if item["reason"] == reason]
        reason_reports[reason] = {
            "security_days": len(conflict_rows),
            "symbol_count": len({row["symbol"] for row in conflict_rows}),
            "interval_count": len(reason_intervals),
            "intervals_at_least_20_days": sum(
                item["security_days"] >= 20 for item in reason_intervals),
            "difference": _summary(
                row.get(_difference_key(reason)) for row in conflict_rows),
        }

    conflict_rows = [row for row in ordered
                     if row.get("conflict_reasons")]
    missing = Counter(row.get("missing_reason") for row in ordered
                      if row.get("missing_reason"))
    top_intervals = sorted(intervals, key=lambda item: (
        -item["security_days"], item["symbol"], item["reason"]))[:100]
    return {
        "input_security_days": len(ordered),
        "conflict_security_days": len(conflict_rows),
        "conflict_symbol_count": len({row["symbol"] for row in conflict_rows}),
        "overlapping_conflict_days": sum(
            len(row.get("conflict_reasons") or []) > 1 for row in ordered),
        "reason_summary": reason_reports,
        "missing_reason_counts": dict(sorted(missing.items())),
        "interval_count": len(intervals),
        "top_intervals": top_intervals,
        "status": "CONFLICTS_REQUIRE_SOURCE_AUDIT" if conflict_rows
        else "NO_SHARE_SOURCE_CONFLICTS",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite share conflict audit")
    report = build_audit(read_jsonl(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    console = {key: value for key, value in report.items()
               if key != "top_intervals"}
    print(json.dumps(console, ensure_ascii=False, indent=2, sort_keys=True))
    return 2 if report["conflict_security_days"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
