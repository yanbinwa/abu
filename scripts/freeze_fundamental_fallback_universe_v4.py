#!/usr/bin/env python3
"""Freeze symbols requiring structured-statement market-cap fallback."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def freeze(panel_path, security_master_path, output_dir, chunk_size=25):
    panel_path = Path(panel_path)
    security_master_path = Path(security_master_path)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite fallback universe")
    with np.load(panel_path, allow_pickle=False) as payload:
        dates = payload["dates"].astype(np.int64)
        symbols = payload["symbols"].astype(str)
        cap = payload["total_market_cap"].astype(np.float64)
        eligible = payload["eligible_non_st_price_day"].astype(bool)
    if cap.shape != eligible.shape or cap.shape != (len(dates), len(symbols)):
        raise ValueError("market-cap panel shape mismatch")
    master = pd.read_csv(security_master_path, dtype=str).fillna("")
    if master.symbol.duplicated().any():
        raise ValueError("security master contains duplicate symbols")
    metadata = master.set_index("symbol").to_dict("index")
    missing = eligible & ~np.isfinite(cap)
    records = []
    for column, symbol in enumerate(symbols):
        positions = np.flatnonzero(missing[:, column])
        if not len(positions):
            continue
        if symbol not in metadata:
            raise ValueError("fallback symbol absent from security master")
        details = metadata[symbol]
        records.append({
            "symbol": symbol,
            "missing_eligible_days": int(len(positions)),
            "first_missing_date": int(dates[positions[0]]),
            "last_missing_date": int(dates[positions[-1]]),
            "exchange": details.get("exchange", symbol[:2]),
            "security_status": details.get("status", ""),
            "delist_date": details.get("delist_date", ""),
        })
    records.sort(key=lambda item: item["symbol"])
    chunk_size = int(chunk_size)
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    output_dir.mkdir(parents=True)
    chunks = []
    for offset in range(0, len(records), chunk_size):
        values = records[offset:offset + chunk_size]
        chunk_id = "{:04d}".format(offset // chunk_size)
        directory = output_dir / "chunks" / chunk_id
        directory.mkdir(parents=True)
        path = directory / "fallback_symbols.json"
        path.write_text(json.dumps({
            "symbols": [item["symbol"] for item in values],
            "records": values,
            "purpose": "structured_statement_total_share_fallback",
        }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        chunks.append({
            "chunk_id": chunk_id, "symbol_count": len(values),
            "input": str(path.relative_to(output_dir)),
            "input_sha256": sha256_file(path),
        })
    all_path = output_dir / "fallback_symbols.json"
    all_path.write_text(json.dumps({
        "symbols": [item["symbol"] for item in records],
        "records": records,
        "purpose": "structured_statement_total_share_fallback",
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    status_counts = pd.Series(
        [item["security_status"] for item in records]).value_counts().to_dict()
    manifest = {
        "market_cap_panel": str(panel_path),
        "market_cap_panel_sha256": sha256_file(panel_path),
        "security_master": str(security_master_path),
        "security_master_sha256": sha256_file(security_master_path),
        "fallback_symbol_count": len(records),
        "missing_eligible_security_days": int(missing.sum()),
        "security_status_counts": status_counts,
        "chunk_size": chunk_size, "chunk_count": len(chunks),
        "chunks": chunks,
        "all_symbols": str(all_path.relative_to(output_dir)),
        "all_symbols_sha256": sha256_file(all_path),
        "status": "FROZEN_FUNDAMENTAL_FALLBACK_UNIVERSE",
        "research_label": "RESEARCH_ONLY / NOT_FOR_LIVE_TRADING",
    }
    manifest_path = output_dir / "universe_manifest.json"
    manifest_path.write_text(json.dumps(
        manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return manifest, manifest_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("market_cap_panel", type=Path)
    parser.add_argument("--security-master", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--chunk-size", type=int, default=25)
    args = parser.parse_args()
    report, path = freeze(
        args.market_cap_panel, args.security_master, args.output_dir,
        chunk_size=args.chunk_size)
    print(json.dumps({**report, "manifest": str(path)}, ensure_ascii=False,
                     indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
