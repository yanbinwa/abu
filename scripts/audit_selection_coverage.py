#!/usr/bin/env python3
"""Audit actual per-file and per-day coverage of selection research inputs."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd


FIELDS = (
    "open", "high", "low", "close", "volume", "amount",
    "outstanding_share", "turnover",
)
SCHEMA_VERSION = "1.0.0"


def _ratio(frame: pd.DataFrame, field: str) -> float:
    if field not in frame or frame.empty:
        return 0.0
    values = pd.to_numeric(frame[field], errors="coerce")
    return float(values.notna().mean())


def _symbol_set(path: Path, candidates=("symbol", "证券代码")) -> set[str]:
    if not path.exists() or path.stat().st_size == 0:
        return set()
    frame = pd.read_csv(path, dtype=str)
    for column in candidates:
        if column not in frame:
            continue
        values = frame[column].dropna().astype(str)
        if column == "证券代码":
            values = values.str.zfill(6)
        return set(values)
    return set()


def audit_coverage(signal_dir: Path, research_dir: Path):
    master_path = research_dir / "security_master.csv"
    master = pd.read_csv(master_path, dtype={"code": str, "symbol": str})
    raw_dir = research_dir / "raw"
    extra_dir = research_dir / "signal_extra"
    signal_symbols = {
        path.name.split("_")[0].split(".")[0]
        for directory in (signal_dir, extra_dir)
        if directory.exists()
        for path in directory.iterdir()
        if path.is_file() and path.name.startswith(("sh", "sz"))
    }
    industry_symbols = _symbol_set(research_dir / "industry_changes.csv")
    action_symbols = _symbol_set(research_dir / "corporate_actions.csv")
    sz_st_codes = _symbol_set(
        research_dir / "sz_name_changes.csv", candidates=("证券代码",)
    )
    sz_st_symbols = {"sz" + code for code in sz_st_codes}

    daily = defaultdict(lambda: {field: 0 for field in FIELDS})
    daily_rows = defaultdict(int)
    symbol_rows = []
    provenance_rows = []

    for row in master.itertuples(index=False):
        symbol = str(row.symbol)
        path = raw_dir / f"{symbol}.csv"
        if not path.exists():
            symbol_rows.append({
                "symbol": symbol, "rows": 0, "first_date": np.nan,
                "last_date": np.nan, "has_signal": symbol in signal_symbols,
                "has_industry_history": symbol in industry_symbols,
                "has_st_history": symbol in sz_st_symbols,
                "has_corporate_actions": symbol in action_symbols,
                **{f"{field}_coverage": 0.0 for field in FIELDS},
            })
            provenance_rows.append({
                "symbol": symbol, "provider_hint": "missing_raw_file",
                "evidence": "no raw file",
            })
            continue

        frame = pd.read_csv(path)
        dates = pd.to_numeric(frame.get("date"), errors="coerce")
        valid_dates = dates.notna()
        frame = frame.loc[valid_dates].copy()
        frame["date"] = dates[valid_dates].astype(np.int64)
        coverages = {field: _ratio(frame, field) for field in FIELDS}
        symbol_rows.append({
            "symbol": symbol,
            "rows": len(frame),
            "first_date": int(frame.date.min()) if not frame.empty else np.nan,
            "last_date": int(frame.date.max()) if not frame.empty else np.nan,
            "has_signal": symbol in signal_symbols,
            "has_industry_history": symbol in industry_symbols,
            "has_st_history": symbol in sz_st_symbols,
            "has_corporate_actions": symbol in action_symbols,
            **{f"{field}_coverage": coverages[field] for field in FIELDS},
        })

        has_attention = all(coverages[field] > 0 for field in
                            ("amount", "outstanding_share", "turnover"))
        provenance_rows.append({
            "symbol": symbol,
            "provider_hint": "sina_like" if has_attention else "tencent_like_or_incomplete",
            "evidence": "field_signature",
        })

        for item in frame.itertuples(index=False):
            date = int(item.date)
            daily_rows[date] += 1
            for field in FIELDS:
                value = getattr(item, field, np.nan)
                if pd.notna(value):
                    daily[date][field] += 1

    daily_output = []
    for date in sorted(daily_rows):
        row_count = daily_rows[date]
        item = {"date": date, "raw_rows": row_count}
        for field in FIELDS:
            item[f"{field}_count"] = daily[date][field]
            item[f"{field}_coverage"] = (
                daily[date][field] / row_count if row_count else 0.0
            )
        daily_output.append(item)

    symbols = pd.DataFrame(symbol_rows).sort_values("symbol").reset_index(drop=True)
    days = pd.DataFrame(daily_output)
    provenance = pd.DataFrame(provenance_rows).sort_values("symbol").reset_index(drop=True)
    field_coverage = {
        field: float(
            np.average(
                symbols[f"{field}_coverage"], weights=symbols.rows
            ) if symbols.rows.sum() else 0.0
        )
        for field in FIELDS
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "security_count": int(len(master)),
        "raw_file_count": int((symbols.rows > 0).sum()),
        "signal_file_count": int(symbols.has_signal.sum()),
        "daily_rows": int(symbols.rows.sum()),
        "first_date": int(days.date.min()) if not days.empty else None,
        "last_date": int(days.date.max()) if not days.empty else None,
        "field_coverage": field_coverage,
        "complete_attention_symbols": int(
            ((symbols.amount_coverage > 0) &
             (symbols.outstanding_share_coverage > 0) &
             (symbols.turnover_coverage > 0)).sum()
        ),
        "incomplete_attention_symbols": int(
            ((symbols.amount_coverage == 0) |
             (symbols.outstanding_share_coverage == 0) |
             (symbols.turnover_coverage == 0)).sum()
        ),
        "industry_history_symbols": int(symbols.has_industry_history.sum()),
        "st_history_symbols": int(symbols.has_st_history.sum()),
        "corporate_action_symbols": int(symbols.has_corporate_actions.sum()),
    }
    return days, symbols, provenance, summary


def write_outputs(output_dir: Path, days, symbols, provenance, summary):
    output_dir.mkdir(parents=True, exist_ok=True)
    days.to_csv(output_dir / "coverage_daily.csv", index=False)
    symbols.to_csv(output_dir / "coverage_symbol.csv", index=False)
    provenance.to_csv(output_dir / "provider_provenance.csv", index=False)
    (output_dir / "coverage_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or args.research_dir
    days, symbols, provenance, summary = audit_coverage(
        args.signal_dir, args.research_dir
    )
    write_outputs(output_dir, days, symbols, provenance, summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
