#!/usr/bin/env python3
"""Audit PIT input availability for value, quality and investment themes.

This script measures whether accounting inputs can be constructed.  It does
not calculate returns, optimize weights, or treat current statement snapshots
as complete historical revision archives.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd


THEME_COMPONENTS = {
    "value": ("ttm_parent_net_profit", "latest_parent_equity"),
    "quality": (
        "ttm_revenue", "ttm_operating_cost", "ttm_parent_net_profit",
        "ttm_operating_cash_flow", "latest_total_assets",
        "latest_total_liabilities", "latest_parent_equity",
    ),
    "investment": ("ttm_capital_expenditure_cash", "annual_asset_growth"),
}


def _read_jsonl(path):
    with Path(path).open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def _eligible(rows, symbol, asof):
    cutoff = date.fromisoformat(str(asof)[:10])
    selected = {}
    for row in rows:
        if row["symbol"] != symbol:
            continue
        # Date-only disclosures become usable at the following session; a
        # strict '<' reproduces that conservative boundary for daily audits.
        if date.fromisoformat(row["announcement"][:10]) >= cutoff:
            continue
        key = (row["statement_type"], row["report_period"], row["raw_field"])
        previous = selected.get(key)
        if previous is None or (row["announcement"], row["revision_id"]) > (
                previous["announcement"], previous["revision_id"]):
            selected[key] = row
    return tuple(selected.values())


def _single_quarters(rows, statement, field):
    values = defaultdict(dict)
    for row in rows:
        if row["statement_type"] != statement or row["raw_field"] != field:
            continue
        period = date.fromisoformat(row["report_period"])
        kind = {(3, 31): "Q1", (6, 30): "H1", (9, 30): "Q3",
                (12, 31): "FY"}.get((period.month, period.day))
        if kind:
            values[period.year][kind] = float(row["raw_value"]) * float(
                row.get("unit_scale", 1.0))
    quarters = {}
    for year, annual in values.items():
        previous = 0.0
        complete = True
        for index, kind in enumerate(("Q1", "H1", "Q3", "FY")):
            ordinal = year * 4 + index
            cumulative = annual.get(kind)
            if cumulative is None or not complete:
                quarters[ordinal] = None
                complete = False
            else:
                quarters[ordinal] = cumulative - previous
                previous = cumulative
    return quarters


def _ttm_available(rows, statement, field):
    quarters = _single_quarters(rows, statement, field)
    valid = [key for key, value in quarters.items() if value is not None]
    if not valid:
        return False
    end = max(valid)
    return all(quarters.get(key) is not None
               for key in range(end - 3, end + 1))


def _latest_available(rows, field):
    return any(row["raw_field"] == field for row in rows)


def _annual_asset_growth_available(rows):
    years = {row["report_period"]
             for row in rows
             if row["raw_field"] == "TOTAL_ASSETS" and
             row["report_period"].endswith("12-31")}
    if len(years) < 2:
        return False
    ordered = sorted(date.fromisoformat(value).year for value in years)
    return any(right == left + 1
               for left, right in zip(ordered[:-1], ordered[1:]))


def component_flags(rows):
    return {
        "ttm_parent_net_profit": _ttm_available(
            rows, "income", "PARENT_NETPROFIT"),
        "ttm_revenue": _ttm_available(
            rows, "income", "TOTAL_OPERATE_INCOME"),
        "ttm_operating_cost": _ttm_available(
            rows, "income", "OPERATE_COST"),
        "ttm_operating_cash_flow": _ttm_available(
            rows, "cashflow", "NETCASH_OPERATE"),
        "ttm_capital_expenditure_cash": _ttm_available(
            rows, "cashflow", "CONSTRUCT_LONG_ASSET"),
        "latest_total_assets": _latest_available(rows, "TOTAL_ASSETS"),
        "latest_total_liabilities": _latest_available(
            rows, "TOTAL_LIABILITIES"),
        "latest_parent_equity": _latest_available(
            rows, "TOTAL_PARENT_EQUITY"),
        "annual_asset_growth": _annual_asset_growth_available(rows),
    }


def _lifecycle_eligible(metadata, asof):
    if not metadata:
        return True
    current = date.fromisoformat(str(asof)[:10])
    listed = metadata.get("list_date")
    delisted = metadata.get("delist_date")
    return ((not listed or date.fromisoformat(str(listed)[:10]) <= current) and
            (not delisted or current <= date.fromisoformat(
                str(delisted)[:10])))


def build_audit(facts_path, symbols_path, evaluation_dates,
                trading_sessions=None, minimum_history_sessions=0):
    facts = _read_jsonl(facts_path)
    frozen = json.loads(Path(symbols_path).read_text(encoding="utf-8"))
    symbols = frozen["symbols"]
    metadata = {item["symbol"]: item for item in frozen.get("records", [])}
    sessions = tuple(sorted(
        date.fromisoformat(str(value)[:10]) for value in
        (trading_sessions or ())))
    rows, component_counts, theme_counts = [], defaultdict(int), defaultdict(int)
    lifecycle_counts, eligible_counts = defaultdict(int), defaultdict(int)
    for asof in evaluation_dates:
        for symbol in symbols:
            eligible = _lifecycle_eligible(metadata.get(symbol), asof)
            listed = (metadata.get(symbol) or {}).get("list_date")
            history_count = (sum(
                date.fromisoformat(str(listed)[:10]) <= session <=
                date.fromisoformat(str(asof)[:10]) for session in sessions)
                if listed and sessions else None)
            selection_eligible = eligible and (
                history_count is None or
                history_count >= int(minimum_history_sessions))
            flags = component_flags(_eligible(facts, symbol, asof))
            themes = {theme: all(flags[name] for name in names)
                      for theme, names in THEME_COMPONENTS.items()}
            rows.append({"date": asof, "symbol": symbol,
                         "lifecycle_eligible": eligible,
                         "history_session_count": history_count,
                         "selection_history_eligible": selection_eligible,
                         "components": flags, "themes": themes})
            lifecycle_counts[asof] += int(eligible)
            if not selection_eligible:
                continue
            eligible_counts[asof] += 1
            for name, value in flags.items():
                component_counts[(asof, name)] += int(value)
            for name, value in themes.items():
                theme_counts[(asof, name)] += int(value)
    by_date = []
    for asof in evaluation_dates:
        denominator = eligible_counts[asof]
        by_date.append({
            "date": asof, "frozen_symbol_count": len(symbols),
            "lifecycle_eligible_symbol_count": lifecycle_counts[asof],
            "selection_history_eligible_symbol_count": denominator,
            "component_coverage": {
                name: component_counts[(asof, name)] / denominator
                if denominator else 0
                for name in sorted(component_flags(()))},
            "theme_coverage": {
                name: theme_counts[(asof, name)] / denominator
                if denominator else 0
                for name in sorted(THEME_COMPONENTS)},
        })
    incomplete = sum(not row.get("revision_history_complete", False)
                     for row in facts)
    return {
        "frozen_symbol_count": len(symbols), "fact_value_count": len(facts),
        "evaluation_dates": list(evaluation_dates),
        "minimum_history_sessions": int(minimum_history_sessions),
        "theme_definition": {
            key: list(value) for key, value in THEME_COMPONENTS.items()},
        "daily_availability": by_date,
        "security_date_details": rows,
        "market_cap_dependency": {
            "value": "requires separately audited total market cap",
        },
        "revision_history_complete": incomplete == 0,
        "revision_gate_status": (
            "PASS_HISTORICAL_REVISION_VERSIONS" if not incomplete else
            "BLOCKED_HISTORICAL_REVISION_VERSIONS"),
        "interpretation": (
            "Availability only; no factor returns or parameter search. "
            "Date-only disclosures are admitted strictly after announcement."
        ),
        "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--symbols", type=Path, required=True)
    parser.add_argument("--evaluation-dates", nargs="+", default=(
        "2025-01-02", "2025-12-31", "2026-09-30"))
    parser.add_argument("--trading-sessions", type=Path, default=Path(
        "/Users/wjy/abu/data/csv/sh000300_20061002_20261002"))
    parser.add_argument("--minimum-history-sessions", type=int, default=252)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite theme availability audit")
    sessions = pd.read_csv(args.trading_sessions, usecols=["date_time"])
    report = build_audit(
        args.facts, args.symbols, args.evaluation_dates,
        trading_sessions=sessions.date_time.tolist(),
        minimum_history_sessions=args.minimum_history_sessions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
