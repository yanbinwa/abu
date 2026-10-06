#!/usr/bin/env python3
"""Audit merged fundamental coverage, chronology and field integrity."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/fundamental_full_market_quality_v1.json"
DEFAULT_OUTPUT = Path("/Users/wjy/abu/data/selection_research/fundamental_full_market_quality_20261006_v1")


def rows(path):
    with Path(path).open(encoding="utf-8") as source:
        for line in source:
            if line.strip():
                yield json.loads(line)


def audit(config, output):
    if output.exists():
        raise FileExistsError("refusing to overwrite fundamental quality audit")
    output.mkdir(parents=True)
    merged = Path(config["merged_root"])
    merge_report = json.loads((merged / "merge_report.json").read_text(
        encoding="utf-8"))
    facts_path = merged / "structured_fundamental_facts.jsonl"
    required = set(config["required_fields"])
    positive = set(config["strict_positive_fields"])
    source_keys = set()
    field_symbols = defaultdict(set)
    statement_symbols = defaultdict(set)
    field_counts = Counter()
    statement_counts = Counter()
    report_quarters = Counter()
    sign_anomalies = Counter()
    chronology = Counter()
    ingested = []
    announcements = []
    periods = []
    incomplete = restated = total = duplicates = 0
    latest_period = {}
    for item in rows(facts_path):
        total += 1
        key = str(item["source_key"])
        duplicates += int(key in source_keys)
        source_keys.add(key)
        symbol = str(item["symbol"])
        field = str(item["raw_field"])
        statement = str(item["statement_type"])
        period = date.fromisoformat(str(item["report_period"])[:10])
        notice = date.fromisoformat(str(item["notice_date"])[:10])
        update = date.fromisoformat(str(item["update_date"])[:10])
        announcement = date.fromisoformat(str(item["announcement"])[:10])
        field_symbols[field].add(symbol)
        statement_symbols[statement].add(symbol)
        field_counts[field] += 1
        statement_counts[statement] += 1
        report_quarters[str(period.month).zfill(2)] += 1
        incomplete += int(not item.get("revision_history_complete", False))
        restated += int(bool(item.get("is_restated")))
        chronology["update_before_notice"] += int(update < notice)
        chronology["announcement_not_later_date"] += int(
            announcement != max(notice, update))
        chronology["announcement_before_report_period"] += int(
            announcement < period)
        value = float(item["raw_value"])
        if field in positive and value <= 0:
            sign_anomalies[field] += 1
        ingested.append(str(item["ingested_at"]))
        announcements.append(announcement.isoformat())
        periods.append(period.isoformat())
        latest_period[(symbol, field)] = max(
            period.isoformat(), latest_period.get((symbol, field), ""))
    security_count = int(merge_report["security_count"])
    fields = []
    for field in config["required_fields"]:
        covered = len(field_symbols[field])
        fields.append({
            "field": field, "fact_count": int(field_counts[field]),
            "symbol_count": covered,
            "symbol_coverage": covered / security_count if security_count else 0,
            "nonpositive_count": int(sign_anomalies[field]),
        })
    chronology_clean = not any(chronology.values())
    required_present = set(field_counts) >= required
    collection_pass = str(merge_report["collection_gate_status"]).startswith("PASS")
    coverage_pass = str(merge_report["coverage_gate_status"]).startswith("PASS")
    historical_pit_pass = incomplete == 0
    report = {
        "merged_root": str(merged), "fact_count": total,
        "unique_source_key_count": len(source_keys),
        "duplicate_source_key_count": duplicates,
        "security_count": security_count,
        "field_audit": fields,
        "statement_fact_count": dict(sorted(statement_counts.items())),
        "statement_symbol_count": {
            key: len(value) for key, value in sorted(statement_symbols.items())},
        "report_month_fact_count": dict(sorted(report_quarters.items())),
        "chronology_violations": dict(chronology),
        "incomplete_revision_fact_count": incomplete,
        "restated_fact_count": restated,
        "restated_fact_share": restated / total if total else 0,
        "earliest_report_period": min(periods) if periods else None,
        "latest_report_period": max(periods) if periods else None,
        "earliest_announcement": min(announcements) if announcements else None,
        "latest_announcement": max(announcements) if announcements else None,
        "earliest_ingested_at": min(ingested) if ingested else None,
        "latest_ingested_at": max(ingested) if ingested else None,
        "collection_gate_passed": collection_pass,
        "coverage_gate_passed": coverage_pass,
        "field_and_chronology_gate_passed": bool(
            required_present and chronology_clean and not duplicates),
        "historical_point_in_time_gate_passed": historical_pit_pass,
        "historical_point_in_time_status": (
            "PASS_HISTORICAL_REVISION_CHAIN" if historical_pit_pass else
            "BLOCKED_MISSING_HISTORICAL_REVISION_CHAIN"),
        "forward_snapshot_status": (
            "ELIGIBLE_FOR_FORWARD_SHADOW_AFTER_INGESTION"
            if collection_pass and coverage_pass and chronology_clean and
            not duplicates else "BLOCKED_FORWARD_SNAPSHOT"),
        "forward_snapshot_not_before": max(ingested) if ingested else None,
        "automatic_strategy_use": False,
    }
    (output / "quality_report.json").write_text(json.dumps(
        report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    field_path = output / "field_coverage.csv"
    import pandas as pd
    pd.DataFrame(fields).to_csv(field_path, index=False)
    lines = ["# 全市场财务数据质量门禁", "",
             f"共核对 {security_count} 只证券、{total:,} 条字段事实和 {len(source_keys):,} 个唯一来源键。", "",
             "| 字段 | 事实数 | 股票覆盖 | 非正值异常 |", "|---|---:|---:|---:|"]
    for item in fields:
        lines.append(f"| {item['field']} | {item['fact_count']:,} | {item['symbol_coverage']:.2%} | {item['nonpositive_count']} |")
    lines += ["", "## 门禁", "",
              f"- 全分块采集：{'通过' if collection_pass else '阻塞'}",
              f"- 语句与字段覆盖：{'通过' if coverage_pass else '阻塞'}",
              f"- 日期顺序、必需字段和唯一键：{'通过' if report['field_and_chronology_gate_passed'] else '阻塞'}",
              f"- 历史修订链：{'通过' if historical_pit_pass else '阻塞'}",
              "",
              "即使采集、覆盖和字段质量通过，只要历史修订链不完整，就不能把当前修订值回填到历史公告日做基本面回测。数据只能从本次实际采集完成之后进入前瞻 shadow；其可用时间不得早于报告中的 `forward_snapshot_not_before`，且正式日线逻辑仍应映射到下一交易时点。", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    print(json.dumps(audit(config, args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
