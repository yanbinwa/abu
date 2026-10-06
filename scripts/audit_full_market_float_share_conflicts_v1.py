#!/usr/bin/env python3
"""Stratify full-market float-share conflicts and freeze an audit sample."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.materialize_full_market_cap_panel_v4 import (  # noqa: E402
    _asof_states, _load_events, _positive, sha256_file,
)

DEFAULT_CONFIG = (
    ROOT / "configs/selection/baostock_float_share_conflict_audit_v1.json"
)


def difference_band(value):
    value = float(value)
    if value <= .01:
        return "00_0.5_to_1pct"
    if value <= .05:
        return "01_1_to_5pct"
    if value <= .20:
        return "02_5_to_20pct"
    if value <= .50:
        return "03_20_to_50pct"
    return "04_above_50pct"


def board(symbol):
    code = symbol[2:]
    if symbol.startswith("sh688"):
        return "star"
    if symbol.startswith(("sz300", "sz301")):
        return "chinext"
    return "main"


def relative_difference(left, right):
    if left is None or right is None:
        return None
    return abs(float(left) - float(right)) / max(abs(float(left)),
                                                    abs(float(right)))


def distribution(values):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return {"count": 0, "median": None, "p90": None, "maximum": None}
    return {
        "count": len(values), "median": statistics.median(values),
        "p90": values[int(.9 * (len(values) - 1))], "maximum": values[-1],
    }


def arbitration_label(auxiliary, local, primary, tolerance):
    if auxiliary is None:
        return "missing_auxiliary"
    local_match = relative_difference(auxiliary, local) <= tolerance
    primary_match = relative_difference(auxiliary, primary) <= tolerance
    if local_match and primary_match:
        return "matches_both"
    if local_match:
        return "supports_local_daily"
    if primary_match:
        return "supports_cninfo"
    return "matches_neither"


def _load_baostock(path, symbols, expected_role):
    grouped = defaultdict(list)
    if path is None:
        return grouped, 0
    seen = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        symbol, day = str(row["symbol"]), int(row["date"])
        if symbol not in symbols:
            raise ValueError("BaoStock audit symbol outside panel: " + symbol)
        if row.get("source_role") != expected_role:
            raise ValueError("invalid BaoStock conflict-audit source role")
        if (symbol, day) in seen:
            raise ValueError("duplicate BaoStock conflict-audit security-day")
        seen.add((symbol, day))
        floated = _positive(row.get("derived_float_shares"))
        if floated is not None:
            grouped[symbol].append((
                day, day, str(day), "baostock:{}:{}".format(symbol, day),
                np.nan, floated))
    for values in grouped.values():
        values.sort(key=lambda item: item[0])
    return grouped, len(seen)


def _future_match(events, day, local, tolerance, max_days):
    effective = [item[0] for item in events]
    start = bisect.bisect_right(effective, int(day))
    observed = date.fromisoformat(str(day)[:4] + "-" + str(day)[4:6] +
                                  "-" + str(day)[6:8])
    for event in events[start:start + 8]:
        future_day = date.fromisoformat(
            str(event[0])[:4] + "-" + str(event[0])[4:6] + "-" +
            str(event[0])[6:8])
        gap = (future_day - observed).days
        if gap > max_days:
            break
        if relative_difference(local, event[5]) <= tolerance:
            return gap
    return None


def _select_sample(symbol_reports, global_top, per_stratum):
    ranked = sorted(symbol_reports, key=lambda item: (
        -item["conflict_days"], item["symbol"]))
    selected = {item["symbol"] for item in ranked[:global_top]}
    strata = defaultdict(list)
    for item in ranked:
        key = (item["board"], item["median_difference_band"],
               item["majority_direction"])
        strata[key].append(item)
    for key in sorted(strata):
        selected.update(item["symbol"] for item in strata[key][:per_stratum])
    return sorted(selected)


def audit(events_path, research_dir, st_panel_path, output_dir, config,
          baostock_path=None):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite float-share audit")
    research_dir = Path(research_dir)
    with np.load(st_panel_path, allow_pickle=False) as payload:
        dates = payload["dates"].astype(np.int64)
        symbols = payload["symbols"].astype(str)
        known = payload["known"].astype(bool)
        is_st = payload["is_st"].astype(bool)
    grouped, event_count = _load_events(events_path, set(symbols))
    baostock, baostock_rows = _load_baostock(
        baostock_path, set(symbols), "share_conflict_audit_only")
    date_index = {int(day): index for index, day in enumerate(dates)}
    tolerance = float(config["strict_rules"]["share_relative_tolerance"])
    max_future_days = int(config["strict_rules"][
        "future_event_match_max_calendar_days"])
    counts = Counter()
    by_exchange = defaultdict(Counter)
    by_board = defaultdict(Counter)
    by_year = defaultdict(Counter)
    by_band = Counter()
    by_direction = Counter()
    arbitration = Counter()
    arbitration_by_future_match = defaultdict(Counter)
    arbitration_by_band = defaultdict(Counter)
    arbitration_by_direction = defaultdict(Counter)
    arbitration_by_local_total = defaultdict(Counter)
    differences = []
    future_gaps = []
    local_vs_baostock = []
    cninfo_vs_baostock = []
    symbol_stats = defaultdict(lambda: {
        "differences": [], "directions": Counter(),
        "future_matches": 0, "local_exceeds_total": 0,
        "arbitration": Counter(),
    })
    intervals = []

    for column, symbol in enumerate(symbols):
        events = grouped.get(symbol, [])
        states = _asof_states(events, dates)
        auxiliary_states = _asof_states(baostock.get(symbol, []), dates)
        path = research_dir / "raw" / (symbol + ".csv")
        if not path.is_file():
            continue
        active = []

        def close_interval():
            if not active:
                return
            interval_diffs = [item[1] for item in active]
            intervals.append({
                "symbol": symbol, "exchange": symbol[:2],
                "board": board(symbol), "start_date": active[0][0],
                "end_date": active[-1][0], "security_days": len(active),
                "difference": distribution(interval_diffs),
                "majority_direction": Counter(
                    item[2] for item in active).most_common(1)[0][0],
                "future_cninfo_match_days": sum(
                    item[3] is not None for item in active),
            })
            active.clear()

        bars = pd.read_csv(path, usecols=lambda name: name in {
            "date", "close", "outstanding_share"})
        for row in bars.itertuples(index=False):
            day = int(row.date)
            position = date_index.get(day)
            if position is None or not known[position, column] or \
                    is_st[position, column] or \
                    _positive(getattr(row, "close", None)) is None:
                close_interval()
                continue
            local = _positive(getattr(row, "outstanding_share", None))
            primary = _positive(states[position, 1])
            if local is None or primary is None:
                close_interval()
                continue
            counts["comparable_days"] += 1
            difference = relative_difference(local, primary)
            if difference <= tolerance:
                close_interval()
                continue
            direction = "local_above_cninfo" if local > primary else \
                "cninfo_above_local"
            band = difference_band(difference)
            future_gap = _future_match(
                events, day, local, tolerance, max_future_days)
            total = _positive(states[position, 0])
            local_exceeds_total = bool(
                total is not None and local > total * (1.0 + tolerance))
            auxiliary = _positive(auxiliary_states[position, 1])
            label = arbitration_label(
                auxiliary, local, primary, tolerance)
            counts["conflict_days"] += 1
            counts["local_exceeds_cninfo_total_days"] += local_exceeds_total
            counts["future_cninfo_match_days"] += future_gap is not None
            differences.append(difference)
            if future_gap is not None:
                future_gaps.append(future_gap)
            by_exchange[symbol[:2]]["days"] += 1
            by_board[board(symbol)]["days"] += 1
            by_year[str(day)[:4]]["days"] += 1
            by_band[band] += 1
            by_direction[direction] += 1
            arbitration[label] += 1
            arbitration_by_future_match[
                "future_match" if future_gap is not None else
                "no_future_match"][label] += 1
            arbitration_by_band[band][label] += 1
            arbitration_by_direction[direction][label] += 1
            arbitration_by_local_total[
                "local_exceeds_total" if local_exceeds_total else
                "local_within_total"][label] += 1
            if auxiliary is not None:
                local_vs_baostock.append(relative_difference(local, auxiliary))
                cninfo_vs_baostock.append(relative_difference(primary, auxiliary))
            stats = symbol_stats[symbol]
            stats["differences"].append(difference)
            stats["directions"][direction] += 1
            stats["future_matches"] += future_gap is not None
            stats["local_exceeds_total"] += local_exceeds_total
            stats["arbitration"][label] += 1
            active.append((day, difference, direction, future_gap))
        close_interval()

    symbol_reports = []
    for symbol, values in sorted(symbol_stats.items()):
        diff = distribution(values["differences"])
        direction = values["directions"].most_common(1)[0][0]
        symbol_reports.append({
            "symbol": symbol, "exchange": symbol[:2],
            "board": board(symbol), "conflict_days": len(values["differences"]),
            "difference": diff,
            "median_difference_band": difference_band(diff["median"]),
            "majority_direction": direction,
            "future_cninfo_match_days": values["future_matches"],
            "local_exceeds_cninfo_total_days": values["local_exceeds_total"],
            "baostock_arbitration": dict(sorted(values["arbitration"].items())),
        })
    symbol_reports.sort(key=lambda item: (
        -item["conflict_days"], item["symbol"]))
    selected = _select_sample(
        symbol_reports,
        int(config["strict_rules"]["sample_global_top_symbols"]),
        int(config["strict_rules"]["sample_per_stratum"]))
    master = pd.read_csv(research_dir / "security_master.csv", dtype=str)
    master = master.set_index("symbol")
    ranges = {}
    for symbol in selected:
        listed_value = master.loc[symbol, "list_date"]
        delisted_value = master.loc[symbol, "delist_date"]
        listed = "" if pd.isna(listed_value) else str(listed_value)[:10]
        delisted = "" if pd.isna(delisted_value) else str(delisted_value)[:10]
        start = max(config["history_start_date"], listed) if listed else \
            config["history_start_date"]
        end = min(config["history_end_date"], delisted) if delisted else \
            config["history_end_date"]
        ranges[symbol] = {"start_date": start, "end_date": end}

    output_dir.mkdir(parents=True)
    sample_path = output_dir / "pilot_symbols.json"
    sample_path.write_text(json.dumps({
        "symbols": selected, "symbol_ranges": ranges,
        "source_role": "share_conflict_audit_only",
        "research_label": config["research_label"],
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    intervals_path = output_dir / "conflict_intervals.csv"
    pd.DataFrame(sorted(intervals, key=lambda item: (
        -item["security_days"], item["symbol"]))).to_csv(
            intervals_path, index=False)
    symbol_path = output_dir / "conflict_symbols.csv"
    pd.DataFrame(sorted(symbol_reports, key=lambda item: (
        -item["conflict_days"], item["symbol"]))).to_csv(
            symbol_path, index=False)
    report = {
        "scope": "eligible_non_st_full_market_float_share_conflicts",
        "normalized_share_event_count": event_count,
        "comparable_security_days": counts["comparable_days"],
        "conflict_security_days": counts["conflict_days"],
        "conflict_rate": counts["conflict_days"] / counts["comparable_days"],
        "conflict_symbol_count": len(symbol_reports),
        "difference": distribution(differences),
        "difference_band_days": dict(sorted(by_band.items())),
        "direction_days": dict(sorted(by_direction.items())),
        "exchange_days": {key: value["days"] for key, value in
                          sorted(by_exchange.items())},
        "board_days": {key: value["days"] for key, value in
                       sorted(by_board.items())},
        "year_days": {key: value["days"] for key, value in
                      sorted(by_year.items())},
        "local_exceeds_cninfo_total_days": counts[
            "local_exceeds_cninfo_total_days"],
        "future_cninfo_match_days": counts["future_cninfo_match_days"],
        "future_cninfo_match_rate": counts["future_cninfo_match_days"] /
        counts["conflict_days"] if counts["conflict_days"] else 0,
        "future_cninfo_match_gap_calendar_days": distribution(future_gaps),
        "interval_count": len(intervals),
        "interval_length_security_days": distribution(
            item["security_days"] for item in intervals),
        "baostock_input_rows": baostock_rows,
        "baostock_comparable_conflict_days": sum(
            value for key, value in arbitration.items()
            if key != "missing_auxiliary"),
        "baostock_arbitration": dict(sorted(arbitration.items())),
        "baostock_arbitration_by_future_match": {
            key: dict(sorted(value.items())) for key, value in
            sorted(arbitration_by_future_match.items())},
        "baostock_arbitration_by_difference_band": {
            key: dict(sorted(value.items())) for key, value in
            sorted(arbitration_by_band.items())},
        "baostock_arbitration_by_direction": {
            key: dict(sorted(value.items())) for key, value in
            sorted(arbitration_by_direction.items())},
        "baostock_arbitration_by_local_total_check": {
            key: dict(sorted(value.items())) for key, value in
            sorted(arbitration_by_local_total.items())},
        "local_vs_baostock_difference": distribution(local_vs_baostock),
        "cninfo_vs_baostock_difference": distribution(cninfo_vs_baostock),
        "sample_symbol_count": len(selected),
        "sample_symbols_sha256": sha256_file(sample_path),
        "top_conflict_symbols": symbol_reports[:100],
        "gate_status": "BLOCKED_SHARE_RECONCILIATION",
        "research_label": config["research_label"],
        "inputs": {
            "share_events": str(events_path),
            "share_events_sha256": sha256_file(events_path),
            "st_panel": str(st_panel_path),
            "st_panel_sha256": sha256_file(st_panel_path),
            "baostock": str(baostock_path) if baostock_path else None,
            "baostock_sha256": sha256_file(baostock_path)
            if baostock_path else None,
        },
        "outputs": {
            "sample_symbols": str(sample_path),
            "conflict_intervals": str(intervals_path),
            "conflict_symbols": str(symbol_path),
        },
    }
    report_path = output_dir / "audit.json"
    report_path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, report_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("share_events", type=Path)
    parser.add_argument("--research-dir", type=Path, required=True)
    parser.add_argument("--st-panel", type=Path, required=True)
    parser.add_argument("--baostock", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report, path = audit(
        args.share_events, args.research_dir, args.st_panel,
        args.output_dir, config, baostock_path=args.baostock)
    console = {key: value for key, value in report.items()
               if key not in {"top_conflict_symbols"}}
    print(json.dumps({**console, "report": str(path)}, ensure_ascii=False,
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
