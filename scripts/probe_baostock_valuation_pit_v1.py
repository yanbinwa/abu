#!/usr/bin/env python3
"""Probe BaoStock daily valuation history without granting PIT authority."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def stable_json(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")


def provider_code(symbol):
    value = str(symbol).lower()
    if len(value) != 8 or value[:2] not in {"sh", "sz"}:
        raise ValueError("invalid symbol " + value)
    return value[:2] + "." + value[2:]


def fetch_rows(api, symbol, fields, start_date, end_date):
    result = api.query_history_k_data_plus(
        provider_code(symbol), ",".join(fields),
        start_date=start_date, end_date=end_date,
        frequency="d", adjustflag="3")
    if result.error_code != "0":
        raise RuntimeError(result.error_code + ":" + result.error_msg)
    rows = []
    while result.next():
        rows.append(dict(zip(result.fields, result.get_row_data())))
    return rows


def load_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(
        encoding="utf-8").splitlines() if line.strip()]


def known_event_dates(symbol, structured_facts, share_events):
    reports = {
        str(row.get("announcement"))[:10]
        for row in structured_facts
        if row.get("symbol") == symbol and
        row.get("statement_type") == "income" and
        row.get("raw_field") == "PARENT_NETPROFIT" and
        row.get("announcement")
    }
    shares = {
        str(row.get("effective_at") or row.get("event_date"))[:10]
        for row in share_events
        if row.get("symbol") == symbol and
        (row.get("effective_at") or row.get("event_date"))
    }
    return sorted(reports | shares), sorted(reports), sorted(shares)


def parse_positive(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return np.nan
    return number if np.isfinite(number) and number > 0 else np.nan


def valuation_diagnostics(rows, threshold):
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame, []
    frame["date"] = pd.to_datetime(frame.date, errors="raise")
    frame["close_number"] = frame.close.map(parse_positive)
    frame["pe_number"] = frame.peTTM.map(parse_positive)
    frame["implied_eps"] = frame.close_number / frame.pe_number
    frame = frame.sort_values("date").reset_index(drop=True)
    previous = frame.implied_eps.shift(1)
    relative = (frame.implied_eps / previous - 1).abs()
    changes = frame.loc[
        frame.implied_eps.notna() & previous.notna() &
        (relative > float(threshold)), ["date", "implied_eps"]].copy()
    changes["relative_change"] = relative.loc[changes.index]
    return frame, changes.to_dict("records")


def match_changes(changes, events, window_days):
    parsed_events = [date.fromisoformat(value) for value in events]
    output = []
    for row in changes:
        changed = pd.Timestamp(row["date"]).date()
        candidates = [event for event in parsed_events
                      if event <= changed <= event + timedelta(
                          days=int(window_days))]
        output.append({
            "date": changed.isoformat(),
            "implied_eps": float(row["implied_eps"]),
            "relative_change": float(row["relative_change"]),
            "matched_event": max(candidates).isoformat()
            if candidates else None,
        })
    return output


def summarize_symbol(symbol, rows, events, config):
    frame, changes = valuation_diagnostics(
        rows, config["implied_eps_change_threshold"])
    matched = match_changes(
        changes, events, config["event_match_calendar_days"])
    trading = frame.tradestatus.eq("1") if len(frame) else pd.Series(dtype=bool)
    pe_valid = frame.pe_number.notna() if len(frame) else pd.Series(dtype=bool)
    valid_trading = int(trading.sum())
    return {
        "symbol": symbol,
        "rows": int(len(frame)),
        "first_date": frame.date.min().date().isoformat() if len(frame) else None,
        "last_date": frame.date.max().date().isoformat() if len(frame) else None,
        "trading_rows": valid_trading,
        "positive_pe_rows": int((trading & pe_valid).sum()) if len(frame) else 0,
        "positive_pe_coverage": float((trading & pe_valid).sum()/valid_trading)
        if valid_trading else None,
        "blank_or_nonpositive_pe_rows": int((trading & ~pe_valid).sum())
        if len(frame) else 0,
        "implied_eps_change_count": int(len(matched)),
        "event_matched_change_count": int(sum(
            row["matched_event"] is not None for row in matched)),
        "event_matched_change_rate": float(sum(
            row["matched_event"] is not None for row in matched)/len(matched))
        if matched else None,
        "implied_eps_changes": matched,
    }


def audit(config, rows_by_symbol, replay_hashes, failures,
          structured_facts, share_events):
    summaries = []
    for symbol in sorted(rows_by_symbol):
        events, report_events, capital_events = known_event_dates(
            symbol, structured_facts, share_events)
        value = summarize_symbol(symbol, rows_by_symbol[symbol], events, config)
        value["known_report_event_count"] = len(report_events)
        value["known_share_event_count"] = len(capital_events)
        summaries.append(value)
    nonempty = [row for row in summaries if row["rows"]]
    weighted_trading = sum(row["trading_rows"] for row in nonempty)
    weighted_positive = sum(row["positive_pe_rows"] for row in nonempty)
    changes = sum(row["implied_eps_change_count"] for row in nonempty)
    matched = sum(row["event_matched_change_count"] for row in nonempty)
    exact_replay = all(value["first"] == value["second"]
                       for value in replay_hashes.values())
    coverage = len(nonempty)/len(summaries) if summaries else 0.0
    evidence_gate = (
        not failures and exact_replay and
        coverage >= float(config["coverage_threshold"]))
    return {
        "probe_id": config["probe_id"],
        "source_role": config["source_role"],
        "sample_symbols": len(summaries),
        "nonempty_symbols": len(nonempty),
        "nonempty_symbol_coverage": coverage,
        "weighted_positive_pe_coverage": (
            weighted_positive/weighted_trading if weighted_trading else None),
        "implied_eps_change_count": changes,
        "known_event_matched_change_count": matched,
        "known_event_matched_change_rate": matched/changes if changes else None,
        "exact_immediate_replay": exact_replay,
        "failure_count": len(failures),
        "auxiliary_evidence_gate": "PASS" if evidence_gate else "FAIL",
        "pit_admission_gate": "FAIL_NO_HISTORICAL_REVISION_LINEAGE",
        "historical_revision_lineage_claimed": bool(
            config["historical_revision_lineage_claimed"]),
        "interpretation": (
            "Immediate replay and coverage can qualify BaoStock valuation data "
            "for auxiliary cross-checks only. They cannot establish what value "
            "was served before later financial restatements."),
        "limitations": [
            "provider documentation does not expose valuation revision lineage",
            "two immediate downloads do not test historical snapshot stability",
            "blank PE conflates loss-making firms and unavailable values",
            "known event matching uses incomplete structured revision history",
            "daily PE cannot replace versioned announcement facts",
        ],
        "symbols": summaries,
        "failures": failures,
    }


def run(config, output):
    import baostock as bs

    pilot = json.loads(Path(config["pilot_symbols"]).read_text(encoding="utf-8"))
    symbols = tuple(str(value).lower() for value in pilot["symbols"])
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "registration.json", {
        "config": config, "config_sha256": sha256_text(stable_json(config)),
        "symbols": list(symbols), "registered_before_collection": True,
    })
    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError("BaoStock login failed: " + login.error_msg)
    rows_by_symbol, replay_hashes, failures = {}, {}, []
    try:
        for index, symbol in enumerate(symbols, 1):
            start = config["symbol_start_overrides"].get(
                symbol, config["start_date"])
            try:
                first = fetch_rows(
                    bs, symbol, config["fields"], start, config["end_date"])
                second = fetch_rows(
                    bs, symbol, config["fields"], start, config["end_date"])
                first_hash = sha256_text(stable_json(first))
                second_hash = sha256_text(stable_json(second))
                rows_by_symbol[symbol] = first
                replay_hashes[symbol] = {
                    "first": first_hash, "second": second_hash,
                    "rows": len(first),
                }
            except Exception as error:
                failures.append({
                    "symbol": symbol, "error_type": type(error).__name__,
                    "error": str(error),
                })
            print(json.dumps({
                "completed": index, "total": len(symbols),
                "symbol": symbol,
                "rows": len(rows_by_symbol.get(symbol, [])),
            }), flush=True)
    finally:
        bs.logout()

    records = []
    for symbol in sorted(rows_by_symbol):
        records.extend({"symbol": symbol, **row}
                       for row in rows_by_symbol[symbol])
    (output / "valuation_daily.jsonl").write_text(
        "".join(stable_json(row) + "\n" for row in records),
        encoding="utf-8")
    write_json(output / "replay_hashes.json", replay_hashes)
    facts = load_jsonl(config["structured_facts"])
    shares = load_jsonl(config["share_events"])
    report = audit(
        config, rows_by_symbol, replay_hashes, failures, facts, shares)
    write_json(output / "audit.json", report)
    write_json(output / "completion.json", {
        "status": "COMPLETE", "auxiliary_evidence_gate":
            report["auxiliary_evidence_gate"],
        "pit_admission_gate": report["pit_admission_gate"],
        "audit_sha256": sha256_text(stable_json(report)),
    })
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/baostock_valuation_pit_probe_v1.json")
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/data/selection_research/baostock_valuation_pit_probe_20261005_v1"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    report = run(config, args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
