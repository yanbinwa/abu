#!/usr/bin/env python3
"""Audit a structured-fundamental pilot; never computes factor returns."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(
        encoding="utf-8").splitlines() if line.strip()]


def build_audit(pilot_dir, expected_fields):
    pilot_dir = Path(pilot_dir)
    symbols = json.loads((pilot_dir / "pilot_symbols.json").read_text(
        encoding="utf-8"))["symbols"]
    collection = json.loads((pilot_dir / "collection_report.json").read_text(
        encoding="utf-8"))
    facts = read_jsonl(pilot_dir / "structured_extracted.jsonl")
    shares = read_jsonl(pilot_dir / "share_events.jsonl")
    by_symbol = defaultdict(set)
    field_counts = Counter()
    restated = 0
    incomplete_revision = 0
    for item in facts:
        by_symbol[item["symbol"]].add(item["statement_type"])
        field_counts[item["raw_field"]] += 1
        restated += int(item["is_restated"])
        incomplete_revision += int(not item["revision_history_complete"])
    share_symbols = {item["symbol"] for item in shares}
    missing_share_symbols = sorted(set(symbols) - share_symbols)
    missing_statements = {
        symbol: sorted({"income", "balance", "cashflow"} - by_symbol[symbol])
        for symbol in symbols
        if {"income", "balance", "cashflow"} - by_symbol[symbol]
    }
    missing_fields = sorted(set(expected_fields) - set(field_counts))
    blockers = []
    if collection["failures"]:
        blockers.append("structured_collection_failures")
    if missing_statements:
        blockers.append("incomplete_statement_symbol_coverage")
    if missing_fields:
        blockers.append("missing_required_structured_fields")
    if missing_share_symbols:
        blockers.append("incomplete_share_capital_symbol_coverage")
    if incomplete_revision:
        blockers.append("historical_revision_versions_unavailable")
    return {
        "config_version": collection["config_version"],
        "research_label": collection["research_label"],
        "pilot_symbol_count": len(symbols),
        "fact_value_count": len(facts),
        "statement_symbol_coverage": {
            kind: sum(kind in by_symbol[symbol] for symbol in symbols) /
            len(symbols) for kind in ("income", "balance", "cashflow")
        },
        "share_symbol_coverage": len(share_symbols) / len(symbols),
        "missing_share_symbols": missing_share_symbols,
        "field_counts": dict(sorted(field_counts.items())),
        "missing_fields": missing_fields,
        "missing_statements": missing_statements,
        "restated_value_count": restated,
        "revision_history_complete": incomplete_revision == 0,
        "deferred": collection["deferred"],
        "collection_failure_count": len(collection["failures"]),
        "blockers": blockers,
        "gate_status": (
            "ELIGIBLE_FOR_FULL_STRUCTURED_COLLECTION"
            if not blockers else "M4_BLOCKED_DATA_GATE"
        ),
        "interpretation": (
            "Structured values are safe only from max(NOTICE_DATE, "
            "UPDATE_DATE); no PDF or announcement reconstruction was used."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pilot_dir", type=Path)
    parser.add_argument("--mapping", type=Path, default=(
        Path(__file__).resolve().parents[1] /
        "configs/selection/fundamental_structured_field_mapping_v4.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite structured pilot audit")
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    report = build_audit(args.pilot_dir, mapping["fields"])
    encoded = json.dumps(report, ensure_ascii=False, indent=2,
                         sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    print("sha256=" + hashlib.sha256(encoded.encode("utf-8")).hexdigest())
    return 0 if report["gate_status"] == \
        "ELIGIBLE_FOR_FULL_STRUCTURED_COLLECTION" else 2


if __name__ == "__main__":
    raise SystemExit(main())
