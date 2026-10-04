#!/usr/bin/env python3
"""Audit Baostock as an auxiliary share-capital and historical-ST source."""
from __future__ import annotations

import argparse
import bisect
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(
        encoding="utf-8").splitlines() if line.strip()]


def relative_difference(left, right):
    if left is None or right is None:
        return None
    left, right = float(left), float(right)
    return abs(left - right) / max(abs(left), abs(right))


def distribution(values):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return {"count": 0, "median": None, "p90": None, "maximum": None}
    return {
        "count": len(values),
        "median": statistics.median(values),
        "p90": values[int(0.9 * (len(values) - 1))],
        "maximum": values[-1],
    }


def build_audit(baostock_rows, capital_rows, tolerance=0.005,
                coverage_min=0.995):
    tolerance = float(tolerance)
    exact = {(row["symbol"], int(row["date"])): row
             for row in baostock_rows}
    derived = defaultdict(list)
    for row in baostock_rows:
        if row.get("derived_float_shares") is not None:
            derived[row["symbol"]].append((
                int(row["date"]), float(row["derived_float_shares"])))
    for values in derived.values():
        values.sort()

    def asof(symbol, day):
        values = derived.get(symbol, [])
        index = bisect.bisect_right(
            values, (int(day), float("inf"))) - 1
        return values[index][1] if index >= 0 else None

    st_exact = 0
    share_asof = 0
    baostock_vs_daily = []
    baostock_vs_primary = []
    arbitration = Counter()
    missing_primary_float_filled = Counter()
    missing_float_evidence = defaultdict(lambda: {
        "days": 0,
        "comparable_days": 0,
        "within_tolerance_days": 0,
        "above_total_days": 0,
        "differences": [],
    })
    conflict_examples = {}
    for row in capital_rows:
        symbol, day = row["symbol"], int(row["date"])
        if (symbol, day) in exact:
            st_exact += 1
        auxiliary = asof(symbol, day)
        if auxiliary is not None:
            share_asof += 1
        daily = row.get("daily_float_shares_observed")
        primary = row.get("primary_float_shares")
        daily_difference = relative_difference(auxiliary, daily)
        primary_difference = relative_difference(auxiliary, primary)
        baostock_vs_daily.append(daily_difference)
        baostock_vs_primary.append(primary_difference)
        if row.get("float_shares") is None and auxiliary is not None:
            missing_primary_float_filled[symbol] += 1
            evidence = missing_float_evidence[symbol]
            evidence["days"] += 1
            balance_total = row.get("balance_total_shares")
            balance_difference = relative_difference(auxiliary, balance_total)
            if balance_difference is not None:
                evidence["comparable_days"] += 1
                evidence["differences"].append(balance_difference)
                if balance_difference <= tolerance:
                    evidence["within_tolerance_days"] += 1
                if float(auxiliary) > float(balance_total):
                    evidence["above_total_days"] += 1
        if "FLOAT_PRIMARY_VS_DAILY" not in (
                row.get("conflict_reasons") or []):
            continue
        matches_daily = (daily_difference is not None and
                         daily_difference <= tolerance)
        matches_primary = (primary_difference is not None and
                           primary_difference <= tolerance)
        if matches_daily and matches_primary:
            label = "matches_both"
        elif matches_primary:
            label = "supports_primary"
        elif matches_daily:
            label = "supports_daily"
        elif auxiliary is None:
            label = "missing_auxiliary"
        else:
            label = "matches_neither"
        arbitration[label] += 1
        conflict_examples.setdefault(label, {
            "symbol": symbol, "date": day,
            "primary_float_shares": primary,
            "daily_float_shares": daily,
            "baostock_derived_float_shares": auxiliary,
            "baostock_vs_primary": primary_difference,
            "baostock_vs_daily": daily_difference,
        })

    denominator = len(capital_rows)
    st_coverage = st_exact / denominator if denominator else 0.0
    share_coverage = share_asof / denominator if denominator else 0.0
    st_counts = Counter(int(row["is_st"]) for row in baostock_rows)
    st_symbols = sorted({row["symbol"] for row in baostock_rows
                         if int(row["is_st"]) == 1})
    missing_float_report = {}
    for symbol, evidence in sorted(missing_float_evidence.items()):
        comparable = evidence["comparable_days"]
        missing_float_report[symbol] = {
            "days": evidence["days"],
            "comparable_days": comparable,
            "within_tolerance_days": evidence["within_tolerance_days"],
            "within_tolerance_rate": (
                evidence["within_tolerance_days"] / comparable
                if comparable else None),
            "above_total_days": evidence["above_total_days"],
            "difference_vs_balance_total": distribution(
                evidence["differences"]),
            "decision": "AUDIT_ONLY_APPROXIMATE_NOT_MATERIALIZED",
        }
    return {
        "capital_security_days": denominator,
        "baostock_security_days": len(baostock_rows),
        "exact_st_security_days": st_exact,
        "exact_st_coverage": st_coverage,
        "asof_derived_float_security_days": share_asof,
        "asof_derived_float_coverage": share_coverage,
        "coverage_threshold": float(coverage_min),
        "coverage_gate": "PASS_PILOT_AUXILIARY_COVERAGE" if
        st_coverage >= coverage_min and share_coverage >= coverage_min else
        "BLOCKED_PILOT_AUXILIARY_COVERAGE",
        "baostock_vs_daily_float": distribution(baostock_vs_daily),
        "baostock_vs_primary_float": distribution(baostock_vs_primary),
        "existing_float_conflict_arbitration": dict(sorted(
            arbitration.items())),
        "conflict_examples": conflict_examples,
        "missing_primary_float_days_fillable": dict(sorted(
            missing_primary_float_filled.items())),
        "missing_float_fallback_evidence": missing_float_report,
        "st_value_counts": dict(sorted(st_counts.items())),
        "symbols_ever_st": st_symbols,
        "authority_status": "MIXED_SOURCE_EVIDENCE_NO_OVERRIDE",
        "limitations": [
            "historical_revision_versions_unavailable",
            "turnover_rounding_introduces_derived_share_noise",
            "derived_float_may_exceed_total_shares_due_to_rounding",
            "pilot_coverage_does_not_prove_full_universe_coverage",
            "auxiliary_source_must_not_override_primary_without_policy_review",
        ],
        "status": "AUDITED_AUXILIARY_PILOT_RESEARCH_ONLY",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baostock_daily", type=Path)
    parser.add_argument("capital_daily", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite Baostock audit")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    rules = config["strict_rules"]
    report = build_audit(
        read_jsonl(args.baostock_daily), read_jsonl(args.capital_daily),
        tolerance=rules["share_relative_tolerance"],
        coverage_min=min(rules["st_security_day_coverage_min"],
                         rules["derived_float_security_day_coverage_min"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
