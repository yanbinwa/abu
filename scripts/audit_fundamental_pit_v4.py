#!/usr/bin/env python3
"""Audit fixed-sample fundamental raw captures without computing returns."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/fundamental_pit_v4.json"


def _load_batches(raw_root):
    result = []
    for path in sorted((Path(raw_root) / "batches").rglob("*.json")):
        item = json.loads(path.read_text(encoding="utf-8"))
        item["metadata_path"] = str(path)
        result.append(item)
    return result


def _blob_json(raw_root, item):
    return json.loads((Path(raw_root) / item["blob_path"]).read_bytes())


def build_source_audit(config, raw_root):
    batches = _load_batches(raw_root)
    expected = [item["symbol"] for item in config["fixed_audit_sample"]]
    outcomes = defaultdict(lambda: defaultdict(int))
    cninfo_symbols = set()
    sina_symbols = set()
    announcement_count = revision_count = document_count = 0
    announcement_dates = 0
    first_announcement = None
    latest_announcement = None
    failures = []
    currencies = set()

    for item in batches:
        outcomes[item["source"]][item["outcome"]] += 1
        if item["outcome"] in {"NETWORK_FAILURE", "HTTP_FAILURE",
                               "PARSE_FAILURE"}:
            failures.append({
                "source": item["source"], "dataset": item["dataset"],
                "symbol": item.get("symbol"), "outcome": item["outcome"],
            })
        if item.get("currency"):
            currencies.update(str(item["currency"]).split(","))
        if item["source"] == "cninfo" and \
                item["dataset"] == "announcement_query" and \
                item["outcome"] in {"SUCCESS_NONEMPTY", "SUCCESS_EMPTY"}:
            if item.get("symbol"):
                cninfo_symbols.add(item["symbol"])
            body = _blob_json(raw_root, item)
            for record in body.get("announcements") or []:
                announcement_count += 1
                timestamp = record.get("announcementTime")
                if timestamp is not None:
                    announcement_dates += 1
                    first_announcement = timestamp if first_announcement is None \
                        else min(first_announcement, timestamp)
                    latest_announcement = timestamp if latest_announcement is None \
                        else max(latest_announcement, timestamp)
                title = str(record.get("announcementTitle") or "")
                if any(word in title for word in ("更正", "修订", "更新后", "补充")):
                    revision_count += 1
        if item["source"] == "cninfo" and \
                item["dataset"] == "announcement_document" and \
                item["outcome"] == "SUCCESS_NONEMPTY":
            document_count += 1
        if item["source"] == "sina" and item["dataset"].startswith(
                "statement_") and item["outcome"] == "SUCCESS_NONEMPTY":
            sina_symbols.add(item["symbol"])

    delisted = {
        item["symbol"] for item in config["fixed_audit_sample"]
        if "delisted" in item["history_flag"]
    }
    blockers = []
    missing_cninfo = sorted(set(expected) - cninfo_symbols)
    if missing_cninfo:
        blockers.append("CNINFO公告查询未覆盖固定样本: " + ",".join(missing_cninfo))
    if not delisted.issubset(cninfo_symbols):
        blockers.append("CNINFO未覆盖全部退市固定样本")
    if announcement_count == 0:
        blockers.append("CNINFO未返回任何历史公告")
    if announcement_dates < announcement_count:
        blockers.append("CNINFO公告存在缺失公告时间")
    if revision_count == 0:
        blockers.append("固定样本未观察到可区分的更正或修订版本")
    if document_count < len(expected):
        blockers.append("每个固定样本至少一份原始公告文档尚未归档")
    if failures:
        blockers.append("采集存在网络、HTTP或解析失败")
    if set(expected) - sina_symbols:
        blockers.append("辅助财务报表未覆盖全部固定样本")

    return {
        "config_version": config["config_version"],
        "research_label": config["research_label"],
        "raw_root": str(Path(raw_root).resolve()),
        "fixed_sample_size": len(expected),
        "fixed_sample_symbols": expected,
        "source_outcomes": {
            source: dict(sorted(values.items()))
            for source, values in sorted(outcomes.items())
        },
        "cninfo": {
            "covered_symbols": sorted(cninfo_symbols),
            "announcement_count": announcement_count,
            "announcement_timestamp_count": announcement_dates,
            "revision_or_correction_count": revision_count,
            "downloaded_document_count": document_count,
            "first_announcement_epoch_ms": first_announcement,
            "latest_announcement_epoch_ms": latest_announcement,
        },
        "sina": {
            "covered_symbols": sorted(sina_symbols),
            "currencies": sorted(currencies),
            "latest_restated_value_risk": True,
            "eligible_to_override_cninfo": False,
        },
        "failures": failures,
        "blockers": blockers,
        "gate_status": (
            "ELIGIBLE_FOR_M3_RAW_LAYER_ONLY" if not blockers
            else "BLOCKED_DATA_GATE"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--raw-root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    raw_root = args.raw_root or Path(config["raw_root"])
    report = build_source_audit(config, raw_root)
    encoded = json.dumps(report, ensure_ascii=False, indent=2,
                         sort_keys=True) + "\n"
    if args.output:
        if args.output.exists():
            raise FileExistsError("refusing to overwrite source audit")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if report["gate_status"] != "BLOCKED_DATA_GATE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
