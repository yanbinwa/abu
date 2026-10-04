#!/usr/bin/env python3
"""Collect a deterministic structured-fundamental pilot without PDFs."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    ImmutableFundamentalRawStore, stable_json, structured_share_events,
    structured_statement_records,
)


DEFAULT_CONFIG = (
    ROOT / "configs/selection/fundamental_structured_pit_v4.json"
)
DEFAULT_MAPPING = (
    ROOT / "configs/selection/fundamental_structured_field_mapping_v4.json"
)


def board_bucket(symbol):
    symbol = str(symbol).lower()
    if symbol.startswith("sh688"):
        return "star"
    if symbol.startswith("sz300"):
        return "chinext"
    if symbol.startswith("sh"):
        return "sh_main"
    if symbol.startswith("sz"):
        return "sz_main"
    return "unsupported"


def select_pilot_symbols(stock_info, sample_size, delisted_anchors):
    """Pick evenly spaced listing-age observations within four board strata."""
    anchors = tuple(dict.fromkeys(str(value).lower()
                                  for value in delisted_anchors))
    listed_target = int(sample_size) - len(anchors)
    if listed_target <= 0:
        raise ValueError("pilot sample must include listed securities")
    frame = stock_info.copy()
    required = {"symbol", "list_date"}
    if not required.issubset(frame.columns):
        raise ValueError("listed source fields mismatch")
    frame["symbol"] = frame.symbol.astype(str).str.lower()
    frame = frame[~frame.symbol.isin(anchors)].copy()
    frame["board"] = frame.symbol.map(board_bucket)
    frame = frame[frame.board != "unsupported"]
    frame["list_date"] = pd.to_datetime(frame.list_date, errors="coerce")
    frame = frame.dropna(subset=["list_date"]).sort_values(
        ["board", "list_date", "symbol"])
    boards = ("sh_main", "sz_main", "chinext", "star")
    base, remainder = divmod(listed_target, len(boards))
    selected = []
    strata = {}
    for index, board in enumerate(boards):
        quota = base + int(index < remainder)
        group = frame[frame.board == board].reset_index(drop=True)
        if len(group) < quota:
            raise ValueError("insufficient pilot symbols for " + board)
        positions = np.linspace(0, len(group) - 1, quota, dtype=int)
        values = group.iloc[positions].symbol.tolist()
        selected.extend(values)
        strata[board] = values
    result = tuple(selected + list(anchors))
    if len(result) != int(sample_size) or len(set(result)) != len(result):
        raise ValueError("pilot symbol selection is not unique and complete")
    return result, strata


def frame_snapshot(frame):
    clean = frame.astype(object).where(pd.notna(frame), None)
    return {
        "columns": [str(value) for value in clean.columns],
        "records": clean.to_dict("records"),
    }


def trading_sessions(path):
    frame = pd.read_csv(path, usecols=["date_time"])
    return sorted(set(pd.to_datetime(
        frame.date_time, errors="raise").dt.date.tolist()))


def load_akshare_adapters():
    import akshare as ak
    return {
        ("income", False): ak.stock_profit_sheet_by_report_em,
        ("balance", False): ak.stock_balance_sheet_by_report_em,
        ("cashflow", False): ak.stock_cash_flow_sheet_by_report_em,
        ("income", True): ak.stock_profit_sheet_by_report_delisted_em,
        ("balance", True): ak.stock_balance_sheet_by_report_delisted_em,
        ("cashflow", True): ak.stock_cash_flow_sheet_by_report_delisted_em,
    }


def fetch_cninfo_share_response(code, start_date, end_date, timeout=30):
    import py_mini_racer
    import requests
    from akshare.datasets import get_ths_js

    runtime = py_mini_racer.MiniRacer()
    runtime.eval(Path(get_ths_js("cninfo.js")).read_text(encoding="utf-8"))
    encoded_key = runtime.call("getResCode1")
    url = "https://webapi.cninfo.com.cn/api/stock/p_stock2215"
    params = {
        "scode": code,
        "sdate": "{}-{}-{}".format(
            start_date[:4], start_date[4:6], start_date[6:]),
        "edate": "{}-{}-{}".format(
            end_date[:4], end_date[4:6], end_date[6:]),
    }
    headers = {
        "Accept-Enckey": encoded_key,
        "Origin": "https://webapi.cninfo.com.cn",
        "Referer": "https://webapi.cninfo.com.cn/",
        "User-Agent": "Mozilla/5.0 (compatible; abu-research/1.0)",
        "X-Requested-With": "XMLHttpRequest",
    }
    response = requests.post(
        url, params=params, headers=headers, timeout=timeout)
    return response, {"url": url, "params": params}


def collect(config, mapping, output_dir, raw_root=None, max_symbols=None,
            only_symbols=None, adapters=None, share_fetcher=None):
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError("refusing to overwrite structured pilot output")
    output_dir.mkdir(parents=True)
    raw_root = Path(raw_root or config["raw_root"])
    store = ImmutableFundamentalRawStore(raw_root)
    listed = pd.read_csv(config["pilot"]["listed_source"], dtype=str)
    symbols, strata = select_pilot_symbols(
        listed, config["pilot"]["sample_size"],
        config["pilot"]["delisted_anchors"])
    if only_symbols:
        requested = tuple(str(value).lower() for value in only_symbols)
        unknown = set(requested) - set(symbols)
        if unknown:
            raise ValueError("symbols outside frozen pilot: " +
                             ",".join(sorted(unknown)))
        symbols = requested
    if max_symbols is not None:
        symbols = symbols[:int(max_symbols)]
    delisted = set(config["pilot"]["delisted_anchors"])
    adapters = adapters or load_akshare_adapters()
    share_fetcher = share_fetcher or fetch_cninfo_share_response
    sessions = trading_sessions(config["trading_sessions_source"])
    start_date = sessions[0].strftime("%Y%m%d")
    end_date = sessions[-1].strftime("%Y%m%d")
    ingested_at = datetime.now(timezone.utc).isoformat()
    extracted = []
    share_records = []
    audits = []
    failures = []

    for symbol in symbols:
        provider_symbol = symbol[:2].upper() + symbol[2:]
        is_delisted = symbol in delisted
        for statement_type in ("income", "balance", "cashflow"):
            adapter = adapters[(statement_type, is_delisted)]
            request = {
                "adapter": adapter.__name__, "symbol": provider_symbol,
                "statement_type": statement_type,
                "adapter_snapshot": True,
            }
            try:
                frame = adapter(provider_symbol)
                snapshot = frame_snapshot(frame)
                payload = stable_json(snapshot).encode("utf-8")
                outcome = "SUCCESS_NONEMPTY" if len(frame) else "SUCCESS_EMPTY"
                _, metadata = store.capture(
                    "eastmoney_structured", "statement_" + statement_type,
                    request, payload, outcome, config["adapter_version"],
                    record_count=len(frame), currency="CNY", unit="yuan",
                    symbol=symbol,
                )
                records, audit = structured_statement_records(
                    snapshot["records"], statement_type, symbol, mapping,
                    metadata["payload_sha256"], ingested_at)
                extracted.extend(records)
                audits.append({"symbol": symbol,
                               "dataset": statement_type, **audit})
            except Exception as error:  # fail closed and preserve the attempt
                _, metadata = store.capture(
                    "eastmoney_structured", "statement_" + statement_type,
                    request, b"", "PARSE_FAILURE",
                    config["adapter_version"], error=error, symbol=symbol)
                failures.append({
                    "symbol": symbol, "dataset": statement_type,
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "batch_id": metadata["batch_id"],
                })

        try:
            response, request = share_fetcher(
                symbol[2:], start_date, end_date)
            http_status = int(response.status_code)
            payload = bytes(response.content)
            body = response.json() if http_status < 400 else {}
            rows = body.get("records") or []
            outcome = "HTTP_FAILURE" if http_status >= 400 else (
                "SUCCESS_NONEMPTY" if rows else "SUCCESS_EMPTY")
            _, metadata = store.capture(
                "cninfo_share_change", "shares", request, payload, outcome,
                config["adapter_version"], http_status=http_status,
                record_count=len(rows), unit="ten_thousand_shares",
                symbol=symbol,
            )
            if http_status < 400:
                events, audit = structured_share_events(
                    rows, symbol, metadata["payload_sha256"], sessions)
                share_records.extend({**event.__dict__} for event in events)
                audits.append({"symbol": symbol, "dataset": "shares", **audit})
        except Exception as error:  # fail closed and preserve the attempt
            _, metadata = store.capture(
                "cninfo_share_change", "shares", {"symbol": symbol}, b"",
                "NETWORK_FAILURE", config["adapter_version"], error=error,
                symbol=symbol)
            failures.append({
                "symbol": symbol, "dataset": "shares",
                "error_type": type(error).__name__, "error": str(error),
                "batch_id": metadata["batch_id"],
            })

    paths = {
        "symbols": output_dir / "pilot_symbols.json",
        "facts": output_dir / "structured_extracted.jsonl",
        "shares": output_dir / "share_events.jsonl",
        "report": output_dir / "collection_report.json",
    }
    paths["symbols"].write_text(json.dumps({
        "symbols": list(symbols), "strata": strata,
        "delisted_anchors": list(delisted),
    }, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["facts"].write_text("".join(
        stable_json(item) + "\n" for item in extracted), encoding="utf-8")
    paths["shares"].write_text("".join(
        stable_json(item) + "\n" for item in share_records), encoding="utf-8")
    report = {
        "config_version": config["config_version"],
        "adapter_version": config["adapter_version"],
        "research_label": config["research_label"],
        "symbol_count": len(symbols), "fact_value_count": len(extracted),
        "share_event_count": len(share_records),
        "dataset_audits": audits, "failures": failures,
        "deferred": config["deferred"],
        "status": "COLLECTED_STRUCTURED_PILOT_RESEARCH_ONLY",
    }
    paths["report"].write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return report, paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--max-symbols", type=int)
    parser.add_argument("--symbols", nargs="+")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    report, paths = collect(
        config, mapping, args.output_dir, raw_root=args.raw_root,
        max_symbols=args.max_symbols, only_symbols=args.symbols)
    print(json.dumps({**report, "outputs": {
        key: str(value) for key, value in paths.items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if not report["failures"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
