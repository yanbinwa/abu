#!/usr/bin/env python3
"""Collect one immutable structured balance-sheet fallback chunk."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    ImmutableFundamentalRawStore, stable_json, structured_statement_records,
)
from scripts.collect_structured_fundamental_v4 import (  # noqa: E402
    frame_snapshot, load_akshare_adapters,
)


DEFAULT_CONFIG = ROOT / "configs/selection/fundamental_balance_fallback_v4.json"
DEFAULT_MAPPING = (
    ROOT / "configs/selection/fundamental_structured_field_mapping_v4.json"
)


def probe_eastmoney_balance(provider_symbol, is_delisted, timeout=30):
    """Verify balance-sheet no-data at the same endpoint AKShare consumes."""
    import requests
    if not is_delisted:
        raise ValueError("listed balance adapter failures cannot be downgraded")
    url = "https://datacenter.eastmoney.com/securities/api/data/get"
    catalog_params = {
        "type": "RPT_F10_FINANCE_GINCOME",
        "sty": "SECUCODE,SECURITY_CODE,REPORT_DATE,REPORT_TYPE,REPORT_DATE_NAME",
        "filter": '(SECUCODE="{}.{}")'.format(
            provider_symbol[2:], provider_symbol[:2]),
        "p": "1", "ps": "200", "sr": "-1", "st": "REPORT_DATE",
        "source": "HSF10", "client": "PC", "v": "07306678536291241",
    }
    catalog = requests.get(url, params=catalog_params, timeout=timeout)
    catalog.raise_for_status()
    result = catalog.json().get("result")
    catalog_rows = [] if result is None else (result.get("data") or [])
    if not catalog_rows:
        return catalog, {"url": url, "params": catalog_params,
                         "probe_stage": "report_catalog"}
    dates = sorted({str(item["REPORT_DATE"])[:10] for item in catalog_rows})
    quoted = ",".join("'{}'".format(value) for value in dates)
    balance_params = {
        "type": "RPT_F10_FINANCE_GBALANCE", "sty": "F10_FINANCE_GBALANCE",
        "filter": '(SECUCODE="{}{}")'.format(
            provider_symbol[2:] + ".", provider_symbol[:2]) +
        "(REPORT_DATE in ({}))".format(quoted),
        "p": "1", "ps": "200", "sr": "-1", "st": "REPORT_DATE",
        "source": "HSF10", "client": "PC", "v": "05767841728614413",
    }
    response = requests.get(url, params=balance_params, timeout=timeout)
    return response, {"url": url, "params": balance_params,
                      "probe_stage": "balance_statement"}


def _verified_empty(response):
    if int(response.status_code) >= 400:
        return False
    body = response.json()
    result = body.get("result")
    return result is None or not (result.get("data") or [])


def collect(config, mapping, chunk_dir, output_dir, adapters=None,
            raw_root=None, ingested_at=None, empty_probe=None):
    chunk_dir = Path(chunk_dir)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite balance fallback chunk")
    output_dir.mkdir(parents=True)
    frozen = json.loads((chunk_dir / "fallback_symbols.json").read_text(
        encoding="utf-8"))
    records_by_symbol = {
        item["symbol"]: item for item in frozen["records"]}
    symbols = frozen["symbols"]
    adapters = adapters or load_akshare_adapters()
    empty_probe = empty_probe or probe_eastmoney_balance
    store = ImmutableFundamentalRawStore(raw_root or config["raw_root"])
    ingested_at = ingested_at or datetime.now(timezone.utc).isoformat()
    facts = []
    audits = []
    failures = []
    empty_symbols = []
    for symbol in symbols:
        metadata = {"batch_id": None}
        is_delisted = records_by_symbol[symbol]["security_status"] == "delisted"
        adapter = adapters[("balance", is_delisted)]
        provider_symbol = symbol[:2].upper() + symbol[2:]
        request = {
            "adapter": adapter.__name__, "symbol": provider_symbol,
            "statement_type": "balance", "adapter_snapshot": True,
            "security_status": records_by_symbol[symbol]["security_status"],
        }
        try:
            try:
                frame = adapter(provider_symbol)
            except Exception as adapter_error:
                response, probe_request = empty_probe(
                    provider_symbol, is_delisted)
                probe_payload = bytes(response.content)
                probe_outcome = (
                    "SUCCESS_EMPTY" if _verified_empty(response) else
                    ("HTTP_FAILURE" if int(response.status_code) >= 400 else
                     "SUCCESS_NONEMPTY"))
                _, probe_metadata = store.capture(
                    "eastmoney_structured", "statement_catalog_probe",
                    probe_request, probe_payload, probe_outcome,
                    config["adapter_version"],
                    http_status=int(response.status_code), symbol=symbol)
                if not _verified_empty(response):
                    raise adapter_error
                frame = pd.DataFrame()
                request["verified_empty_probe_batch_id"] = \
                    probe_metadata["batch_id"]
            snapshot = frame_snapshot(frame)
            payload = stable_json(snapshot).encode("utf-8")
            outcome = "SUCCESS_NONEMPTY" if len(frame) else "SUCCESS_EMPTY"
            _, metadata = store.capture(
                "eastmoney_structured", "statement_balance", request,
                payload, outcome, config["adapter_version"],
                record_count=len(frame), currency="CNY", unit="yuan",
                symbol=symbol)
            normalized, audit = structured_statement_records(
                snapshot["records"], "balance", symbol, mapping,
                metadata["payload_sha256"], ingested_at)
            facts.extend(normalized)
            audits.append({
                "symbol": symbol, "batch_id": metadata["batch_id"], **audit})
            if not normalized:
                empty_symbols.append(symbol)
        except Exception as error:
            if metadata["batch_id"] is None:
                _, metadata = store.capture(
                    "eastmoney_structured", "statement_balance", request,
                    b"", "PARSE_FAILURE", config["adapter_version"],
                    error=error, symbol=symbol)
            failures.append({
                "symbol": symbol, "error_type": type(error).__name__,
                "error": str(error), "batch_id": metadata["batch_id"],
            })
    facts.sort(key=lambda item: (
        item["symbol"], item["report_period"], item["source_key"]))
    facts_path = output_dir / "structured_balance_facts.jsonl"
    report_path = output_dir / "collection_report.json"
    facts_path.write_text("".join(
        stable_json(item) + "\n" for item in facts), encoding="utf-8")
    report = {
        "config_version": config["config_version"],
        "adapter_version": config["adapter_version"],
        "source_role": config["source_role"],
        "symbol_count": len(symbols),
        "symbols_with_facts": len(symbols) - len(empty_symbols) -
        len({item["symbol"] for item in failures}),
        "empty_symbols": empty_symbols, "fact_value_count": len(facts),
        "dataset_audits": audits, "failures": failures,
        "revision_history_complete": False,
        "status": "COLLECTED_BALANCE_FALLBACK_CHUNK" if not failures else
        "BLOCKED_BALANCE_FALLBACK_CHUNK",
        "research_label": config["research_label"],
    }
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
    console = {key: value for key, value in report.items()
               if key != "dataset_audits"}
    print(json.dumps({**console, "outputs": {
        key: str(value) for key, value in paths.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
