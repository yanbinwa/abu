#!/usr/bin/env python3
"""Generate the v4 coverage gate report; never computes strategy returns."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_multifactor_v4.json"
DEFAULT_RESEARCH = Path("/Users/wjy/abu/data/selection_research")


def blocked_report(config, research_dir, facts_path, st_audit_path=None,
                   market_cap_audit_path=None, revision_audit_path=None):
    master_path = research_dir / "security_master.csv"
    master = pd.read_csv(master_path, dtype={"symbol": str})
    exchanges = master.symbol.str[:2].value_counts().sort_index().to_dict()
    price_summary_path = research_dir / "coverage_summary.json"
    price_summary = json.loads(price_summary_path.read_text(
        encoding="utf-8")) if price_summary_path.is_file() else {}
    facts_exist = facts_path.is_file() and facts_path.stat().st_size > 0
    st_audit = {}
    if st_audit_path is not None and Path(st_audit_path).is_file():
        st_audit = json.loads(Path(st_audit_path).read_text(encoding="utf-8"))
    st_passed = st_audit.get("gate_status") == "PASS_ST_EXCLUSION_DATA_GATE"
    cap_audit = {}
    if market_cap_audit_path is not None and Path(
            market_cap_audit_path).is_file():
        cap_audit = json.loads(Path(market_cap_audit_path).read_text(
            encoding="utf-8"))
    cap_summary = cap_audit.get("eligible_non_st_price_days", {})
    gates = config["coverage_gates"]
    total_coverage = float(cap_summary.get("total_market_cap_coverage", 0))
    float_coverage = float(cap_summary.get("float_market_cap_coverage", 0))
    total_passed = total_coverage >= float(gates.get(
        "total_market_cap_min", .995))
    float_passed = float_coverage >= float(gates.get(
        "float_market_cap_min", .995))
    reconciliation_passed = cap_audit.get(
        "reconciliation_gate_status") == "PASS_SHARE_RECONCILIATION"
    revision_audit = {}
    if revision_audit_path is not None and Path(revision_audit_path).is_file():
        revision_audit = json.loads(Path(revision_audit_path).read_text(
            encoding="utf-8"))
    revision_passed = revision_audit.get(
        "capability_gate_status") == "PASS_OFFICIAL_XBRL_REVISION_CAPABILITY"
    blockers = [
        "normalized_fundamental_facts_missing" if not facts_exist
        else "normalized_fundamental_coverage_not_materialized",
        "economic_theme_coverage_unavailable",
    ]
    if not total_passed:
        blockers.insert(1, "total_shares_and_total_market_cap_not_materialized")
    if not float_passed:
        blockers.insert(1, "float_shares_and_float_market_cap_below_threshold")
    if not reconciliation_passed:
        blockers.insert(1, "share_market_cap_reconciliation_conflicts")
    if not revision_passed:
        blockers.insert(1, "historical_fundamental_revision_versions_incomplete")
    if not st_passed:
        blockers.insert(1, "shanghai_historical_st_incomplete")
    return {
        "strategy_version": config["strategy_version"],
        "research_label": config["research_label"],
        "audit_scope": "pre-model full-universe input availability",
        "security_count": int(len(master)),
        "exchange_security_count": exchanges,
        "price_layer": price_summary,
        "fundamental_facts_path": str(facts_path),
        "fundamental_facts_available": facts_exist,
        "fundamental_revision_history": {
            "passed": revision_passed,
            "audit_path": (str(revision_audit_path)
                           if revision_audit_path else None),
            "historical_revision_versions_exposed": revision_audit.get(
                "historical_revision_versions_exposed"),
        },
        "theme_coverage": {
            theme: {"median": 0.0, "p10": 0.0, "passed": False}
            for theme in config["economic_themes"]
        },
        "st_coverage": {
            "shenzhen_history_present": bool(
                (research_dir / "sz_name_changes.csv").is_file()),
            "shanghai_history_complete": st_passed,
            "full_market_exclusion_gate_passed": st_passed,
            "exact_security_day_coverage": st_audit.get(
                "exact_st_coverage"),
            "audit_path": str(st_audit_path) if st_audit_path else None,
            "source_role": st_audit.get("source_role"),
            "unknown_st_policy": st_audit.get("unknown_st_policy", "exclude"),
            "spec_threshold": config["coverage_gates"][
                "lifecycle_and_st_known_min"],
        },
        "market_cap": {
            "float_market_cap_price_layer_available": bool(
                price_summary.get("field_coverage", {}).get(
                    "outstanding_share", 0) > 0),
            "total_market_cap_available": total_passed,
            "eligible_total_market_cap_coverage": total_coverage,
            "eligible_float_market_cap_coverage": float_coverage,
            "total_market_cap_threshold": float(gates.get(
                "total_market_cap_min", .995)),
            "float_market_cap_threshold": float(gates.get(
                "float_market_cap_min", .995)),
            "reconciliation_passed": reconciliation_passed,
            "audit_path": (str(market_cap_audit_path)
                           if market_cap_audit_path else None),
        },
        "blockers": blockers,
        "gate_status": "BLOCKED_DATA_GATE",
        "next_allowed_action": (
            "build and audit versioned full-universe structured fundamental "
            "facts and reconciled total/float share history; do not run D1/B2"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--facts", type=Path)
    parser.add_argument("--st-audit", type=Path)
    parser.add_argument("--market-cap-audit", type=Path)
    parser.add_argument("--revision-audit", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite v4 coverage audit")
    config = json.loads(args.config.read_text(encoding="utf-8"))
    facts = args.facts or (
        args.research_dir / "fundamental_normalized/fundamental_facts.jsonl")
    report = blocked_report(
        config, args.research_dir, facts, st_audit_path=args.st_audit,
        market_cap_audit_path=args.market_cap_audit,
        revision_audit_path=args.revision_audit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(
        report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
