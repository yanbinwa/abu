#!/usr/bin/env python3
"""Freeze an A-share master including securities delisted since a cutoff."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.expand_alpha158_history_2015_data_v1 import query_basic  # noqa: E402


PATTERNS = {
    "sh": re.compile(r"^(600|601|603|605|688|689)\d{3}$"),
    "sz": re.compile(r"^(000|001|002|003|300|301)\d{3}$"),
}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def build_master(frame, cutoff="2015-01-01"):
    work = frame.copy().fillna("")
    work["provider_code"] = work.code.astype(str).str.lower()
    work = work[work.type.astype(str).eq("1")].copy()
    work["exchange"] = work.provider_code.str[:2]
    work["code"] = work.provider_code.str[3:]
    valid = [bool(PATTERNS.get(exchange, re.compile(r"$.")).fullmatch(code))
             for exchange, code in zip(work.exchange, work.code)]
    work = work[valid].copy()
    active = work.status.astype(str).eq("1")
    delisted = work.outDate.astype(str).ge(cutoff) & work.outDate.astype(str).ne("")
    work = work[active | delisted].copy()
    work["symbol"] = work.exchange + work.code
    work["security_status"] = np.where(
        work.status.astype(str).eq("1"), "listed", "delisted")
    result = pd.DataFrame({
        "code": work.code, "name": work.code_name,
        "list_date": work.ipoDate,
        "delist_date": work.outDate.replace("", np.nan),
        "exchange": work.exchange, "symbol": work.symbol,
        "status": work.security_status,
    })
    result.sort_values("symbol", inplace=True)
    result.drop_duplicates("symbol", keep="first", inplace=True)
    return result.reset_index(drop=True)


def freeze(output_dir, cutoff="2015-01-01"):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(output_dir)
    output_dir.mkdir(parents=True)
    source = query_basic()
    source_path = output_dir / "baostock_stock_basic.csv"
    source.to_csv(source_path, index=False)
    master = build_master(source, cutoff)
    master_path = output_dir / "security_master.csv"
    master.to_csv(master_path, index=False)
    report = {
        "dataset_id": "security_master_{}_full_market_v1".format(
            str(cutoff)[:4]),
        "created_at": datetime.now().astimezone().isoformat(),
        "delist_cutoff": cutoff, "security_count": len(master),
        "exchange_counts": master.exchange.value_counts().sort_index().to_dict(),
        "status_counts": master.status.value_counts().sort_index().to_dict(),
        "source": "baostock.query_stock_basic",
        "source_sha256": sha256(source_path),
        "security_master_sha256": sha256(master_path),
        "historical_snapshot_claim": False,
    }
    (output_dir / "registration.json").write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--delist-cutoff", default="2015-01-01")
    args = parser.parse_args()
    print(json.dumps(freeze(
        args.output_dir, args.delist_cutoff), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
