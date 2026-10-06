#!/usr/bin/env python3
"""Freeze a reproducible full-market structured-fundamental collection plan."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/fundamental_full_market_v1.json"
SYMBOL_PATTERN = re.compile(r"^(sh|sz)\d{6}$")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _optional_date(value, default=None):
    if value is None or pd.isna(value) or not str(value).strip():
        return default
    return date.fromisoformat(str(value)[:10])


def board_bucket(symbol):
    if symbol.startswith("sh688"):
        return "star"
    if symbol.startswith("sz300"):
        return "chinext"
    return "sh_main" if symbol.startswith("sh") else "sz_main"


def _validation_records(records, sample_size):
    """Evenly sample listing age within board/status strata."""
    if sample_size <= 0:
        return []
    frame = pd.DataFrame(records)
    frame["stratum"] = frame.board + ":" + frame.security_status
    groups = tuple(sorted(frame.stratum.unique()))
    base, remainder = divmod(sample_size, len(groups))
    chosen = []
    for index, key in enumerate(groups):
        quota = base + int(index < remainder)
        group = frame[frame.stratum == key].sort_values(
            ["list_date", "symbol"]).reset_index(drop=True)
        quota = min(quota, len(group))
        if quota:
            positions = np.linspace(0, len(group) - 1, quota, dtype=int)
            chosen.extend(group.iloc[positions].to_dict("records"))
    # Fill a shortfall deterministically when a small stratum exhausted.
    selected = {item["symbol"] for item in chosen}
    for item in records:
        if len(chosen) >= sample_size:
            break
        if item["symbol"] not in selected:
            chosen.append(item)
            selected.add(item["symbol"])
    return sorted(chosen, key=lambda item: item["symbol"])


def _write_chunk(output_dir, relative, records, purpose):
    path = output_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbols": [item["symbol"] for item in records],
        "records": records,
        "purpose": purpose,
    }
    path.write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return path


def freeze(config, output_dir):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite fundamental universe")
    master_path = Path(config["security_master"])
    master = pd.read_csv(master_path, dtype=str).fillna("")
    required = {"symbol", "list_date", "delist_date", "exchange", "status"}
    if not required.issubset(master.columns):
        raise ValueError("security master missing required fields")
    if master.symbol.duplicated().any():
        raise ValueError("security master contains duplicate symbols")
    start = date.fromisoformat(config["history_start_date"])
    end = date.fromisoformat(config["history_end_date"])
    records, exclusions = [], {}
    for row in master.to_dict("records"):
        symbol = str(row["symbol"]).lower()
        try:
            if not SYMBOL_PATTERN.fullmatch(symbol):
                raise ValueError("INVALID_SYMBOL")
            listed = _optional_date(row["list_date"])
            delisted = _optional_date(row["delist_date"], end)
            if listed is None:
                raise ValueError("MISSING_LIST_DATE")
            if max(start, listed) > min(end, delisted):
                raise ValueError("OUTSIDE_RESEARCH_INTERVAL")
        except (TypeError, ValueError) as error:
            reason = str(error)
            exclusions[reason] = exclusions.get(reason, 0) + 1
            continue
        records.append({
            "symbol": symbol, "exchange": str(row["exchange"]),
            "security_status": str(row["status"]),
            "list_date": listed.isoformat(),
            "delist_date": (str(row["delist_date"])[:10]
                             if str(row["delist_date"]) else ""),
            "board": board_bucket(symbol),
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
        relative = Path("chunks") / chunk_id / "fundamental_symbols.json"
        path = _write_chunk(output_dir, relative, values,
                            "full_market_structured_fundamental")
        chunks.append({
            "chunk_id": chunk_id, "symbol_count": len(values),
            "first_symbol": values[0]["symbol"],
            "last_symbol": values[-1]["symbol"],
            "input": str(relative), "input_sha256": sha256_file(path),
        })
    all_path = _write_chunk(
        output_dir, Path("fundamental_symbols.json"), records,
        "full_market_structured_fundamental")
    validation = _validation_records(
        records, int(config.get("validation_sample_size", 0)))
    validation_path = _write_chunk(
        output_dir, Path("validation") / "fundamental_symbols.json",
        validation, "deterministic_connectivity_and_coverage_validation")
    manifest = {
        "config_version": config["config_version"],
        "config_sha256": hashlib.sha256(json.dumps(
            config, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")).hexdigest(),
        "adapter_version": config["adapter_version"],
        "required_statements": list(config["required_statements"]),
        "minimum_history_sessions": int(config.get(
            "minimum_history_sessions", 0)),
        "security_master": str(master_path),
        "security_master_sha256": sha256_file(master_path),
        "history_start_date": start.isoformat(),
        "history_end_date": end.isoformat(),
        "security_count": len(records), "chunk_size": chunk_size,
        "chunk_count": len(chunks), "chunks": chunks,
        "security_status_counts": dict(sorted(pd.Series(
            [item["security_status"] for item in records]).value_counts(
        ).to_dict().items())),
        "board_counts": dict(sorted(pd.Series(
            [item["board"] for item in records]).value_counts(
        ).to_dict().items())),
        "excluded_security_count": sum(exclusions.values()),
        "exclusion_reasons": dict(sorted(exclusions.items())),
        "all_symbols": str(all_path.relative_to(output_dir)),
        "all_symbols_sha256": sha256_file(all_path),
        "validation_sample": str(validation_path.relative_to(output_dir)),
        "validation_sample_sha256": sha256_file(validation_path),
        "validation_symbol_count": len(validation),
        "max_concurrency": int(config["max_concurrency"]),
        "status": "FROZEN_FULL_MARKET_FUNDAMENTAL_COLLECTION_PLAN",
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


if __name__ == "__main__":
    main()
