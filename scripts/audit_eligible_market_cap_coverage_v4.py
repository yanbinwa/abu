#!/usr/bin/env python3
"""Audit market-cap coverage after applying PIT ST and price eligibility."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = (
    ROOT / "configs/selection/fundamental_structured_pit_v4.json"
)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_positive(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric) and numeric > 0


def _coverage(covered, denominator):
    return covered / denominator if denominator else 0.0


def build_audit(capital_rows, st_panel_path, config):
    with np.load(Path(st_panel_path), allow_pickle=False) as payload:
        role = payload["source_role"].astype(str).tolist()
        if role != ["st_exclusion_only"]:
            raise ValueError("ST panel is not exclusion-only")
        dates = payload["dates"].astype(np.int64)
        symbols = payload["symbols"].astype(str)
        known = payload["known"].astype(bool)
        is_st = payload["is_st"].astype(bool)
    if known.shape != (len(dates), len(symbols)) or is_st.shape != known.shape:
        raise ValueError("ST panel shape mismatch")
    if np.any(is_st & ~known):
        raise ValueError("ST panel marks unknown state as ST")
    date_index = {int(day): index for index, day in enumerate(dates)}
    symbol_index = {symbol: index for index, symbol in enumerate(symbols)}

    raw = Counter()
    eligible = Counter()
    exclusions = Counter()
    eligible_conflict_reasons = Counter()
    daily = defaultdict(Counter)
    exchange = defaultdict(Counter)
    missing_examples = []
    symbols_seen = set()
    for row in capital_rows:
        symbol = str(row["symbol"])
        day = int(row["date"])
        symbols_seen.add(symbol)
        raw["security_days"] += 1
        raw["total_covered"] += int(
            _valid_positive(row.get("total_market_cap")))
        raw["float_covered"] += int(
            _valid_positive(row.get("float_market_cap")))
        raw["conflict_rows"] += int(
            row.get("reconciliation_status") == "CONFLICT")

        reasons = []
        day_position = date_index.get(day)
        symbol_position = symbol_index.get(symbol)
        if day_position is None or symbol_position is None:
            reasons.append("OUTSIDE_ST_PANEL")
        else:
            if not known[day_position, symbol_position]:
                reasons.append("UNKNOWN_ST_STATUS")
            elif is_st[day_position, symbol_position]:
                reasons.append("KNOWN_ST")
        if not _valid_positive(row.get("raw_close")):
            reasons.append("MISSING_RAW_CLOSE")
        if reasons:
            for reason in reasons:
                exclusions[reason] += 1
            continue

        total_valid = _valid_positive(row.get("total_market_cap"))
        float_valid = _valid_positive(row.get("float_market_cap"))
        conflict = row.get("reconciliation_status") == "CONFLICT"
        eligible["security_days"] += 1
        eligible["total_covered"] += int(total_valid)
        eligible["float_covered"] += int(float_valid)
        eligible["conflict_rows"] += int(conflict)
        daily[day]["security_days"] += 1
        daily[day]["total_covered"] += int(total_valid)
        daily[day]["float_covered"] += int(float_valid)
        daily[day]["conflict_rows"] += int(conflict)
        exchange[symbol[:2]]["security_days"] += 1
        exchange[symbol[:2]]["total_covered"] += int(total_valid)
        exchange[symbol[:2]]["float_covered"] += int(float_valid)
        exchange[symbol[:2]]["conflict_rows"] += int(conflict)
        if conflict:
            for reason in row.get("conflict_reasons") or []:
                eligible_conflict_reasons[reason] += 1
        if (not total_valid or not float_valid) and len(missing_examples) < 100:
            missing_examples.append({
                "symbol": symbol, "date": day,
                "total_market_cap_covered": total_valid,
                "float_market_cap_covered": float_valid,
                "reconciliation_status": row.get("reconciliation_status"),
            })

    denominator = eligible["security_days"]
    total_coverage = _coverage(eligible["total_covered"], denominator)
    float_coverage = _coverage(eligible["float_covered"], denominator)
    rules = config["strict_rules"]
    coverage_blockers = []
    if total_coverage < float(rules["total_market_cap_coverage_min"]):
        coverage_blockers.append("eligible_total_market_cap_coverage")
    if float_coverage < float(rules["float_market_cap_coverage_min"]):
        coverage_blockers.append("eligible_float_market_cap_coverage")

    def summarized(counts):
        count = counts["security_days"]
        return {
            "security_days": count,
            "total_market_cap_coverage": _coverage(
                counts["total_covered"], count),
            "float_market_cap_coverage": _coverage(
                counts["float_covered"], count),
            "conflict_rows": counts["conflict_rows"],
        }

    daily_report = []
    for day, counts in sorted(daily.items()):
        daily_report.append({"date": day, **summarized(counts)})
    return {
        "scope": "capital_rows_after_pit_st_and_raw_price_eligibility",
        "symbol_count": len(symbols_seen),
        "raw_all_price_days": summarized(raw),
        "eligible_non_st_price_days": summarized(eligible),
        "exclusion_reason_counts": dict(sorted(exclusions.items())),
        "exchange_eligible_coverage": {
            key: summarized(value) for key, value in sorted(exchange.items())
        },
        "daily_eligible_coverage": daily_report,
        "eligible_missing_examples": missing_examples,
        "eligible_conflict_reason_counts": dict(sorted(
            eligible_conflict_reasons.items())),
        "coverage_thresholds": {
            "total": float(rules["total_market_cap_coverage_min"]),
            "float": float(rules["float_market_cap_coverage_min"]),
        },
        "coverage_blockers": coverage_blockers,
        "coverage_gate_status": (
            "PASS_ELIGIBLE_MARKET_CAP_COVERAGE" if not coverage_blockers
            else "BLOCKED_ELIGIBLE_MARKET_CAP_COVERAGE"),
        "reconciliation_gate_status": (
            "PASS_SHARE_RECONCILIATION" if not eligible["conflict_rows"]
            else "BLOCKED_SHARE_RECONCILIATION"),
        "selection_semantics": (
            "known ST excluded; unknown ST excluded; missing raw close excluded"),
        "research_label": config["research_label"],
    }


def read_jsonl(path):
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capital_daily", type=Path)
    parser.add_argument("--st-panel", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite eligible-cap audit")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = build_audit(
        read_jsonl(args.capital_daily), args.st_panel, config)
    report["inputs"] = {
        "capital_daily": str(args.capital_daily),
        "capital_daily_sha256": sha256_file(args.capital_daily),
        "st_panel": str(args.st_panel),
        "st_panel_sha256": sha256_file(args.st_panel),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    console = {key: value for key, value in report.items()
               if key != "daily_eligible_coverage"}
    print(json.dumps(console, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["coverage_gate_status"].startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
