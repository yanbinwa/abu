#!/usr/bin/env python3
"""Probe whether the official SSE XBRL catalog exposes report versions."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    ImmutableFundamentalRawStore,
)


DEFAULT_CONFIG = ROOT / "configs/selection/sse_xbrl_revision_probe_v4.json"


def fetch_catalog(config, symbol, timeout=30):
    import requests
    params = {
        "isPagination": "true", "sqlId": "COMMON_SSE_PL_XBRL_YJGL",
        "stockId": symbol, "startYear": str(config["start_year"]),
        "endYear": str(config["end_year"]),
        "reportPeriodId": config["report_period_ids"], "type": "inParams",
        "isMax": "", "pageHelp.pageSize": "200",
        "pageHelp.pageNo": "1", "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
    }
    headers = {
        "Referer": config["referer"],
        "User-Agent": "Mozilla/5.0 (compatible; abu-research/1.0)",
    }
    response = requests.get(
        config["endpoint"], params=params, headers=headers, timeout=timeout)
    return response, {"url": config["endpoint"], "params": params}


def probe(config, output_dir, fetcher=None, raw_root=None):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite SSE XBRL probe")
    output_dir.mkdir(parents=True)
    store = ImmutableFundamentalRawStore(raw_root or config["raw_root"])
    fetcher = fetcher or (lambda symbol: fetch_catalog(config, symbol))
    rows = []
    failures = []
    batches = []
    for symbol in config["symbols"]:
        metadata = {"batch_id": None}
        try:
            response, request = fetcher(symbol)
            status = int(response.status_code)
            payload = bytes(response.content)
            body = response.json() if status < 400 else {}
            result = body.get("result") or []
            outcome = "HTTP_FAILURE" if status >= 400 else (
                "SUCCESS_NONEMPTY" if result else "SUCCESS_EMPTY")
            _, metadata = store.capture(
                "sse_official_xbrl", "report_catalog", request, payload,
                outcome, config["source_version"], http_status=status,
                record_count=len(result), symbol="sh" + symbol)
            if status >= 400:
                raise RuntimeError("SSE_XBRL_HTTP_{}".format(status))
            rows.extend(result)
            batches.append({
                "symbol": "sh" + symbol, "batch_id": metadata["batch_id"],
                "record_count": len(result),
                "payload_sha256": metadata["payload_sha256"],
            })
        except Exception as error:
            if metadata["batch_id"] is None:
                _, metadata = store.capture(
                    "sse_official_xbrl", "report_catalog",
                    {"symbol": symbol}, b"", "NETWORK_FAILURE",
                    config["source_version"], error=error,
                    symbol="sh" + symbol)
            failures.append({
                "symbol": "sh" + symbol,
                "error_type": type(error).__name__, "error": str(error),
                "batch_id": metadata["batch_id"],
            })
    identities = defaultdict(list)
    observed_fields = set()
    for row in rows:
        observed_fields.update(row)
        identity = (
            str(row.get("STOCK_ID")), str(row.get("REPORT_YEAR")),
            str(row.get("REPORT_PERIOD_ID")))
        identities[identity].append(row)
    duplicates = {
        ":".join(identity): values for identity, values in identities.items()
        if len(values) > 1
    }
    required = set(config["required_revision_fields"])
    available_revision_fields = sorted(required.intersection(observed_fields))
    unique_disclosure_dates = {
        key: sorted({str(row.get("ACTUAL_DATE")) for row in values})
        for key, values in duplicates.items()
    }
    version_history_exposed = bool(available_revision_fields) or any(
        len(values) > 1 for values in unique_disclosure_dates.values())
    field_counts = Counter(field for row in rows for field in row)
    report = {
        "config_version": config["config_version"],
        "source_version": config["source_version"],
        "source_role": config["source_role"],
        "symbols": ["sh" + value for value in config["symbols"]],
        "report_count": len(rows), "identity_count": len(identities),
        "duplicate_identity_count": len(duplicates),
        "duplicate_identity_disclosure_dates": unique_disclosure_dates,
        "observed_fields": sorted(observed_fields),
        "field_presence_counts": dict(sorted(field_counts.items())),
        "required_revision_fields": sorted(required),
        "available_revision_fields": available_revision_fields,
        "historical_revision_versions_exposed": version_history_exposed,
        "raw_batches": batches, "failures": failures,
        "capability_gate_status": (
            "PASS_OFFICIAL_XBRL_REVISION_CAPABILITY"
            if version_history_exposed and not failures else
            "BLOCKED_OFFICIAL_XBRL_REVISION_CAPABILITY"),
        "interpretation": (
            "catalog rows without a version identifier cannot establish "
            "historical original-versus-corrected values"),
        "research_label": config["research_label"],
    }
    report_path = output_dir / "revision_capability_report.json"
    rows_path = output_dir / "catalog_rows.jsonl"
    rows_path.write_text("".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8")
    report_path.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, {"rows": rows_path, "report": report_path}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report, paths = probe(
        config, args.output_dir, raw_root=args.raw_root)
    print(json.dumps({**report, "outputs": {
        key: str(value) for key, value in paths.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["capability_gate_status"].startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
