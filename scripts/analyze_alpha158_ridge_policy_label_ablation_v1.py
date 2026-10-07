#!/usr/bin/env python3
"""Explain annual outcomes and every frozen gate for the Ridge label ablation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


DEFAULT_ROOT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_ridge_policy_label_ablation_v1_20261007")
BASELINE = "ridge_excess20_common_v1"
CANDIDATE = "ridge_event_r60_v1"


def gate_audit(evidence, minimum_annualized_uplift_pp=0.5,
               top5_share_max=0.5):
    primary = evidence["cost_scenarios"]["25bp"]
    rows = [{
        "gate": "25bp_minimum_annualized_uplift",
        "passed": primary["annualized_uplift_pp"] >=
        minimum_annualized_uplift_pp,
        "actual": primary["annualized_uplift_pp"],
        "required": ">={:.3f}pp".format(minimum_annualized_uplift_pp),
    }, {
        "gate": "25bp_calmar_improves",
        "passed": primary["calmar_candidate"] > primary["calmar_baseline"],
        "actual": primary["calmar_candidate"]-primary["calmar_baseline"],
        "required": ">0",
    }, {
        "gate": "bootstrap20_annualized_lower_positive",
        "passed": evidence["bootstrap_20"]["annualized_return"]["lower"] > 0,
        "actual": evidence["bootstrap_20"]["annualized_return"]["lower"],
        "required": ">0",
    }, {
        "gate": "positive_year_count",
        "passed": evidence["positive_year_count"] >= 3,
        "actual": evidence["positive_year_count"],
        "required": ">=3",
    }, {
        "gate": "top5_positive_profit_share",
        "passed": evidence["top5_positive_profit_share"] <= top5_share_max,
        "actual": evidence["top5_positive_profit_share"],
        "required": "<={:.3f}".format(top5_share_max),
    }]
    comparisons = (
        ("cumulative_return", lambda item: item["cumulative_return_candidate"] >
         item["cumulative_return_baseline"], "candidate>baseline"),
        ("max_drawdown", lambda item: item["max_drawdown_candidate"] >=
         item["max_drawdown_baseline"], "candidate>=baseline"),
        ("es95", lambda item: item["es95_candidate"] >=
         item["es95_baseline"], "candidate>=baseline"),
        ("three_limit_down_return", lambda item:
         item["three_limit_down_return_candidate"] >=
         item["three_limit_down_return_baseline"], "candidate>=baseline"),
        ("average_exposure_gap", lambda item: abs(
         item["average_exposure_candidate"]-
         item["average_exposure_baseline"]) <= .01, "absolute_gap<=0.01"),
    )
    for cost, item in evidence["cost_scenarios"].items():
        for name, predicate, required in comparisons:
            if name == "average_exposure_gap":
                actual = abs(item["average_exposure_candidate"]-
                             item["average_exposure_baseline"])
            else:
                actual = (item[name+"_candidate"]-
                          item[name+"_baseline"])
            rows.append({"gate": "{}_{}".format(cost, name),
                         "passed": bool(predicate(item)), "actual": actual,
                         "required": required})
    return rows


def yearly_returns(root):
    frames = []
    for arm in (BASELINE, CANDIDATE):
        frame = pd.read_csv(root/(arm+"_25bp")/"daily_nav.csv")
        frame["year"] = frame.date//10000
        grouped = frame.groupby("year", sort=True).capital.agg(["first", "last"])
        grouped[arm] = (grouped["last"]/grouped["first"]-1.0)*100.0
        frames.append(grouped[[arm]])
    result = frames[0].join(frames[1], how="inner")
    result["uplift_pp"] = result[CANDIDATE]-result[BASELINE]
    return result.reset_index()


def _write_markdown(root, evidence, gates, yearly):
    costs = evidence["cost_scenarios"]
    lines = [
        "# Alpha158 Ridge事件标签消融", "",
        "- 决策：`REJECT_HISTORICAL_SCREEN`",
        "- 实验区间：2013-11-18至2026-09-30。",
        "- 仅替换训练目标；26因子、Ridge、训练行、执行与风险规则保持一致。",
        "", "## 账户结果", "",
        "| 成本 | 对照累计收益 | 事件标签累计收益 | 年化增量 | 对照回撤 | 事件标签回撤 |", "|---|---:|---:|---:|---:|---:|",
    ]
    for cost in ("25bp", "40bp", "60bp"):
        item = costs[cost]
        lines.append("| {} | {:.2%} | {:.2%} | {:+.3f}pp | {:.2%} | {:.2%} |".format(
            cost, item["cumulative_return_baseline"],
            item["cumulative_return_candidate"],
            item["annualized_uplift_pp"], item["max_drawdown_baseline"],
            item["max_drawdown_candidate"]))
    lines += ["", "## 未通过门槛", ""]
    for row in gates:
        if not row["passed"]:
            lines.append("- `{}`：实际 `{}`，要求 `{}`。".format(
                row["gate"], row["actual"], row["required"]))
    lines += ["", "## 25bp年度结果", "",
              "| 年度 | 对照 | 事件标签 | 增量 |", "|---|---:|---:|---:|"]
    for row in yearly.itertuples():
        lines.append("| {} | {:.2f}% | {:.2f}% | {:+.2f}pp |".format(
            int(row.year), getattr(row, BASELINE), getattr(row, CANDIDATE),
            row.uplift_pp))
    lines += [
        "", "历史总收益改善但置信区间、压力成本回撤和仓位可比性未同时通过，",
        "因此结果只支持保留为研究候选，不支持替换正式策略。", "",
    ]
    (root/"REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    evidence = json.loads((args.root/"evidence.json").read_text(encoding="utf-8"))
    gates = gate_audit(evidence)
    yearly = yearly_returns(args.root)
    yearly.to_csv(args.root/"yearly_metrics.csv", index=False)
    (args.root/"gate_audit.json").write_text(json.dumps(
        {"passed": sum(item["passed"] for item in gates),
         "failed": sum(not item["passed"] for item in gates),
         "gates": gates}, ensure_ascii=False, indent=2,
        allow_nan=False)+"\n", encoding="utf-8")
    _write_markdown(args.root, evidence, gates, yearly)
    print(json.dumps({"passed": sum(item["passed"] for item in gates),
                      "failed": sum(not item["passed"] for item in gates)},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
