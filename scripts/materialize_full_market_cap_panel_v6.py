#!/usr/bin/env python3
"""Materialize PIT market cap with official-first float-share arbitration."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    structured_balance_total_share_events,
)
from scripts.collect_structured_fundamental_v4 import (  # noqa: E402
    trading_sessions,
)

DEFAULT_CONFIG = ROOT / "configs/selection/full_market_cap_panel_v6.json"
DEFAULT_RESEARCH = Path("/Users/wjy/abu/data/selection_research")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _positive(value):
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) and numeric > 0 else None


def _load_events(path, symbols):
    grouped = defaultdict(list)
    seen_keys = set()
    duplicate_keys = 0
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            symbol = str(row["symbol"])
            if symbol not in symbols:
                raise ValueError("share event outside panel: " + symbol)
            key = str(row["source_key"])
            duplicate_keys += int(key in seen_keys)
            seen_keys.add(key)
            effective = int(str(row["effective_at"])[:10].replace("-", ""))
            economic = int(str(row.get("event_date") or
                               row["effective_at"][:10]).replace("-", ""))
            total = _positive(row.get("total_shares"))
            floated = _positive(row.get("float_shares"))
            if total is None or floated is None or floated > total:
                raise ValueError("invalid normalized share event: " + key)
            grouped[symbol].append((
                effective, economic, str(row["effective_at"]), key,
                total, floated))
    if duplicate_keys:
        raise ValueError("duplicate share event source keys")
    for values in grouped.values():
        values.sort(key=lambda item: (item[0], item[2], item[3]))
    return grouped, len(seen_keys)


def _asof_states(events, days):
    """Match ShareCapitalStore semantics without an O(days*events) scan."""
    values = np.full((len(days), 2), np.nan, dtype=np.float64)
    cursor = 0
    best = None
    for position, day in enumerate(days):
        while cursor < len(events) and events[cursor][0] <= int(day):
            candidate = events[cursor]
            state_key = (candidate[1], candidate[2], candidate[3])
            if best is None or state_key > best[0]:
                best = (state_key, candidate[4], candidate[5])
            cursor += 1
        if best is not None:
            values[position] = best[1], best[2]
    return values


def _load_balance_states(path, config, symbols):
    if path is None:
        return {}, {"input_records": 0, "state_transitions": 0}
    records = []
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("raw_field") != "SHARE_CAPITAL":
                continue
            if row.get("symbol") not in symbols:
                raise ValueError("balance fact outside panel")
            records.append(row)
    events, audit = structured_balance_total_share_events(
        records, trading_sessions(config["trading_sessions_source"]))
    grouped = defaultdict(list)
    for event in events:
        effective = int(event.effective_at[:10].replace("-", ""))
        economic = int((event.report_period or
                        event.effective_at[:10]).replace("-", ""))
        grouped[event.symbol].append((
            effective, economic, event.effective_at, event.source_key,
            float(event.total_shares), np.nan))
    for values in grouped.values():
        values.sort(key=lambda item: (item[0], item[2], item[3]))
    return grouped, audit


def _load_baostock_states(path, symbols):
    grouped = defaultdict(list)
    if path is None:
        return grouped, 0
    count = 0
    seen = set()
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            row = json.loads(line)
            symbol = str(row["symbol"])
            if symbol not in symbols:
                raise ValueError("BaoStock fallback outside panel: " + symbol)
            if row.get("source_role") != "float_share_gap_fallback_only":
                raise ValueError("invalid BaoStock fallback source role")
            row_key = (symbol, int(row["date"]))
            if row_key in seen:
                raise ValueError("duplicate BaoStock fallback security-day")
            seen.add(row_key)
            floated = _positive(row.get("derived_float_shares"))
            if floated is None:
                continue
            day = row_key[1]
            key = "baostock:{}:{}".format(symbol, day)
            grouped[symbol].append((
                day, day, str(day), key, np.nan, floated))
            count += 1
    for values in grouped.values():
        values.sort(key=lambda item: (item[0], item[2], item[3]))
    return grouped, count


def materialize(events_path, research_dir, st_panel_path, output_dir, config,
                balance_facts_path=None, baostock_fallback_path=None):
    events_path = Path(events_path)
    research_dir = Path(research_dir)
    st_panel_path = Path(st_panel_path)
    output_dir = Path(output_dir)
    expected_priority = [
        "cninfo_share_change", "local_daily",
        "baostock_turnover_derived",
    ]
    if config.get("float_share_priority", expected_priority) != \
            expected_priority:
        raise ValueError("unsupported float-share source priority")
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite full market-cap panel")
    with np.load(st_panel_path, allow_pickle=False) as payload:
        dates = payload["dates"].astype(np.int64)
        symbols = payload["symbols"].astype(str)
        known = payload["known"].astype(bool)
        is_st = payload["is_st"].astype(bool)
        role = payload["source_role"].astype(str).tolist()
    if role != ["st_exclusion_only"]:
        raise ValueError("ST panel is not exclusion-only")
    shape = (len(dates), len(symbols))
    if known.shape != shape or is_st.shape != shape:
        raise ValueError("ST panel shape mismatch")
    if len(set(symbols)) != len(symbols) or len(set(dates)) != len(dates):
        raise ValueError("duplicate panel axis")
    grouped, event_count = _load_events(events_path, set(symbols))
    balance_grouped, balance_audit = _load_balance_states(
        balance_facts_path, config, set(symbols))
    baostock_grouped, baostock_row_count = _load_baostock_states(
        baostock_fallback_path, set(symbols))
    date_index = {int(day): index for index, day in enumerate(dates)}
    total_cap = np.full(shape, np.nan, dtype=np.float64)
    float_cap = np.full(shape, np.nan, dtype=np.float64)
    source_code = np.zeros(shape, dtype=np.uint8)
    float_source_code = np.zeros(shape, dtype=np.uint8)
    eligible_mask = np.zeros(shape, dtype=bool)
    local_float_known = np.zeros(shape, dtype=bool)
    float_conflict = np.zeros(shape, dtype=bool)
    total_conflict = np.zeros(shape, dtype=bool)
    raw = Counter()
    eligible = Counter()
    exclusions = Counter()
    exchanges = defaultdict(Counter)
    daily = defaultdict(Counter)
    missing_by_symbol = Counter()
    conflict_by_symbol = Counter()
    conflict_examples = []
    missing_examples = []
    rejected_fallback_examples = []
    float_source_counts = Counter()
    tolerance = float(config["strict_rules"][
        "share_reconciliation_relative_tolerance"])

    for column, symbol in enumerate(symbols):
        states = _asof_states(grouped.get(symbol, []), dates)
        balance_states = _asof_states(
            balance_grouped.get(symbol, []), dates)
        baostock_states = _asof_states(
            baostock_grouped.get(symbol, []), dates)
        raw_path = research_dir / "raw" / (symbol + ".csv")
        if not raw_path.is_file():
            exclusions["NO_LOCAL_PRICE_FILE"] += 1
            continue
        bars = pd.read_csv(raw_path, usecols=lambda name: name in {
            "date", "close", "outstanding_share"})
        for row in bars.itertuples(index=False):
            day = int(row.date)
            position = date_index.get(day)
            if position is None:
                continue
            close = _positive(getattr(row, "close", None))
            local_float = _positive(getattr(row, "outstanding_share", None))
            primary_total = _positive(states[position, 0])
            primary_float = _positive(states[position, 1])
            balance_total = _positive(balance_states[position, 0])
            baostock_float = _positive(baostock_states[position, 1])
            selected_total = (primary_total if primary_total is not None
                              else balance_total)
            selected_float = primary_float
            selected_float_source = "cninfo_share_change"
            if selected_float is None and local_float is not None:
                selected_float = local_float
                selected_float_source = "local_daily"
            fallback_rejected = False
            if selected_float is None and baostock_float is not None:
                fallback_rejected = bool(
                    selected_total is not None and
                    baostock_float > selected_total * (1.0 + tolerance))
                if not fallback_rejected:
                    selected_float = baostock_float
                    selected_float_source = "baostock_turnover_derived"
                elif len(rejected_fallback_examples) < 100:
                    rejected_fallback_examples.append({
                        "symbol": symbol, "date": day,
                        "baostock_derived_float_shares": baostock_float,
                        "selected_total_shares": selected_total,
                    })
            raw["security_days"] += 1
            raw["total_covered"] += int(
                close is not None and selected_total is not None)
            raw["float_covered"] += int(
                close is not None and local_float is not None)
            if close is None:
                exclusions["MISSING_RAW_CLOSE"] += 1
                continue
            if not known[position, column]:
                exclusions["UNKNOWN_ST_STATUS"] += 1
                continue
            if is_st[position, column]:
                exclusions["KNOWN_ST"] += 1
                continue
            total_valid = selected_total is not None
            float_valid = selected_float is not None
            eligible_mask[position, column] = True
            local_float_known[position, column] = float_valid
            conflict = False
            if primary_float is not None and local_float is not None:
                denominator = max(primary_float, local_float)
                conflict = abs(primary_float - local_float) / denominator > \
                    tolerance
            total_disagreement = False
            if primary_total is not None and balance_total is not None:
                denominator = max(primary_total, balance_total)
                total_disagreement = abs(
                    primary_total - balance_total) / denominator > tolerance
            eligible["security_days"] += 1
            eligible["total_covered"] += int(total_valid)
            eligible["float_covered"] += int(float_valid)
            eligible["conflict_rows"] += int(conflict)
            daily[day]["security_days"] += 1
            daily[day]["total_covered"] += int(total_valid)
            daily[day]["float_covered"] += int(float_valid)
            daily[day]["conflict_rows"] += int(conflict)
            exchange = symbol[:2]
            exchanges[exchange]["security_days"] += 1
            exchanges[exchange]["total_covered"] += int(total_valid)
            exchanges[exchange]["float_covered"] += int(float_valid)
            exchanges[exchange]["conflict_rows"] += int(conflict)
            if total_valid:
                total_cap[position, column] = close * selected_total
                source_code[position, column] = (
                    1 if primary_total is not None else 2)
            else:
                missing_by_symbol[symbol] += 1
                if len(missing_examples) < 100:
                    missing_examples.append({"symbol": symbol, "date": day})
            if float_valid:
                float_cap[position, column] = close * selected_float
                code = {
                    "local_daily": 1,
                    "cninfo_share_change": 2,
                    "baostock_turnover_derived": 3,
                }[selected_float_source]
                float_source_code[position, column] = code
                float_source_counts[selected_float_source] += 1
            else:
                float_source_counts["missing"] += 1
                if fallback_rejected:
                    float_source_counts["baostock_rejected_above_total"] += 1
            if conflict:
                float_conflict[position, column] = True
                conflict_by_symbol[symbol] += 1
                if len(conflict_examples) < 100:
                    conflict_examples.append({
                        "symbol": symbol, "date": day,
                        "primary_float_shares": primary_float,
                        "local_float_shares": local_float,
                    })
            if total_disagreement:
                total_conflict[position, column] = True

    def summarized(counts):
        denominator = counts["security_days"]
        return {
            "security_days": denominator,
            "total_market_cap_coverage": (
                counts["total_covered"] / denominator if denominator else 0),
            "float_market_cap_coverage": (
                counts["float_covered"] / denominator if denominator else 0),
            "float_share_conflict_rows": counts["conflict_rows"],
        }

    eligible_summary = summarized(eligible)
    rules = config["strict_rules"]
    coverage_blockers = []
    if eligible_summary["total_market_cap_coverage"] < float(
            rules["total_market_cap_coverage_min"]):
        coverage_blockers.append("eligible_total_market_cap_coverage")
    if eligible_summary["float_market_cap_coverage"] < float(
            rules["float_market_cap_coverage_min"]):
        coverage_blockers.append("eligible_float_market_cap_coverage")

    output_dir.mkdir(parents=True)
    panel_path = output_dir / "market_cap_panel_v6.npz"
    np.savez_compressed(
        panel_path, dates=dates.astype(np.int32), symbols=symbols,
        total_market_cap=total_cap, total_share_source_code=source_code,
        float_market_cap=float_cap,
        float_share_source_code=float_source_code,
        eligible_non_st_price_day=eligible_mask,
        local_float_share_known=local_float_known,
        selected_float_share_known=np.isfinite(float_cap),
        float_share_conflict=float_conflict,
        total_share_conflict=total_conflict,
        source_names=np.array([
            "missing", "cninfo_share_change",
            "eastmoney_balance_share_capital"]),
        float_source_names=np.array([
            "missing", "local_daily", "cninfo_share_change",
            "baostock_turnover_derived"]))
    report = {
        "config_version": config.get("config_version"),
        "float_share_priority": expected_priority,
        "scope": "full_market_pit_total_cap_after_st_exclusion",
        "symbol_count": len(symbols), "date_count": len(dates),
        "normalized_share_event_count": event_count,
        "symbols_with_share_events": len(grouped),
        "symbols_with_balance_fallback_events": len(balance_grouped),
        "symbols_with_baostock_float_fallback": len(baostock_grouped),
        "baostock_derived_float_row_count": baostock_row_count,
        "balance_event_audit": balance_audit,
        "raw_price_days": summarized(raw),
        "eligible_non_st_price_days": eligible_summary,
        "exclusion_reason_counts": dict(sorted(exclusions.items())),
        "exchange_eligible_coverage": {
            key: summarized(value) for key, value in sorted(exchanges.items())
        },
        "daily_eligible_coverage": [
            {"date": day, **summarized(value)}
            for day, value in sorted(daily.items())
        ],
        "eligible_missing_total_cap_days": sum(missing_by_symbol.values()),
        "top_missing_total_cap_symbols": [
            {"symbol": symbol, "missing_days": count}
            for symbol, count in missing_by_symbol.most_common(100)
        ],
        "eligible_missing_examples": missing_examples,
        "eligible_float_share_conflict_days": sum(
            conflict_by_symbol.values()),
        "eligible_total_share_conflict_days": int(
            np.sum(total_conflict & eligible_mask)),
        "eligible_float_share_source_counts": dict(sorted(
            float_source_counts.items())),
        "baostock_fallback_rejected_above_total_days": int(
            float_source_counts["baostock_rejected_above_total"]),
        "baostock_fallback_rejected_examples": rejected_fallback_examples,
        "top_float_share_conflict_symbols": [
            {"symbol": symbol, "conflict_days": count}
            for symbol, count in conflict_by_symbol.most_common(100)
        ],
        "float_share_conflict_examples": conflict_examples,
        "coverage_thresholds": {
            "total": float(rules["total_market_cap_coverage_min"]),
            "float": float(rules["float_market_cap_coverage_min"]),
        },
        "coverage_blockers": coverage_blockers,
        "coverage_gate_status": (
            "PASS_ELIGIBLE_MARKET_CAP_COVERAGE" if not coverage_blockers else
            "BLOCKED_ELIGIBLE_MARKET_CAP_COVERAGE"),
        "reconciliation_gate_status": (
            "PASS_SHARE_RECONCILIATION" if not conflict_by_symbol and not
            np.any(total_conflict & eligible_mask) else
            "BLOCKED_SHARE_RECONCILIATION"),
        "selection_semantics": (
            "known ST excluded; unknown ST excluded; missing raw close excluded; "
            "missing total share remains missing; float-share priority is "
            "effective-dated CNINFO, then local daily, then as-of BaoStock "
            "turnover-derived fallback; local and BaoStock never override "
            "effective-dated CNINFO"),
        "inputs": {
            "share_events": str(events_path),
            "share_events_sha256": sha256_file(events_path),
            "st_panel": str(st_panel_path),
            "st_panel_sha256": sha256_file(st_panel_path),
            "balance_facts": (str(balance_facts_path)
                              if balance_facts_path else None),
            "balance_facts_sha256": (
                sha256_file(balance_facts_path)
                if balance_facts_path else None),
            "baostock_float_fallback": (
                str(baostock_fallback_path)
                if baostock_fallback_path else None),
            "baostock_float_fallback_sha256": (
                sha256_file(baostock_fallback_path)
                if baostock_fallback_path else None),
        },
        "panel": str(panel_path),
        "panel_sha256": sha256_file(panel_path),
        "research_label": config["research_label"],
    }
    report_path = output_dir / "coverage_report.json"
    report_path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, {"panel": panel_path, "report": report_path}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("share_events", type=Path)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--st-panel", type=Path, required=True)
    parser.add_argument("--balance-facts", type=Path)
    parser.add_argument("--baostock-fallback", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report, paths = materialize(
        args.share_events, args.research_dir, args.st_panel,
        args.output_dir, config, balance_facts_path=args.balance_facts,
        baostock_fallback_path=args.baostock_fallback)
    console = {key: value for key, value in report.items()
               if key not in {"daily_eligible_coverage",
                              "float_share_conflict_examples"}}
    print(json.dumps({**console, "outputs": {
        key: str(value) for key, value in paths.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["coverage_gate_status"].startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
