#!/usr/bin/env python3
"""Collect immutable raw fundamental responses for the fixed v4 audit sample."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    ImmutableFundamentalRawStore,
)


DEFAULT_CONFIG = ROOT / "configs/selection/fundamental_pit_v4.json"
CNINFO_CATALOG = "https://www.cninfo.com.cn/new/data/szse_stock.json"
CNINFO_QUERY = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC = "https://static.cninfo.com.cn/"
SINA_REPORT = (
    "https://quotes.sina.cn/cn/api/openapi.php/"
    "CompanyFinanceService.getFinanceReport2022"
)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; abu-research/1.0)",
    "Referer": "https://www.cninfo.com.cn/",
    "X-Requested-With": "XMLHttpRequest",
}


def _json_count(response, path):
    value = response
    try:
        for key in path:
            value = value[key]
        return len(value)
    except (KeyError, TypeError):
        return None


def _capture_http(store, session, method, url, source, dataset, request,
                  source_version, symbol=None, records_path=None,
                  timeout=30):
    try:
        response = session.request(
            method, url, timeout=timeout,
            headers=HEADERS,
            params=request.get("params") if method == "GET" else None,
            data=request.get("data") if method == "POST" else None,
        )
    except requests.RequestException as error:
        return store.capture(
            source, dataset, request, b"", "NETWORK_FAILURE",
            source_version, error=error, symbol=symbol,
        )
    record_count = None
    outcome = "HTTP_FAILURE" if response.status_code >= 400 else \
        "SUCCESS_NONEMPTY"
    if response.status_code < 400 and records_path is not None:
        try:
            record_count = _json_count(response.json(), records_path)
            outcome = "SUCCESS_NONEMPTY" if record_count else "SUCCESS_EMPTY"
        except (ValueError, TypeError):
            outcome = "PARSE_FAILURE"
    elif response.status_code < 400 and not response.content:
        outcome = "SUCCESS_EMPTY"
    return store.capture(
        source, dataset, request, response.content, outcome, source_version,
        http_status=response.status_code, record_count=record_count,
        response_headers={
            key: response.headers.get(key)
            for key in ("Content-Type", "ETag", "Last-Modified")
            if response.headers.get(key) is not None
        },
        symbol=symbol,
    )


def collect_cninfo(config, store, session, symbols,
                   download_documents=False, max_documents_per_symbol=3):
    _, catalog_meta = _capture_http(
        store, session, "GET", CNINFO_CATALOG, "cninfo",
        "security_catalog", {"url": CNINFO_CATALOG, "params": {}},
        config["adapter_version"], records_path=("stockList",),
    )
    if catalog_meta["outcome"] != "SUCCESS_NONEMPTY":
        return
    catalog_payload = store.read_payload(catalog_meta)
    catalog_json = json.loads(catalog_payload)
    organisations = {
        item["code"]: item["orgId"] for item in catalog_json["stockList"]
    }
    period = config["audit_period"]
    start = period["start"]
    end = period["end"]
    date_range = "{}-{}-{}~{}-{}-{}".format(
        start[:4], start[4:6], start[6:], end[:4], end[4:6], end[6:])
    for sample in symbols:
        symbol = sample["symbol"]
        code = symbol[2:]
        if code not in organisations:
            store.capture(
                "cninfo", "announcement_query", {"symbol": symbol}, b"",
                "SOURCE_UNSUPPORTED", config["adapter_version"],
                record_count=0, symbol=symbol,
            )
            continue
        announcements = []
        for category in config["cninfo_categories"]:
            page = 1
            while True:
                form = {
                    "pageNum": str(page), "pageSize": "30",
                    "column": "szse", "tabName": "fulltext", "plate": "",
                    "stock": "{},{}".format(code, organisations[code]),
                    "searchkey": "", "secid": "", "category": category,
                    "trade": "", "seDate": date_range, "sortName": "",
                    "sortType": "", "isHLtitle": "true",
                }
                _, metadata = _capture_http(
                    store, session, "POST", CNINFO_QUERY, "cninfo",
                    "announcement_query", {"url": CNINFO_QUERY, "data": form},
                    config["adapter_version"], symbol=symbol,
                    records_path=("announcements",),
                )
                if metadata["outcome"] not in {
                        "SUCCESS_NONEMPTY", "SUCCESS_EMPTY"}:
                    break
                body = json.loads(store.read_payload(metadata))
                records = body.get("announcements") or []
                announcements.extend(records)
                total = int(body.get("totalAnnouncement") or 0)
                if page * 30 >= total:
                    break
                page += 1

        if not download_documents or not announcements:
            continue
        unique = {
            str(item.get("announcementId")): item for item in announcements
            if item.get("adjunctUrl")
        }
        ordered = sorted(
            unique.values(), key=lambda item: (
                0 if any(word in str(item.get("announcementTitle", ""))
                         for word in ("更正", "修订", "更新后", "补充")) else 1,
                -int(item.get("announcementTime") or 0),
                str(item.get("announcementId")),
            )
        )[:max_documents_per_symbol]
        for item in ordered:
            url = CNINFO_STATIC + str(item["adjunctUrl"]).lstrip("/")
            _capture_http(
                store, session, "GET", url, "cninfo", "announcement_document",
                {"url": url, "params": {},
                 "announcement_id": item.get("announcementId"),
                 "announcement_title": item.get("announcementTitle")},
                config["adapter_version"], symbol=symbol,
            )


def collect_sina(config, store, session, symbols):
    source_map = {"fzb": "fzb", "lrb": "lrb", "llb": "llb"}
    for sample in symbols:
        symbol = sample["symbol"]
        for statement in config["statement_types"]:
            params = {
                "paperCode": symbol, "source": source_map[statement],
                "type": "0", "page": "1", "num": "1000",
            }
            try:
                response = session.get(
                    SINA_REPORT, params=params, headers=HEADERS, timeout=60)
            except requests.RequestException as error:
                store.capture(
                    "sina", "statement_" + statement,
                    {"url": SINA_REPORT, "params": params}, b"",
                    "NETWORK_FAILURE", config["adapter_version"],
                    error=error, symbol=symbol,
                )
                continue
            outcome, count, currency = "HTTP_FAILURE", None, None
            if response.status_code < 400:
                try:
                    body = response.json()
                    reports = body.get("result", {}).get("data", {})
                    count = len(reports.get("report_date") or [])
                    outcome = "SUCCESS_NONEMPTY" if count else "NO_DISCLOSURE"
                    currencies = {
                        str(value.get("rCurrency"))
                        for value in (reports.get("report_list") or {}).values()
                        if value.get("rCurrency") is not None
                    }
                    currency = ",".join(sorted(currencies)) or None
                except (ValueError, TypeError):
                    outcome = "PARSE_FAILURE"
            store.capture(
                "sina", "statement_" + statement,
                {"url": SINA_REPORT, "params": params}, response.content,
                outcome, config["adapter_version"],
                http_status=response.status_code, record_count=count,
                currency=currency, unit="source_native",
                response_headers={"Content-Type": response.headers.get(
                    "Content-Type")}, symbol=symbol,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--sources", nargs="+", choices=("cninfo", "sina"),
                        default=("cninfo", "sina"))
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--download-documents", action="store_true")
    parser.add_argument("--max-documents-per-symbol", type=int, default=3)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    root = args.output_root or Path(config["raw_root"])
    store = ImmutableFundamentalRawStore(root)
    symbols = config["fixed_audit_sample"][:args.max_symbols]
    session = requests.Session()
    if "cninfo" in args.sources:
        collect_cninfo(
            config, store, session, symbols,
            download_documents=args.download_documents,
            max_documents_per_symbol=args.max_documents_per_symbol,
        )
    if "sina" in args.sources:
        collect_sina(config, store, session, symbols)
    print(json.dumps({
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "raw_root": str(root), "symbols": [item["symbol"] for item in symbols],
        "sources": list(args.sources), "status": "RAW_CAPTURE_COMPLETE",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
