#!/usr/bin/env python3
"""Replay frozen market/industry absolute-state exits on all_mean_rank."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
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
    MarketIndustryAbsoluteState, MarketIndustryExitEngine,
    load_market_industry_exit_config,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.backtest_alpha158_event_exit_only_2025_2026 import (  # noqa: E402
    save_audit,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.validate_alpha158_cost_gate_v1 import (  # noqa: E402
    paired_block_interval,
)


DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_all_factor_utility_interim_replay_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_market_industry_exit_v1_20261007")
VARIANTS = {
    "A0_frozen": "当前 all_mean_rank＋3ATR 退出",
    "M1_market": "市场绝对状态退出＋原退出",
    "I1_industry": "行业绝对状态退出＋原退出",
    "MI1_combined": "市场优先、行业其次＋原退出",
}
COSTS = (25, 40, 60)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, payload):
    Path(path).write_text(json.dumps(
        payload, ensure_ascii=False, indent=2, sort_keys=True,
        default=str, allow_nan=False) + "\n", encoding="utf-8")


def curve_cagr(curve):
    start = pd.Timestamp(str(int(curve.date.iloc[0])))
    end = pd.Timestamp(str(int(curve.date.iloc[-1])))
    years = max((end - start).days / 365.25, 1 / 365.25)
    return float(((curve.capital.iloc[-1] / curve.capital.iloc[0]) **
                  (1 / years) - 1) * 100)


def load_baseline(source):
    result = pd.read_csv(source / "portfolio_results.csv")
    result = result[result.arm.eq("all_mean_rank")].copy()
    rows, curves, annual = [], {}, []
    for cost in COSTS:
        row = result[result.slippage_bps.eq(float(cost))].iloc[0].to_dict()
        row.update({"variant": "A0_frozen", "label": VARIANTS["A0_frozen"]})
        rows.append(row)
        curve = pd.read_csv(source / f"all_mean_rank_{cost}bp/daily_nav.csv")
        curves[cost] = curve
        year = annual_returns(curve)
        year["variant"] = "A0_frozen"
        year["slippage_bps"] = float(cost)
        annual.append(year)
    return rows, curves, annual


def factory(overlay, context, holder):
    def build(panel, strategy_config):
        engine = MarketIndustryExitEngine(
            panel, strategy_config, overlay, context=context)
        holder.append(engine)
        return engine
    return build


def comparison_rows(results, curves, annual):
    frame = pd.DataFrame(results)
    annual_frame = pd.concat(annual, ignore_index=True)
    rows = []
    for cost in COSTS:
        indexed = frame[frame.slippage_bps.eq(float(cost))].set_index("variant")
        base = indexed.loc["A0_frozen"]
        base_curve = curves[("A0_frozen", cost)]
        base_nav = base_curve.capital.to_numpy(dtype=float)
        for offset, variant in enumerate(VARIANTS):
            if variant == "A0_frozen":
                continue
            candidate = indexed.loc[variant]
            candidate_curve = curves[(variant, cost)]
            interval = paired_block_interval(
                base_nav, candidate_curve.capital.to_numpy(dtype=float),
                seed=20261007 + offset * 100 + cost,
                replicates=5000, block=20)
            rows.append({
                "variant": variant,
                "slippage_bps": float(cost),
                "return_delta_pp": float(candidate.return_pct - base.return_pct),
                "cagr_delta_pp": float(candidate.cagr_pct - base.cagr_pct),
                "max_drawdown_improvement_pp": float(
                    candidate.max_drawdown_pct - base.max_drawdown_pct),
                "expected_shortfall_improvement_pp": float(
                    candidate.daily_expected_shortfall_95_pct -
                    base.daily_expected_shortfall_95_pct),
                "liquidation_return_delta_pp": float(
                    candidate.liquidation_3_limits_return_pct -
                    base.liquidation_3_limits_return_pct),
                "average_exposure_delta_pp": float(
                    candidate.average_exposure_pct - base.average_exposure_pct),
                **interval,
            })
    return pd.DataFrame(rows), annual_frame


def gates(comparison, annual, triggers):
    output = {}
    base_annual = annual[annual.variant.eq("A0_frozen")]
    for variant in VARIANTS:
        if variant == "A0_frozen":
            continue
        rows = comparison[comparison.variant.eq(variant)]
        primary = rows[rows.slippage_bps.eq(25.0)].iloc[0]
        candidate_annual = annual[
            annual.variant.eq(variant) & annual.slippage_bps.eq(25.0)]
        base = base_annual[base_annual.slippage_bps.eq(25.0)]
        paired = base[["year", "return_pct"]].merge(
            candidate_annual[["year", "return_pct"]], on="year",
            suffixes=("_base", "_candidate"))
        positive_years = int((
            paired.return_pct_candidate > paired.return_pct_base).sum())
        variant_triggers = triggers[
            triggers.variant.eq(variant) & triggers.slippage_bps.eq(25.0)]
        checks = {
            "return_all_costs": bool(rows.return_delta_pp.gt(0).all()),
            "drawdown_all_costs": bool(
                rows.max_drawdown_improvement_pp.ge(0).all()),
            "es95_all_costs": bool(
                rows.expected_shortfall_improvement_pp.ge(0).all()),
            "liquidation_all_costs": bool(
                rows.liquidation_return_delta_pp.ge(0).all()),
            "paired_ci95_low_positive_25bp": bool(
                primary.ci95_low_pp >= 0),
            "positive_annual_delta_at_least_3_of_4": positive_years >= 3,
            "independent_trigger_dates_at_least_10": int(
                variant_triggers.date.nunique()) >= 10,
        }
        output[variant] = {
            "checks": checks,
            "positive_years": positive_years,
            "trigger_dates": int(variant_triggers.date.nunique()),
            "passed": bool(all(checks.values())),
            "automatic_admission": False,
        }
    return output


def report_markdown(results, comparison, annual, triggers, gate, output):
    results = results.sort_values(["slippage_bps", "variant"])
    primary = results[results.slippage_bps.eq(25.0)].set_index("variant")
    comp25 = comparison[comparison.slippage_bps.eq(25.0)].set_index("variant")
    lines = [
        "# Alpha158 市场—行业绝对状态退出实验 v1", "",
        "本实验冻结 `all_mean_rank` 选股、七族等权、10只目标持仓、事件退出和风险参数。",
        "所有状态阈值由当日前252个交易日的滚动分位数确定，当前日不参与阈值计算；",
        "信号在收盘确认，下一交易日开盘执行。历史已被观察，结果只能用于筛选或新增 shadow。",
        "", "## 25bp 主结果", "",
        "|版本|累计收益|年化收益|最大回撤|ES95|平均仓位|压力收益|额外情绪退出|",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, label in VARIANTS.items():
        row = primary.loc[variant]
        count = 0 if variant == "A0_frozen" else int(triggers[
            triggers.variant.eq(variant) &
            triggers.slippage_bps.eq(25.0)].shape[0])
        lines.append(
            f"|{variant}：{label}|{row.return_pct:+.2f}%|{row.cagr_pct:+.2f}%|"
            f"{row.max_drawdown_pct:.2f}%|{row.daily_expected_shortfall_95_pct:.3f}%|"
            f"{row.average_exposure_pct:.2f}%|{row.liquidation_3_limits_return_pct:+.2f}%|{count}|"
        )
    lines += ["", "## 相对冻结版本（25bp）", "",
              "|版本|收益增量|回撤改善|ES95改善|仓位变化|区块95%区间|触发日期|结论|",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for variant in list(VARIANTS)[1:]:
        row = comp25.loc[variant]
        decision = "通过全部门槛" if gate[variant]["passed"] else "未通过"
        lines.append(
            f"|{variant}|{row.return_delta_pp:+.2f}pp|"
            f"{row.max_drawdown_improvement_pp:+.2f}pp|"
            f"{row.expected_shortfall_improvement_pp:+.3f}pp|"
            f"{row.average_exposure_delta_pp:+.2f}pp|"
            f"[{row.ci95_low_pp:+.2f}, {row.ci95_high_pp:+.2f}]pp|"
            f"{gate[variant]['trigger_dates']}|{decision}|"
        )
    lines += ["", "## 年度稳定性（25bp）", "",
              "|年度|A0|M1|I1|MI1|", "|---:|---:|---:|---:|---:|"]
    year25 = annual[annual.slippage_bps.eq(25.0)].pivot(
        index="year", columns="variant", values="return_pct")
    for year, row in year25.iterrows():
        lines.append(
            f"|{int(year)}|{row.get('A0_frozen', np.nan):+.2f}%|"
            f"{row.get('M1_market', np.nan):+.2f}%|"
            f"{row.get('I1_industry', np.nan):+.2f}%|"
            f"{row.get('MI1_combined', np.nan):+.2f}%|"
        )
    lines += ["", "## 约束与解释", "",
              "- 市场状态使用沪深300五日收益与日内收益，以及全市场成交额和涨跌宽度。",
              "- 行业状态使用申万历史行业归属下的绝对五日收益、日内收益、成交额和上涨比例。",
              "- 行业信号不是行业横截面排名；全行业一起下跌时不会因相对排名上升而被误判为安全。",
              "- 只有已经达到1R并启用原移动止损的盈利持仓，才允许被情绪覆盖层提前退出。",
              "- 本轮不含50%减仓。全退出版本只有通过稳定性门槛后，才值得增加部分退出自由度。",
              "- 任一版本都不会自动写入模拟盘或替换当前冻结策略。", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(args.output_dir)
    args.output_dir.mkdir(parents=True)

    config_path = ROOT / "configs/selection/alpha158_market_industry_exit_v1.json"
    source_path = ROOT / "configs/selection/alpha158_lite_v1.json"
    policy_path = ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json"
    risk_path = ROOT / "configs/selection/risk_v1.json"
    prediction_path = args.source_dir / "ensemble_oos_predictions.csv.gz"
    input_files = [config_path, source_path, policy_path, risk_path,
                   prediction_path, Path(__file__),
                   ROOT / "abupy/AlphaBu/ABuMarketIndustryExit.py"]
    input_hashes = {str(path): digest(path) for path in input_files}
    write_json(args.output_dir / "registration.json", {
        "registered_at": datetime.now().astimezone().isoformat(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "variants": VARIANTS,
        "slippage_bps": COSTS,
        "input_hashes": input_hashes,
        "parameter_search": False,
        "research_only": True,
        "automatic_admission": False,
        "observed_history": True,
        "new_holdout": False,
    })
    frozen = args.output_dir / "frozen_configs"
    frozen.mkdir()
    for path in (config_path, source_path, policy_path, risk_path):
        shutil.copy2(path, frozen / path.name)

    source = load_alpha158_lite_config(source_path)
    policy = load_alpha158_lite_low_turnover_config(policy_path)
    risk = load_risk_config(risk_path)
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("source and policy configs differ")
    predictions = pd.read_csv(
        prediction_path,
        usecols=["signal_asof", "symbol", "column", "all_mean_rank"],
        dtype={"symbol": str})
    predictions = predictions.rename(
        columns={"all_mean_rank": policy.score_column})
    scores = rank_frame(
        predictions, policy.score_column,
        max(policy.entry_rank_limit, policy.retention_rank_limit))
    end_date = int(predictions.signal_asof.max())
    print("Loading PIT panel...", flush=True)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=20200101, end_date=end_date)
    common_config = load_market_industry_exit_config(config_path, "combined")
    print("Building causal market/industry states...", flush=True)
    context = MarketIndustryAbsoluteState(panel, common_config)
    context.market_features.to_csv(
        args.output_dir / "market_state_daily.csv", index=False)

    results, baseline_curves, annual = load_baseline(args.source_dir)
    curves = {("A0_frozen", cost): curve
              for cost, curve in baseline_curves.items()}
    triggers = []
    for cost in COSTS:
        for variant, mode in (("M1_market", "market"),
                              ("I1_industry", "industry"),
                              ("MI1_combined", "combined")):
            print(f"RUN {variant} {cost}bp", flush=True)
            overlay = load_market_industry_exit_config(config_path, mode)
            holder = []
            result, audit = run_low_turnover(
                panel, scores,
                replace(source, label_slippage_bps=float(cost)),
                policy, risk, end_date,
                review_overlay=CostAwareReview(suppress_rank_exits=True),
                exit_engine_factory=factory(overlay, context, holder))
            if len(holder) != 1:
                raise AssertionError("exit engine factory count mismatch")
            trigger = pd.DataFrame(holder[0].trigger_log)
            if not trigger.empty:
                trigger["variant"] = variant
                trigger["slippage_bps"] = float(cost)
                triggers.append(trigger)
            result.update({
                "variant": variant, "label": VARIANTS[variant],
                "cagr_pct": curve_cagr(audit["curve"]),
            })
            results.append(result)
            curves[(variant, cost)] = audit["curve"].copy()
            year = annual_returns(audit["curve"])
            year["variant"] = variant
            year["slippage_bps"] = float(cost)
            annual.append(year)
            save_audit(args.output_dir / f"{variant}_{cost}bp", audit)
            print(json.dumps({
                "variant": variant, "cost": cost,
                "return_pct": result["return_pct"],
                "max_drawdown_pct": result["max_drawdown_pct"],
                "es95": result["daily_expected_shortfall_95_pct"],
                "trigger_count": len(trigger),
            }, ensure_ascii=False), flush=True)

    result_frame = pd.DataFrame(results)
    # The stored A0 rows already include CAGR from the source replay.
    result_frame.to_csv(args.output_dir / "results.csv", index=False)
    trigger_frame = (pd.concat(triggers, ignore_index=True)
                     if triggers else pd.DataFrame(columns=[
                         "date", "symbol", "reason", "variant",
                         "slippage_bps"]))
    trigger_frame.to_csv(args.output_dir / "sentiment_exit_triggers.csv", index=False)
    comparison, annual_frame = comparison_rows(
        results, curves, annual)
    comparison.to_csv(args.output_dir / "comparisons.csv", index=False)
    annual_frame.to_csv(args.output_dir / "annual_returns.csv", index=False)
    gate = gates(comparison, annual_frame, trigger_frame)
    write_json(args.output_dir / "gates.json", gate)
    report_markdown(
        result_frame, comparison, annual_frame, trigger_frame,
        gate, args.output_dir)
    changed = [path for path, value in input_hashes.items()
               if digest(path) != value]
    summary = {
        "status": "COMPLETE" if not changed else "INVALID_INPUT_CHANGED",
        "inputs_unchanged": not changed,
        "changed_inputs": changed,
        "gates": gate,
        "decision": "RETAIN_FOR_SHADOW_REVIEW" if any(
            item["passed"] for item in gate.values()) else
            "REJECT_ALL_CONTEXT_EXIT_VARIANTS",
        "automatic_admission": False,
        "research_only": True,
    }
    write_json(args.output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if changed:
        raise RuntimeError("inputs changed during replay")


if __name__ == "__main__":
    main()
