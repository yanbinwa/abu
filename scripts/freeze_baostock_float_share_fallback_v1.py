#!/usr/bin/env python3
"""Freeze BaoStock collection to CNINFO's explicitly empty symbols."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = (
    ROOT / "configs/selection/baostock_float_share_fallback_v1.json"
)
SYMBOL_PATTERN = re.compile(r"^(sh|sz)\d{6}$")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_date(value, default):
    if value is None or pd.isna(value) or not str(value).strip():
        return default
    return date.fromisoformat(str(value)[:10])


def freeze(config, output_dir):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite BaoStock fallback plan")
    coverage_path = Path(config["cninfo_coverage_audit"])
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    symbols = sorted(set(map(str, coverage["explicit_empty_symbols"])))
    if len(symbols) != int(coverage["explicit_empty_symbol_count"]):
        raise ValueError("CNINFO explicit-empty symbol count mismatch")
    if any(not SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise ValueError("invalid symbol in CNINFO explicit-empty list")

    master_path = Path(config["security_master"])
    master = pd.read_csv(master_path, dtype=str).set_index("symbol")
    missing = sorted(set(symbols) - set(master.index))
    if missing:
        raise ValueError("fallback symbols absent from security master: " +
                         ",".join(missing))
    history_start = date.fromisoformat(config["history_start_date"])
    history_end = date.fromisoformat(config["history_end_date"])
    ranges = {}
    records = []
    for symbol in symbols:
        row = master.loc[symbol]
        listed = _optional_date(row.get("list_date"), history_start)
        delisted = _optional_date(row.get("delist_date"), history_end)
        start = max(history_start, listed)
        end = min(history_end, delisted)
        if start > end:
            raise ValueError("fallback symbol outside research range: " + symbol)
        ranges[symbol] = {
            "start_date": start.isoformat(), "end_date": end.isoformat(),
        }
        records.append({
            "symbol": symbol, "start_date": start.isoformat(),
            "end_date": end.isoformat(), "exchange": row["exchange"],
            "security_status": row["status"],
        })

    output_dir.mkdir(parents=True)
    symbols_path = output_dir / "pilot_symbols.json"
    symbols_path.write_text(json.dumps({
        "symbols": symbols, "symbol_ranges": ranges,
        "source_role": config["source_role"],
        "research_label": config["research_label"],
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "config_version": config["config_version"],
        "source_version": config["source_version"],
        "source_role": config["source_role"],
        "security_count": len(symbols),
        "records": records,
        "cninfo_coverage_audit": str(coverage_path),
        "cninfo_coverage_audit_sha256": sha256_file(coverage_path),
        "security_master": str(master_path),
        "security_master_sha256": sha256_file(master_path),
        "symbols_path": str(symbols_path),
        "symbols_sha256": sha256_file(symbols_path),
        "history_start_date": history_start.isoformat(),
        "history_end_date": history_end.isoformat(),
        "status": "FROZEN_BAOSTOCK_FLOAT_SHARE_FALLBACK_PLAN",
        "research_label": config["research_label"],
    }
    manifest_path = output_dir / "universe_manifest.json"
    manifest_path.write_text(json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return manifest, manifest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report, path = freeze(config, args.output_dir)
    print(json.dumps({**report, "manifest": str(path)}, ensure_ascii=False,
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
