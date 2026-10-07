#!/usr/bin/env python3
"""Validate the frozen partial market/industry risk overlay over 2015--2026."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuMarketIndustryExit import (  # noqa: E402
    MarketIndustryAbsoluteState, MarketIndustryPartialRiskOverlay,
    load_market_industry_exit_config,
    load_market_industry_partial_risk_config,
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


COSTS = (25.0, 40.0, 60.0)
ARMS = {
    "A0_frozen": (False, False),
    "E1_entry_gate": (True, False),
    "P1_partial_exit": (False, True),
    "MP1_combined": (True, True),
}
DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_market_industry_partial_risk_history_v1_20261007")
DEFAULT_PREREGISTRATION = Path(
    "/Users/wjy/abu/backtests/preregistrations/"
    "alpha158_market_industry_partial_risk_history_v1_20261007.json")


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


def register(output, inputs, experiment):
    if output.exists():
        registration = json.loads((output / "registration.json").read_text())
        changed = [str(path) for path in inputs
                   if registration["input_hashes"].get(str(path)) != digest(path)]
        if changed:
            raise ValueError("registered input changed: " + ", ".join(changed))
        return registration
    output.mkdir(parents=True)
    frozen = output / "frozen_inputs"
    frozen.mkdir()
    for path in inputs:
        shutil.copy2(path, frozen / path.name)
    registration = {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": experiment,
        "arms": list(ARMS), "costs_bps": list(COSTS),
        "input_hashes": {str(path): digest(path) for path in inputs},
        "parameter_search_performed": False,
        "threshold_search_performed": False,
        "financial_factors_in_primary_result": False,
        "automatic_admission": False,
    }
    write_json(output / "registration.json", registration)
    return registration


def verify_preregistration(path, inputs):
    payload = json.loads(Path(path).read_text())
    changed = [str(item) for item in inputs
               if payload["input_hashes"].get(str(item)) != digest(item)]
    if changed:
        raise ValueError("preregistered strategy input changed: " +
                         ", ".join(changed))
    return payload


def load_base(source, cost):
    results = pd.read_csv(source / "portfolio_results.csv")
    row = results[
        results.arm.eq("all_mean_rank") &
        results.slippage_bps.eq(float(cost))].iloc[0].to_dict()
    curve = pd.read_csv(source / f"all_mean_rank_{int(cost)}bp/daily_nav.csv")
    return row, curve


def summarize_actions(actions):
    frame = pd.DataFrame(actions)
    if frame.empty:
        return {
            "blocked_entry_dates": 0, "blocked_entries": 0,
            "reduction_signal_dates": 0, "reduction_fills": 0,
        }
    blocks = frame[frame.action.eq("BLOCK_ENTRIES")]
    fills = frame[frame.action.eq("FILL_REDUCTION")]
    proposals = frame[frame.action.eq("PROPOSE_REDUCTION")]
    return {
        "blocked_entry_dates": int(blocks.date.nunique()) if not blocks.empty else 0,
        "blocked_entries": int(blocks.blocked_count.sum()) if not blocks.empty else 0,
        "reduction_signal_dates": int(proposals.date.nunique()) if not proposals.empty else 0,
        "reduction_proposals": int(len(proposals)),
        "reduction_fills": int(len(fills)),
    }


def run_experiment(args, experiment, source_config, policy, risk, state_config,
                   execution_config, panel, predictions):
    scores = rank_frame(
        predictions[["signal_asof", "symbol", "column", "all_mean_rank"]].rename(
            columns={"all_mean_rank": "alpha_score"}),
        "alpha_score", max(policy.entry_rank_limit, policy.retention_rank_limit))
    context = MarketIndustryAbsoluteState(panel, state_config)
    results, segments, annual_frames, curves, action_rows = [], [], [], {}, []
    end_date = int(experiment["evaluation_end_date"])
    for cost in COSTS:
        base_row, base_curve = load_base(args.source_dir, cost)
        base_row.update({"arm": "A0_frozen", "slippage_bps": cost})
        results.append(base_row)
        curves[("A0_frozen", cost)] = base_curve
        for name, bounds in (
                ("primary_holdout", experiment["primary_holdout"]),
                ("secondary_observed_history",
                 experiment["secondary_observed_history"])):
            metric = segment_metrics(
                base_curve, bounds["start_date"], bounds["end_date"])
            metric.update({"arm": "A0_frozen", "segment": name,
                           "slippage_bps": cost})
            segments.append(metric)
        yearly = annual_returns(base_curve)
        yearly["arm"] = "A0_frozen"
        yearly["slippage_bps"] = cost
        annual_frames.append(yearly)
        for arm, (entry_gate, partial_exit) in list(ARMS.items())[1:]:
            overlay = MarketIndustryPartialRiskOverlay(
                panel, state_config, execution_config, context=context,
                enable_entry_block=entry_gate,
                enable_partial_reduction=partial_exit)
            result, audit = run_low_turnover(
                panel, scores, replace(source_config, label_slippage_bps=cost),
                policy, risk, end_date,
                review_overlay=CostAwareReview(suppress_rank_exits=True),
                context_risk_overlay=overlay)
            directory = args.output_dir / f"{arm}_{int(cost)}bp"
            save_audit(directory, audit)
            pd.DataFrame(overlay.actions).to_csv(
                directory / "context_risk_actions.csv", index=False)
            full = segment_metrics(
                audit["curve"], experiment["evaluation_start_date"], end_date)
            result.update(full)
            result.update({"arm": arm, "slippage_bps": cost,
                           **summarize_actions(overlay.actions)})
            results.append(result)
            curves[(arm, cost)] = audit["curve"].copy()
            for action in overlay.actions:
                action_rows.append({"arm": arm, "slippage_bps": cost, **action})
            for name, bounds in (
                    ("primary_holdout", experiment["primary_holdout"]),
                    ("secondary_observed_history",
                     experiment["secondary_observed_history"])):
                metric = segment_metrics(
                    audit["curve"], bounds["start_date"], bounds["end_date"])
                metric.update({"arm": arm, "segment": name,
                               "slippage_bps": cost})
                segments.append(metric)
            yearly = annual_returns(audit["curve"])
            yearly["arm"] = arm
            yearly["slippage_bps"] = cost
            annual_frames.append(yearly)
            print(json.dumps({
                "arm": arm, "cost": cost,
                "return_pct": full["return_pct"],
                "cagr_pct": full["cagr_pct"],
                "max_drawdown_pct": full["max_drawdown_pct"],
                **summarize_actions(overlay.actions),
            }), flush=True)
    return (pd.DataFrame(results), pd.DataFrame(segments),
            pd.concat(annual_frames, ignore_index=True), curves,
            pd.DataFrame(action_rows))


def compare(results, segments, annual, curves, experiment):
    rows = []
    for cost in COSTS:
        full = results[results.slippage_bps.eq(cost)].set_index("arm")
        primary = segments[
            segments.slippage_bps.eq(cost) &
            segments.segment.eq("primary_holdout")].set_index("arm")
        bounds = experiment["primary_holdout"]
        for offset, arm in enumerate(list(ARMS)[1:]):
            base_curve = curves[("A0_frozen", cost)]
            candidate_curve = curves[(arm, cost)]
            base_part = base_curve[base_curve.date.between(
                bounds["start_date"], bounds["end_date"])].reset_index(drop=True)
            candidate_part = candidate_curve[candidate_curve.date.between(
                bounds["start_date"], bounds["end_date"])].reset_index(drop=True)
            interval = paired_cagr_interval(
                base_part, candidate_part, experiment,
                int(experiment["bootstrap_seed"]) + int(cost) + offset * 100)
            rows.append({
                "arm": arm, "slippage_bps": cost,
                "full_return_delta_pp": float(
                    full.loc[arm, "return_pct"] -
                    full.loc["A0_frozen", "return_pct"]),
                "full_cagr_delta_pp": float(
                    full.loc[arm, "cagr_pct"] -
                    full.loc["A0_frozen", "cagr_pct"]),
                "full_max_drawdown_improvement_pp": float(
                    full.loc[arm, "max_drawdown_pct"] -
                    full.loc["A0_frozen", "max_drawdown_pct"]),
                "full_es95_improvement_pp": float(
                    full.loc[arm, "daily_expected_shortfall_95_pct"] -
                    full.loc["A0_frozen", "daily_expected_shortfall_95_pct"]),
                "full_average_exposure_delta_pp": float(
                    full.loc[arm, "average_exposure_pct"] -
                    full.loc["A0_frozen", "average_exposure_pct"]),
                "primary_return_delta_pp": float(
                    primary.loc[arm, "return_pct"] -
                    primary.loc["A0_frozen", "return_pct"]),
                "primary_cagr_delta_pp": float(
                    primary.loc[arm, "cagr_pct"] -
                    primary.loc["A0_frozen", "cagr_pct"]),
                "primary_max_drawdown_improvement_pp": float(
                    primary.loc[arm, "max_drawdown_pct"] -
                    primary.loc["A0_frozen", "max_drawdown_pct"]),
                "primary_es95_improvement_pp": float(
                    primary.loc[arm, "daily_expected_shortfall_95_pct"] -
                    primary.loc["A0_frozen", "daily_expected_shortfall_95_pct"]),
                "primary_cagr_delta_ci95_low_pp": interval[0],
                "primary_cagr_delta_ci95_high_pp": interval[1],
            })
    comparisons = pd.DataFrame(rows)
    combined = comparisons[comparisons.arm.eq("MP1_combined")]
    primary25 = combined[combined.slippage_bps.eq(25.0)].iloc[0]
    annual25 = annual[annual.slippage_bps.eq(25.0)].pivot(
        index="year", columns="arm", values="return_pct")
    positive_years = int((
        annual25.MP1_combined > annual25.A0_frozen).sum())
    actions25 = results[
        results.arm.eq("MP1_combined") &
        results.slippage_bps.eq(25.0)].iloc[0]
    gates = experiment["gates"]
    checks = {
        "primary_return_positive": bool(primary25.primary_return_delta_pp > 0),
        "primary_drawdown_not_worse": bool(
            primary25.primary_max_drawdown_improvement_pp >= 0),
        "primary_es95_not_worse": bool(
            primary25.primary_es95_improvement_pp >= 0),
        "primary_ci95_low_nonnegative": bool(
            primary25.primary_cagr_delta_ci95_low_pp >=
            gates["primary_cagr_delta_ci95_low_min"]),
        "all_cost_return_positive": bool(
            combined.full_return_delta_pp.gt(0).all()),
        "all_cost_drawdown_not_worse": bool(
            combined.full_max_drawdown_improvement_pp.ge(0).all()),
        "positive_calendar_years": bool(
            positive_years >= gates["positive_calendar_year_delta_min_count"]),
        "independent_reduction_dates": bool(
            int(actions25.get("reduction_signal_dates", 0)) >=
            gates["minimum_independent_reduction_signal_dates"]),
    }
    gate = {
        "checks": checks, "positive_calendar_years": positive_years,
        "reduction_signal_dates_25bp": int(
            actions25.get("reduction_signal_dates", 0)),
        "passed": bool(all(checks.values())), "automatic_admission": False,
    }
    return comparisons, gate


def report_markdown(results, segments, comparisons, annual, gate):
    result25 = results[results.slippage_bps.eq(25.0)].set_index("arm")
    comparison25 = comparisons[
        comparisons.slippage_bps.eq(25.0)].set_index("arm")
    lines = [
        "# Alpha158 市场—行业部分风险控制：2015–2026冻结回测", "",
        "选股固定为七因子族等权 `all_mean_rank`。市场绝对事件只暂停下一交易日新开仓；",
        "已达到 +1R 的仓位遇到市场极端、行业过热耗竭或行业退潮时，下一开盘仅减仓50%，",
        "每笔持仓最多一次。原始止损、3ATR移动止损和停滞退出优先并负责最终清仓。", "",
        "本次没有搜索事件阈值、减仓比例、确认窗口或因子权重。", "",
        "## 25bp 全区间结果", "",
        "|版本|累计收益|年化收益|最大回撤|ES95|平均仓位|减仓信号日期|暂停开仓日期|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    def safe_int(value):
        return 0 if pd.isna(value) else int(value)

    for arm in ARMS:
        row = result25.loc[arm]
        lines.append(
            f"|{arm}|{row.return_pct:+.2f}%|{row.cagr_pct:+.2f}%|"
            f"{row.max_drawdown_pct:.2f}%|{row.daily_expected_shortfall_95_pct:.3f}%|"
            f"{row.average_exposure_pct:.2f}%|"
            f"{safe_int(row.get('reduction_signal_dates', 0))}|"
            f"{safe_int(row.get('blocked_entry_dates', 0))}|"
        )
    lines += ["", "## 相对基础策略（25bp）", "",
              "|版本|全期收益增量|全期回撤改善|主验证收益增量|主验证回撤改善|主验证CAGR增量95%区间|",
              "|---|---:|---:|---:|---:|---:|"]
    for arm in list(ARMS)[1:]:
        row = comparison25.loc[arm]
        lines.append(
            f"|{arm}|{row.full_return_delta_pp:+.2f}pp|"
            f"{row.full_max_drawdown_improvement_pp:+.2f}pp|"
            f"{row.primary_return_delta_pp:+.2f}pp|"
            f"{row.primary_max_drawdown_improvement_pp:+.2f}pp|"
            f"[{row.primary_cagr_delta_ci95_low_pp:+.2f}, "
            f"{row.primary_cagr_delta_ci95_high_pp:+.2f}]pp|"
        )
    lines += ["", "## 冻结门槛", "",
              f"- 组合候选：`MP1_combined`",
              f"- 改善年度数：{gate['positive_calendar_years']} / 12",
              f"- 独立减仓信号日期：{gate['reduction_signal_dates_25bp']}",
              f"- 全部门槛：{'通过' if gate['passed'] else '未通过'}",
              "- 无论结果如何，本实验都不会自动替换模拟盘策略。", "",
              "## 数据边界", "",
              "市场基准使用沪深300；横截面广度和行业状态来自严格PIT深圳A股研究池，",
              "因此不能把本结果解释为完整沪深全市场广度验证。财务因子未进入主结果。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_market_industry_partial_risk_history_v1.json")
    parser.add_argument("--state-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_market_industry_exit_v1.json")
    parser.add_argument("--execution-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_market_industry_partial_risk_v1.json")
    parser.add_argument("--source-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_v1.json")
    parser.add_argument("--policy-config", type=Path, default=
                        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    parser.add_argument("--risk-config", type=Path, default=
                        ROOT / "configs/selection/risk_v1.json")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--preregistration", type=Path,
                        default=DEFAULT_PREREGISTRATION)
    args = parser.parse_args()
    if not (args.source_dir / "completion.json").exists():
        raise RuntimeError("frozen all_mean_rank historical source is incomplete")
    predictions_path = args.source_dir / "ensemble_oos_predictions.csv.gz"
    strategy_inputs = [
        args.experiment_config, args.state_config, args.execution_config,
        args.source_config, args.policy_config, args.risk_config,
        ROOT / "abupy/AlphaBu/ABuMarketIndustryExit.py",
        ROOT / "scripts/backtest_alpha158_lite_low_turnover_v3.py",
        Path(__file__),
    ]
    verify_preregistration(args.preregistration, strategy_inputs)
    inputs = [*strategy_inputs, args.preregistration, predictions_path]
    experiment = json.loads(args.experiment_config.read_text())
    register(args.output_dir, inputs, experiment)
    source = load_alpha158_lite_config(args.source_config)
    policy = load_alpha158_lite_low_turnover_config(args.policy_config)
    risk = load_risk_config(args.risk_config)
    state_config = load_market_industry_exit_config(args.state_config)
    execution_config = load_market_industry_partial_risk_config(
        args.execution_config)
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("source/policy configuration mismatch")
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=int(experiment["panel_start_date"]),
        end_date=int(experiment["evaluation_end_date"]))
    predictions = pd.read_csv(predictions_path, dtype={"symbol": str})
    predictions = predictions[predictions.signal_asof.between(
        experiment["evaluation_start_date"],
        experiment["evaluation_end_date"])].copy()
    if predictions.empty or not (predictions.train_end < predictions.signal_asof).all():
        raise ValueError("non-empty strictly OOS predictions are required")
    results, segments, annual, curves, actions = run_experiment(
        args, experiment, source, policy, risk, state_config,
        execution_config, panel, predictions)
    comparisons, gate = compare(
        results, segments, annual, curves, experiment)
    results.to_csv(args.output_dir / "portfolio_results.csv", index=False)
    segments.to_csv(args.output_dir / "segment_results.csv", index=False)
    annual.to_csv(args.output_dir / "annual_returns.csv", index=False)
    comparisons.to_csv(args.output_dir / "comparisons.csv", index=False)
    actions.to_csv(args.output_dir / "context_risk_actions.csv", index=False)
    write_json(args.output_dir / "gates.json", gate)
    (args.output_dir / "REPORT.md").write_text(
        report_markdown(results, segments, comparisons, annual, gate),
        encoding="utf-8")
    report = {
        "status": "COMPLETE", "experiment": experiment,
        "portfolio_results": results.to_dict("records"),
        "segment_results": segments.to_dict("records"),
        "comparisons": comparisons.to_dict("records"),
        "gate": gate,
        "decision": ("RETAIN_FOR_FORWARD_SHADOW_REVIEW" if gate["passed"]
                     else "REJECT_ON_PREREGISTERED_HISTORY_GATE"),
        "automatic_admission": False,
        "financial_factors_in_primary_result": False,
        "market_breadth_scope": "strict_pit_shenzhen_strategy_universe",
        "benchmark_scope": "csi_300",
    }
    write_json(args.output_dir / "report.json", report)
    write_json(args.output_dir / "completion.json", {
        "status": "COMPLETE", "decision": report["decision"],
        "report_sha256": digest(args.output_dir / "report.json")})
    print(json.dumps({"decision": report["decision"], "gate": gate},
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
