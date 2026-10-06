#!/usr/bin/env python3
"""Collect one immutable full-market structured-fundamental chunk."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    ImmutableFundamentalRawStore, stable_json, structured_statement_records,
)
from scripts.collect_structured_fundamental_v4 import frame_snapshot  # noqa: E402
from scripts.eastmoney_bounded_statements_v1 import (  # noqa: E402
    load_bounded_eastmoney_adapters,
)


DEFAULT_CONFIG = ROOT / "configs/selection/fundamental_full_market_v1.json"
DEFAULT_MAPPING = ROOT / "configs/selection/fundamental_structured_field_mapping_v4.json"


def collect(config, mapping, chunk_dir, output_dir, adapters=None,
            raw_root=None, ingested_at=None):
    chunk_dir, output_dir = Path(chunk_dir), Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite fundamental chunk")
    output_dir.mkdir(parents=True)
    frozen_path = chunk_dir / "fundamental_symbols.json"
    frozen = json.loads(frozen_path.read_text(encoding="utf-8"))
    records = {item["symbol"]: item for item in frozen["records"]}
    symbols = list(frozen["symbols"])
    if set(symbols) != set(records):
        raise ValueError("frozen fundamental symbols and records disagree")
    adapters = adapters or load_bounded_eastmoney_adapters(config)
    store = ImmutableFundamentalRawStore(raw_root or config["raw_root"])
    ingested_at = ingested_at or datetime.now(timezone.utc).isoformat()
    facts, audits, failures, empty = [], [], [], []
    required_statements = tuple(config["required_statements"])
    config_sha256 = hashlib.sha256(stable_json(config).encode(
        "utf-8")).hexdigest()
    mapping_sha256 = hashlib.sha256(stable_json(mapping).encode(
        "utf-8")).hexdigest()
    for symbol in symbols:
        provider_symbol = symbol[:2].upper() + symbol[2:]
        is_delisted = records[symbol]["security_status"] == "delisted"
        for statement_type in required_statements:
            adapter = adapters[(statement_type, is_delisted)]
            request = {
                "adapter": adapter.__name__, "symbol": provider_symbol,
                "statement_type": statement_type, "adapter_snapshot": True,
                "security_status": records[symbol]["security_status"],
                "report_period_start_date": config.get(
                    "report_period_start_date"),
                "report_period_end_date": config.get("history_end_date"),
            }
            metadata = {"batch_id": None}
            try:
                frame = adapter(provider_symbol)
                snapshot = frame_snapshot(frame)
                payload = stable_json(snapshot).encode("utf-8")
                outcome = "SUCCESS_NONEMPTY" if len(frame) else "SUCCESS_EMPTY"
                _, metadata = store.capture(
                    "eastmoney_structured", "statement_" + statement_type,
                    request, payload, outcome, config["adapter_version"],
                    record_count=len(frame), currency="CNY", unit="yuan",
                    symbol=symbol)
                normalized, audit = structured_statement_records(
                    snapshot["records"], statement_type, symbol, mapping,
                    metadata["payload_sha256"], ingested_at)
                facts.extend(normalized)
                audits.append({
                    "symbol": symbol, "dataset": statement_type,
                    "batch_id": metadata["batch_id"], **audit,
                })
                if not normalized:
                    empty.append({"symbol": symbol,
                                  "statement_type": statement_type})
            except Exception as error:
                if metadata["batch_id"] is None:
                    _, metadata = store.capture(
                        "eastmoney_structured",
                        "statement_" + statement_type, request, b"",
                        "PARSE_FAILURE", config["adapter_version"],
                        error=error, symbol=symbol)
                failures.append({
                    "symbol": symbol, "statement_type": statement_type,
                    "error_type": type(error).__name__, "error": str(error),
                    "batch_id": metadata["batch_id"],
                })
    facts.sort(key=lambda item: (
        item["symbol"], item["statement_type"], item["report_period"],
        item["source_key"]))
    facts_path = output_dir / "structured_fundamental_facts.jsonl"
    facts_path.write_text("".join(
        stable_json(item) + "\n" for item in facts), encoding="utf-8")
    clients = {id(adapter.client): adapter.client
               for adapter in adapters.values()
               if hasattr(adapter, "client")}
    report = {
        "config_version": config["config_version"],
        "config_sha256": config_sha256,
        "adapter_version": config["adapter_version"],
        "mapping_version": mapping.get("mapping_version"),
        "mapping_sha256": mapping_sha256,
        "source_role": config["source_role"],
        "symbol_count": len(symbols),
        "statement_attempt_count": len(symbols) * len(required_statements),
        "fact_value_count": len(facts), "dataset_audits": audits,
        "explicit_empty_statements": empty, "failures": failures,
        "adapter_request_metrics": [client.metrics()
                                    for client in clients.values()],
        "revision_history_complete": False,
        "status": ("COLLECTED_FULL_MARKET_FUNDAMENTAL_CHUNK"
                   if not failures else
                   "BLOCKED_FULL_MARKET_FUNDAMENTAL_CHUNK"),
        "research_label": config["research_label"],
    }
    report_path = output_dir / "collection_report.json"
    report_path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, {"facts": facts_path, "report": report_path}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chunk_dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    report, paths = collect(
        config, mapping, args.chunk_dir, args.output_dir,
        raw_root=args.raw_root)
    print(json.dumps({**report, "outputs": {
        key: str(value) for key, value in paths.items()}},
        ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
