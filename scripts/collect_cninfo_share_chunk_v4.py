#!/usr/bin/env python3
"""Collect one immutable full-market CNINFO share-capital chunk."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    ImmutableFundamentalRawStore, stable_json, structured_share_events,
)
from scripts.collect_structured_fundamental_v4 import (  # noqa: E402
    fetch_cninfo_share_response, trading_sessions,
)


DEFAULT_CONFIG = ROOT / "configs/selection/cninfo_share_capital_full_v4.json"


def collect(config, chunk_dir, output_dir, fetcher=None, raw_root=None):
    chunk_dir = Path(chunk_dir)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite CNINFO share chunk")
    output_dir.mkdir(parents=True)
    frozen = json.loads((chunk_dir / "pilot_symbols.json").read_text(
        encoding="utf-8"))
    if frozen.get("source_role") != config["source_role"]:
        raise ValueError("frozen chunk source role mismatch")
    symbols = frozen["symbols"]
    ranges = frozen["symbol_ranges"]
    sessions = trading_sessions(config["trading_sessions_source"])
    store = ImmutableFundamentalRawStore(raw_root or config["raw_root"])
    fetcher = fetcher or fetch_cninfo_share_response
    events = []
    audits = []
    failures = []
    empty_symbols = []
    for symbol in symbols:
        metadata = {"batch_id": None}
        start = ranges[symbol]["start_date"].replace("-", "")
        end = ranges[symbol]["end_date"].replace("-", "")
        try:
            response, request = fetcher(symbol[2:], start, end)
            http_status = int(response.status_code)
            payload = bytes(response.content)
            body = response.json() if http_status < 400 else {}
            rows = body.get("records") or []
            outcome = "HTTP_FAILURE" if http_status >= 400 else (
                "SUCCESS_NONEMPTY" if rows else "SUCCESS_EMPTY")
            _, metadata = store.capture(
                "cninfo_share_change", "shares", request, payload, outcome,
                config["source_version"], http_status=http_status,
                record_count=len(rows), unit="ten_thousand_shares",
                symbol=symbol)
            if http_status >= 400:
                raise RuntimeError("CNINFO_HTTP_{}".format(http_status))
            normalized, audit = structured_share_events(
                rows, symbol, metadata["payload_sha256"], sessions,
                unit_scale=float(config["fields"]["unit_scale"]))
            events.extend(event.__dict__ for event in normalized)
            audits.append({"symbol": symbol, **audit,
                           "batch_id": metadata["batch_id"]})
            if not normalized:
                empty_symbols.append(symbol)
        except Exception as error:
            if not isinstance(error, RuntimeError) or not str(error).startswith(
                    "CNINFO_HTTP_"):
                _, metadata = store.capture(
                    "cninfo_share_change", "shares", {
                        "symbol": symbol, "start_date": start,
                        "end_date": end}, b"", "NETWORK_FAILURE",
                    config["source_version"], error=error, symbol=symbol)
            failures.append({
                "symbol": symbol, "error_type": type(error).__name__,
                "error": str(error), "batch_id": metadata["batch_id"],
            })
    events.sort(key=lambda item: (
        item["symbol"], item["effective_at"], item["source_key"]))
    events_path = output_dir / "share_events.jsonl"
    report_path = output_dir / "collection_report.json"
    events_path.write_text("".join(
        stable_json(item) + "\n" for item in events), encoding="utf-8")
    report = {
        "config_version": config["config_version"],
        "source_version": config["source_version"],
        "source_role": config["source_role"],
        "symbol_count": len(symbols),
        "symbols_with_events": len(symbols) - len(empty_symbols) -
        len({item["symbol"] for item in failures}),
        "empty_symbols": empty_symbols,
        "share_event_count": len(events),
        "dataset_audits": audits, "failures": failures,
        "status": "COLLECTED_CNINFO_SHARE_CHUNK" if not failures else
        "BLOCKED_CNINFO_SHARE_CHUNK",
        "research_label": config["research_label"],
    }
    report_path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, {"events": events_path, "report": report_path}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chunk_dir", type=Path)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report, paths = collect(
        config, args.chunk_dir, args.output_dir, raw_root=args.raw_root)
    console = {key: value for key, value in report.items()
               if key != "dataset_audits"}
    print(json.dumps({**console, "outputs": {
        key: str(value) for key, value in paths.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
