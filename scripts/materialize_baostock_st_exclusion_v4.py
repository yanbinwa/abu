#!/usr/bin/env python3
"""Materialize audited Baostock daily ST states into a read-only NPZ panel."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.audit_baostock_st_exclusion_v4 import _read_rows  # noqa: E402


DEFAULT_CONFIG = ROOT / "configs/selection/baostock_st_exclusion_v4.json"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def materialize(universe_dir, collection_roots, audit_path, output_dir,
                config):
    universe_dir = Path(universe_dir)
    collection_roots = [Path(root) for root in collection_roots]
    audit_path = Path(audit_path)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite ST exclusion panel")
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if audit.get("gate_status") != "PASS_ST_EXCLUSION_DATA_GATE":
        raise ValueError("ST exclusion audit has not passed")
    if audit.get("source_role") != "st_exclusion_only":
        raise ValueError("invalid ST source role")
    manifest = json.loads((universe_dir / "universe_manifest.json").read_text(
        encoding="utf-8"))

    master = pd.read_csv(config["security_master"], dtype=str)
    symbols = sorted(master.symbol.astype(str).unique())
    symbol_index = {symbol: index for index, symbol in enumerate(symbols)}
    sessions = pd.read_csv(config["trading_sessions_source"], usecols=["date"])
    dates = pd.to_numeric(sessions.date, errors="coerce").dropna().astype(int)
    start = int(config["history_start_date"].replace("-", ""))
    end = int(config["history_end_date"].replace("-", ""))
    dates = sorted(set(dates[(dates >= start) & (dates <= end)].tolist()))
    date_index = {day: index for index, day in enumerate(dates)}
    shape = (len(dates), len(symbols))
    known = np.zeros(shape, dtype=bool)
    is_st = np.zeros(shape, dtype=bool)
    selected_chunks = []
    ignored_rows = 0

    for chunk in manifest["chunks"]:
        chunk_id = chunk["chunk_id"]
        selected = None
        for root in collection_roots:
            directory = root / "chunks" / chunk_id
            report_path = directory / "collection_report.json"
            data_path = directory / "baostock_daily.jsonl"
            if not report_path.is_file() or not data_path.is_file():
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if report.get("status") == "COLLECTED_ST_EXCLUSION_CHUNK" and \
                    not report.get("failures"):
                selected = data_path
                break
        if selected is None:
            raise ValueError("missing complete ST chunk: " + chunk_id)
        selected_chunks.append({
            "chunk_id": chunk_id, "path": str(selected),
            "sha256": sha256_file(selected),
        })
        for row in _read_rows(selected):
            if row.get("source_role") != "st_exclusion_only" or \
                    row.get("derived_float_shares") is not None:
                raise ValueError("ST panel row violates exclusion-only role")
            row_index = date_index.get(int(row["date"]))
            column = symbol_index.get(str(row["symbol"]))
            if row_index is None or column is None:
                ignored_rows += 1
                continue
            if known[row_index, column]:
                raise ValueError("duplicate ST state in selected chunks")
            known[row_index, column] = True
            is_st[row_index, column] = bool(int(row["is_st"]))

    if np.any(is_st & ~known):
        raise ValueError("ST state cannot be true when status is unknown")
    output_dir.mkdir(parents=True)
    panel_path = output_dir / "st_exclusion_panel_v4.npz"
    np.savez_compressed(
        panel_path, dates=np.asarray(dates, dtype=np.int32),
        symbols=np.asarray(symbols, dtype="U8"), known=known, is_st=is_st,
        source_role=np.asarray(["st_exclusion_only"], dtype="U32"))
    report = {
        "config_version": config["config_version"],
        "source_role": "st_exclusion_only",
        "date_count": len(dates),
        "symbol_count": len(symbols),
        "known_matrix_cells": int(known.sum()),
        "st_matrix_cells": int(is_st.sum()),
        "ignored_source_rows_outside_panel": ignored_rows,
        "audit_path": str(audit_path),
        "audit_sha256": sha256_file(audit_path),
        "selected_chunks": selected_chunks,
        "panel_path": str(panel_path),
        "panel_sha256": sha256_file(panel_path),
        "unknown_st_policy": "exclude",
        "allow_as_alpha_or_ranking_feature": False,
        "status": "MATERIALIZED_ST_EXCLUSION_PANEL",
        "research_label": config["research_label"],
    }
    report_path = output_dir / "materialization_report.json"
    report_path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, {"panel": panel_path, "report": report_path}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("universe_dir", type=Path)
    parser.add_argument("collection_roots", type=Path, nargs="+")
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report, paths = materialize(
        args.universe_dir, args.collection_roots, args.audit,
        args.output_dir, config)
    print(json.dumps({**report, "outputs": {
        key: str(value) for key, value in paths.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
