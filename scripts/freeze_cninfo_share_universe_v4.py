#!/usr/bin/env python3
"""Freeze the full-market CNINFO share-capital collection universe."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/cninfo_share_capital_full_v4.json"
SYMBOL_PATTERN = re.compile(r"^(sh|sz)\d{6}$")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date(value, default=None):
    if value is None or pd.isna(value) or not str(value).strip():
        return default
    return date.fromisoformat(str(value)[:10])


def freeze_universe(config, output_dir):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite CNINFO share universe")
    master_path = Path(config["security_master"])
    master = pd.read_csv(master_path, dtype=str)
    required = {"symbol", "list_date", "delist_date", "exchange", "status"}
    if not required.issubset(master.columns):
        raise ValueError("security master missing required fields")
    if master.symbol.duplicated().any():
        raise ValueError("security master contains duplicate symbols")
    history_start = date.fromisoformat(config["history_start_date"])
    history_end = date.fromisoformat(config["history_end_date"])
    records = []
    exclusions = {}
    for row in master.to_dict("records"):
        symbol = str(row["symbol"]).lower()
        try:
            if not SYMBOL_PATTERN.fullmatch(symbol):
                raise ValueError("INVALID_SYMBOL")
            listed = _date(row["list_date"])
            delisted = _date(row.get("delist_date"), history_end)
            start = max(history_start, listed)
            end = min(history_end, delisted)
            if start > end:
                raise ValueError("OUTSIDE_RESEARCH_INTERVAL")
        except (TypeError, ValueError) as error:
            reason = str(error)
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        records.append({
            "symbol": symbol, "start_date": start.isoformat(),
            "end_date": end.isoformat(), "exchange": row["exchange"],
            "security_status": row["status"],
        })
    records.sort(key=lambda item: item["symbol"])
    chunk_size = int(config["chunk_size"])
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    output_dir.mkdir(parents=True)
    chunks = []
    for offset in range(0, len(records), chunk_size):
        values = records[offset:offset + chunk_size]
        chunk_id = "{:04d}".format(offset // chunk_size)
        directory = output_dir / "chunks" / chunk_id
        directory.mkdir(parents=True)
        payload = {
            "symbols": [item["symbol"] for item in values],
            "symbol_ranges": {
                item["symbol"]: {
                    "start_date": item["start_date"],
                    "end_date": item["end_date"],
                } for item in values
            },
            "source_role": config["source_role"],
            "research_label": config["research_label"],
        }
        path = directory / "pilot_symbols.json"
        path.write_text(json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        chunks.append({
            "chunk_id": chunk_id, "symbol_count": len(values),
            "first_symbol": values[0]["symbol"],
            "last_symbol": values[-1]["symbol"],
            "input": str(path.relative_to(output_dir)),
            "input_sha256": sha256_file(path),
        })
    all_symbols_path = output_dir / "pilot_symbols.json"
    all_symbols_path.write_text(json.dumps({
        "symbols": [item["symbol"] for item in records],
        "source_role": config["source_role"],
        "research_label": config["research_label"],
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "config_version": config["config_version"],
        "source_version": config["source_version"],
        "source_role": config["source_role"],
        "security_master": str(master_path),
        "security_master_sha256": sha256_file(master_path),
        "history_start_date": history_start.isoformat(),
        "history_end_date": history_end.isoformat(),
        "security_count": len(records), "chunk_size": chunk_size,
        "chunk_count": len(chunks), "chunks": chunks,
        "excluded_security_count": sum(exclusions.values()),
        "exclusion_reasons": dict(sorted(exclusions.items())),
        "all_symbols_path": str(all_symbols_path.relative_to(output_dir)),
        "all_symbols_sha256": sha256_file(all_symbols_path),
        "max_concurrency": int(config["max_concurrency"]),
        "status": "FROZEN_CNINFO_SHARE_COLLECTION_PLAN",
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
    report, path = freeze_universe(config, args.output_dir)
    print(json.dumps({**report, "manifest": str(path)}, ensure_ascii=False,
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
