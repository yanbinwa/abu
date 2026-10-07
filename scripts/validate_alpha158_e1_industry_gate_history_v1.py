#!/usr/bin/env python3
"""Compare frozen E1 and I1 entry gates on all_mean_rank over 2015--2026."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuAlphaIndustryGate import (  # noqa: E402
    IndustryExcessEntryGate, IndustryExcessHistory,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.backtest_alpha158_event_exit_only_2025_2026 import save_audit  # noqa: E402
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.validate_alpha158_all_factor_utility_v1 import paired_cagr_interval  # noqa: E402
from scripts.validate_alpha158_history_2015_v1 import segment_metrics  # noqa: E402


ARMS = ("A0_frozen", "E1_market_entry_gate", "I1_industry_relative_gate")
DEFAULT_CONFIG = ROOT / "configs/selection/alpha158_e1_industry_gate_history_v1.json"
DEFAULT_BASE = Path("/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007")
DEFAULT_E1 = Path("/Users/wjy/abu/backtests/alpha158_market_industry_partial_risk_history_v1_20261007")
DEFAULT_OUTPUT = Path("/Users/wjy/abu/backtests/alpha158_e1_industry_gate_history_v1_20261007")


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_json(path, payload):
    Path(path).write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")


def register(output, config, inputs):
    hashes = {str(path): digest(path) for path in inputs}
    if output.exists():
        registration = json.loads((output / "registration.json").read_text())
        changed = [path for path, value in hashes.items()
                   if registration["input_hashes"].get(path) != value]
        if changed:
            raise ValueError("registered input changed: " + ", ".join(changed))
        if (output / "completion.json").exists():
            raise FileExistsError("completed output already exists: " + str(output))
        return registration
    output.mkdir(parents=True)
    registration = {
        "registered_at": datetime.now().astimezone().isoformat(),
        "registered_before_results": True,
        "experiment": config,
        "input_hashes": hashes,
        "parameter_search_performed": False,
        "threshold_search_performed": False,
        "automatic_admission": False,
        "financial_factors_in_primary_result": False,
    }
    write_json(output / "registration.json", registration)
    return registration


def curve_metrics(curve, config):
    return segment_metrics(
        curve, config["evaluation_start_date"], config["evaluation_end_date"])


def load_frozen_curves(base, e1, cost):
    a0 = pd.read_csv(base / f"all_mean_rank_{int(cost)}bp/daily_nav.csv")
    e1_curve = pd.read_csv(e1 / f"E1_entry_gate_{int(cost)}bp/daily_nav.csv")
    source_result = pd.read_csv(e1 / "portfolio_results.csv")
    expected = source_result[
        source_result.arm.eq("E1_entry_gate") &
        source_result.slippage_bps.eq(float(cost))].iloc[0]
    actual = curve_metrics(e1_curve, {
        "evaluation_start_date": int(a0.date.min()),
        "evaluation_end_date": int(a0.date.max()),
    })
    for key in ("return_pct", "cagr_pct", "max_drawdown_pct"):
        if not np.isclose(float(expected[key]), float(actual[key]), rtol=0, atol=1e-9):
            raise ValueError(f"frozen E1 {key} mismatch at {cost}bp")
    return a0, e1_curve


def run_industry(config, args, panel, predictions, source, policy, risk):
    scores = rank_frame(
        predictions[["signal_asof", "symbol", "column", "all_mean_rank"]].rename(
            columns={"all_mean_rank": "alpha_score"}),
        "alpha_score", max(policy.entry_rank_limit, policy.retention_rank_limit))
    history = IndustryExcessHistory(panel, source)
    curves, decisions, results = {}, [], []
    for cost in config["costs_bps"]:
        gate = IndustryExcessEntryGate(
            history, float(config["industry_minimum_excess"]))
        result, audit = run_low_turnover(
            panel, scores, replace(source, label_slippage_bps=float(cost)),
            policy, risk, int(config["evaluation_end_date"]),
            review_overlay=gate)
        directory = args.output_dir / f"I1_industry_relative_gate_{int(cost)}bp"
        save_audit(directory, audit)
        gate_frame = pd.DataFrame(gate.entry_decisions)
        gate_frame.to_csv(directory / "industry_entry_decisions.csv", index=False)
        curve = audit["curve"].copy()
        curves[float(cost)] = curve
        metrics = curve_metrics(curve, config)
        row = dict(result)
        row.update(metrics)
        row.update({
            "arm": "I1_industry_relative_gate",
            "slippage_bps": float(cost),
            "gate_evaluations": int(len(gate_frame)),
            "gate_allowed": int(gate_frame.allowed.sum()) if len(gate_frame) else 0,
            "gate_rejected": int((~gate_frame.allowed).sum()) if len(gate_frame) else 0,
            "gate_missing": int(gate_frame.stock_excess_industry_20d.isna().sum()) if len(gate_frame) else 0,
        })
        results.append(row)
        if len(gate_frame):
            gate_frame.insert(0, "slippage_bps", float(cost))
            decisions.append(gate_frame)
        print(json.dumps({
            "arm": row["arm"], "cost": cost,
            "return_pct": row["return_pct"],
            "cagr_pct": row["cagr_pct"],
            "max_drawdown_pct": row["max_drawdown_pct"],
            "gate_rejected": row["gate_rejected"],
        }), flush=True)
    return pd.DataFrame(results), curves, (
        pd.concat(decisions, ignore_index=True) if decisions else pd.DataFrame())


def paired_tables(config, curves):
    rows, segments, annual_frames = [], [], []
    for cost in config["costs_bps"]:
        for arm in ARMS:
            curve = curves[(arm, float(cost))]
            row = curve_metrics(curve, config)
            row.update({"arm": arm, "slippage_bps": float(cost)})
            rows.append(row)
            for segment, bounds in (
                    ("primary_holdout", config["primary_holdout"]),
                    ("secondary_observed_history", config["secondary_observed_history"])):
                metric = segment_metrics(curve, bounds["start_date"], bounds["end_date"])
                metric.update({"arm": arm, "segment": segment,
                               "slippage_bps": float(cost)})
                segments.append(metric)
            annual = annual_returns(curve)
            annual["arm"] = arm
            annual["slippage_bps"] = float(cost)
            annual_frames.append(annual)
    return (pd.DataFrame(rows), pd.DataFrame(segments),
            pd.concat(annual_frames, ignore_index=True))


def compare(config, portfolio, segments, annual, curves):
    rows = []
    bounds = config["primary_holdout"]
    bootstrap = {
        "bootstrap_block_sessions": config["bootstrap_block_sessions"],
        "bootstrap_replicates": config["bootstrap_replicates"],
    }
    for cost in config["costs_bps"]:
        full = portfolio[portfolio.slippage_bps.eq(float(cost))].set_index("arm")
        primary = segments[
            segments.slippage_bps.eq(float(cost)) &
            segments.segment.eq("primary_holdout")].set_index("arm")
        base_part = curves[("A0_frozen", float(cost))]
        base_part = base_part[base_part.date.between(
            bounds["start_date"], bounds["end_date"])].reset_index(drop=True)
        for offset, arm in enumerate(ARMS[1:]):
            candidate = curves[(arm, float(cost))]
            candidate = candidate[candidate.date.between(
                bounds["start_date"], bounds["end_date"])].reset_index(drop=True)
            interval = paired_cagr_interval(
                base_part, candidate, bootstrap,
                int(config["bootstrap_seed"]) + int(cost) + offset * 1000)
            rows.append({
                "arm": arm, "slippage_bps": float(cost),
                "full_return_delta_pp": float(full.loc[arm, "return_pct"] - full.loc["A0_frozen", "return_pct"]),
                "full_cagr_delta_pp": float(full.loc[arm, "cagr_pct"] - full.loc["A0_frozen", "cagr_pct"]),
                "full_max_drawdown_improvement_pp": float(full.loc[arm, "max_drawdown_pct"] - full.loc["A0_frozen", "max_drawdown_pct"]),
                "full_es95_improvement_pp": float(full.loc[arm, "daily_expected_shortfall_95_pct"] - full.loc["A0_frozen", "daily_expected_shortfall_95_pct"]),
                "full_average_exposure_delta_pp": float(full.loc[arm, "average_exposure_pct"] - full.loc["A0_frozen", "average_exposure_pct"]),
                "primary_return_delta_pp": float(primary.loc[arm, "return_pct"] - primary.loc["A0_frozen", "return_pct"]),
                "primary_cagr_delta_pp": float(primary.loc[arm, "cagr_pct"] - primary.loc["A0_frozen", "cagr_pct"]),
                "primary_max_drawdown_improvement_pp": float(primary.loc[arm, "max_drawdown_pct"] - primary.loc["A0_frozen", "max_drawdown_pct"]),
                "primary_es95_improvement_pp": float(primary.loc[arm, "daily_expected_shortfall_95_pct"] - primary.loc["A0_frozen", "daily_expected_shortfall_95_pct"]),
                "primary_cagr_delta_ci95_low_pp": interval[0],
                "primary_cagr_delta_ci95_high_pp": interval[1],
            })
    comparisons = pd.DataFrame(rows)
    gates = {}
    annual25 = annual[annual.slippage_bps.eq(25.0)].pivot(
        index="year", columns="arm", values="return_pct")
    for arm in ARMS[1:]:
        candidate = comparisons[comparisons.arm.eq(arm)]
        primary25 = candidate[candidate.slippage_bps.eq(25.0)].iloc[0]
        positive_years = int((annual25[arm] > annual25.A0_frozen).sum())
        checks = {
            "primary_return_positive": bool(primary25.primary_return_delta_pp > 0),
            "primary_drawdown_not_worse": bool(primary25.primary_max_drawdown_improvement_pp >= 0),
            "primary_es95_not_worse": bool(primary25.primary_es95_improvement_pp >= 0),
            "primary_ci95_low_nonnegative": bool(primary25.primary_cagr_delta_ci95_low_pp >= 0),
            "all_cost_return_positive": bool(candidate.full_return_delta_pp.gt(0).all()),
            "all_cost_drawdown_not_worse": bool(candidate.full_max_drawdown_improvement_pp.ge(0).all()),
            "positive_calendar_years": bool(positive_years >= int(config["gates"]["positive_calendar_year_delta_min_count"])),
        }
        gates[arm] = {
            "checks": checks, "positive_calendar_years": positive_years,
            "passed": bool(all(checks.values())), "automatic_admission": False,
        }
    return comparisons, gates


def report_markdown(config, portfolio, segments, annual, comparisons, gates, industry_results):
    main = portfolio[portfolio.slippage_bps.eq(25.0)].set_index("arm")
    comparison25 = comparisons[comparisons.slippage_bps.eq(25.0)].set_index("arm")
    primary = segments[
        segments.slippage_bps.eq(25.0) &
        segments.segment.eq("primary_holdout")].set_index("arm")
    secondary = segments[
        segments.slippage_bps.eq(25.0) &
        segments.segment.eq("secondary_observed_history")].set_index("arm")
    labels = {
        "A0_frozen": "A0 all_mean_rank",
        "E1_market_entry_gate": "E1 市场风险暂停开仓",
        "I1_industry_relative_gate": "I1 行业相对强弱过滤",
    }
    lines = [
        "# all_mean_rank 两种入场风控：2015–2026冻结回测", "",
        "所有版本使用相同的七因子族等权排名、event_exit_only、2%组合开放风险上限和10只目标持仓。E1只在冻结的市场绝对风险事件后暂停下一交易日新开仓；I1只允许个股20日收益减同行业当日均值不低于-5个百分点的新开仓。没有重新选择阈值或调整退出参数。", "",
        "## 25bp全区间", "",
        "|策略|累计收益|年化收益|最大回撤|ES95|平均仓位|", "|---|---:|---:|---:|---:|---:|",
    ]
    for arm in ARMS:
        row = main.loc[arm]
        lines.append(f"|{labels[arm]}|{row.return_pct:+.2f}%|{row.cagr_pct:+.2f}%|{row.max_drawdown_pct:.2f}%|{row.daily_expected_shortfall_95_pct:.3f}%|{row.average_exposure_pct:.2f}%|")
    lines += ["", "## 分段结果（25bp）", "",
              "|策略|2015–2019收益|2015–2019回撤|2020–2026收益|2020–2026回撤|",
              "|---|---:|---:|---:|---:|"]
    for arm in ARMS:
        p, s = primary.loc[arm], secondary.loc[arm]
        lines.append(f"|{labels[arm]}|{p.return_pct:+.2f}%|{p.max_drawdown_pct:.2f}%|{s.return_pct:+.2f}%|{s.max_drawdown_pct:.2f}%|")
    lines += ["", "## 相对A0增量（25bp）", "",
              "|策略|全期收益增量|回撤改善|主验证收益增量|主验证CAGR增量95%区间|改善年度|通过冻结门槛|",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for arm in ARMS[1:]:
        row, gate = comparison25.loc[arm], gates[arm]
        lines.append(f"|{labels[arm]}|{row.full_return_delta_pp:+.2f}pp|{row.full_max_drawdown_improvement_pp:+.2f}pp|{row.primary_return_delta_pp:+.2f}pp|[{row.primary_cagr_delta_ci95_low_pp:+.2f}, {row.primary_cagr_delta_ci95_high_pp:+.2f}]pp|{gate['positive_calendar_years']}/12|{'是' if gate['passed'] else '否'}|")
    i25 = industry_results[industry_results.slippage_bps.eq(25.0)].iloc[0]
    lines += ["", "## 事件与数据边界", "",
              f"- I1在25bp路径评估了{int(i25.gate_evaluations)}个基础入场候选，拒绝{int(i25.gate_rejected)}个；缺失行业相对收益{int(i25.gate_missing)}个。",
              "- E1结果复用并重新核验已有冻结历史回测；其25bp路径只有3个暂停开仓日期、共阻止42个入场。",
              "- 行业横截面来自严格PIT深圳研究池，市场基准为沪深300；结果不能解释为完整沪深全市场行业验证。",
              "- 2015–2019作为主要跨周期检查，2020–2026为已观察补充；本实验不会自动替换模拟盘策略。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE)
    parser.add_argument("--e1-dir", type=Path, default=DEFAULT_E1)
    parser.add_argument("--source-config", type=Path, default=ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=ROOT / "configs/selection/risk_v1.json")
    parser.add_argument("--signal-dir", type=Path, default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path, default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if not (args.base_dir / "completion.json").exists() or not (args.e1_dir / "completion.json").exists():
        raise RuntimeError("frozen A0/E1 source is incomplete")
    predictions_path = args.base_dir / "ensemble_oos_predictions.csv.gz"
    inputs = [
        args.config, args.source_config, args.policy_config, args.risk_config,
        ROOT / "configs/selection/alpha158_market_industry_exit_v1.json",
        ROOT / "configs/selection/alpha158_market_industry_partial_risk_v1.json",
        ROOT / "abupy/AlphaBu/ABuAlphaIndustryGate.py",
        ROOT / "abupy/AlphaBu/ABuMarketIndustryExit.py",
        ROOT / "scripts/backtest_alpha158_lite_low_turnover_v3.py",
        args.base_dir / "completion.json", args.base_dir / "portfolio_results.csv",
        args.e1_dir / "completion.json", args.e1_dir / "portfolio_results.csv",
        predictions_path, Path(__file__),
    ]
    register(args.output_dir, config, inputs)
    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("source/policy configuration mismatch")
    predictions = pd.read_csv(predictions_path, dtype={"symbol": str})
    predictions = predictions[predictions.signal_asof.between(
        config["evaluation_start_date"], config["evaluation_end_date"])].copy()
    if predictions.empty or not (predictions.train_end < predictions.signal_asof).all():
        raise ValueError("strictly OOS predictions are required")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=int(config["panel_start_date"]),
        end_date=int(config["evaluation_end_date"]))
    industry_results, industry_curves, decisions = run_industry(
        config, args, panel, predictions, source, policy, risk)
    curves = {}
    for cost in config["costs_bps"]:
        a0, e1 = load_frozen_curves(args.base_dir, args.e1_dir, cost)
        curves[("A0_frozen", float(cost))] = a0
        curves[("E1_market_entry_gate", float(cost))] = e1
        curves[("I1_industry_relative_gate", float(cost))] = industry_curves[float(cost)]
    portfolio, segments, annual = paired_tables(config, curves)
    comparisons, gates = compare(config, portfolio, segments, annual, curves)
    portfolio.to_csv(args.output_dir / "portfolio_results.csv", index=False)
    segments.to_csv(args.output_dir / "segment_results.csv", index=False)
    annual.to_csv(args.output_dir / "annual_returns.csv", index=False)
    comparisons.to_csv(args.output_dir / "comparisons.csv", index=False)
    industry_results.to_csv(args.output_dir / "industry_gate_results.csv", index=False)
    decisions.to_csv(args.output_dir / "industry_entry_decisions.csv", index=False)
    write_json(args.output_dir / "gates.json", gates)
    report = report_markdown(
        config, portfolio, segments, annual, comparisons, gates, industry_results)
    (args.output_dir / "REPORT.md").write_text(report, encoding="utf-8")
    write_json(args.output_dir / "completion.json", {
        "status": "COMPLETE", "gates": gates,
        "decision": {arm: ("PASS" if gates[arm]["passed"] else "FAIL") for arm in ARMS[1:]},
        "automatic_admission": False,
        "market_breadth_scope": "strict_pit_shenzhen_strategy_universe",
        "benchmark_scope": "csi_300",
        "report_sha256": digest(args.output_dir / "REPORT.md"),
    })
    print(json.dumps({"status": "COMPLETE", "gates": gates}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
