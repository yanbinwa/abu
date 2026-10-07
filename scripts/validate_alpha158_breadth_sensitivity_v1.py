#!/usr/bin/env python3
"""Explore whether a wider candidate sleeve monetizes the frozen ranking."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.analyze_alpha158_long_cycle_robustness_v1 import (  # noqa: E402
    ERAS, segment_account,
)
from scripts.backtest_alpha158_event_exit_only_2025_2026 import save_audit  # noqa: E402
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402


DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_breadth_sensitivity_v1_r2_20261007")


def write_json(path, value):
    Path(path).write_text(json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/alpha158_breadth_sensitivity_v1.json")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True)
    source = load_alpha158_lite_config(
        ROOT / "configs/selection/alpha158_lite_v1.json")
    policy = load_alpha158_lite_low_turnover_config(
        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    if (risk.portfolio_open_risk_fraction !=
            config["portfolio_open_risk_fraction"] or
            risk.single_trade_risk_fraction !=
            config["single_trade_risk_fraction"]):
        raise ValueError("frozen risk configuration mismatch")
    predictions = pd.read_csv(
        args.source / "ensemble_oos_predictions.csv.gz",
        usecols=["signal_asof", "symbol", "column", "all_mean_rank"],
        dtype={"symbol": str})
    predictions = predictions[predictions.signal_asof.between(
        config["evaluation_start_date"], config["evaluation_end_date"])]
    scores = rank_frame(
        predictions.rename(columns={"all_mean_rank": "alpha_score"}),
        "alpha_score", max(policy.entry_rank_limit,
                           policy.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20110101,
        end_date=config["evaluation_end_date"])
    results, segments, annual_frames = [], [], []
    for positions in config["target_position_grid"]:
        arm = "K{}".format(positions)
        if positions == 10:
            curve = pd.read_csv(
                args.source / "all_mean_rank_25bp/daily_nav.csv")
            source_results = pd.read_csv(args.source / "portfolio_results.csv")
            result = source_results[
                source_results.arm.eq("all_mean_rank") &
                source_results.slippage_bps.eq(25.0)].iloc[0].to_dict()
        else:
            candidate_policy = replace(
                policy, target_positions=int(positions),
                strategy_version="alpha158_breadth_{}_v1".format(positions))
            candidate_source = replace(
                source,
                retention_top_k=max(source.retention_top_k, int(positions)),
                portfolio_score_depth=max(
                    source.portfolio_score_depth, int(positions)))
            result, audit = run_low_turnover(
                panel, scores, candidate_source, candidate_policy, risk,
                config["evaluation_end_date"],
                review_overlay=CostAwareReview(suppress_rank_exits=True))
            save_audit(args.output / arm, audit)
            curve = audit["curve"]
        full = segment_account(
            curve, config["evaluation_start_date"],
            config["evaluation_end_date"])
        result.update(full)
        result.update({"arm": arm, "target_positions": int(positions)})
        results.append(result)
        for era, bounds in ERAS.items():
            metric = segment_account(curve, *bounds)
            metric.update({"arm": arm, "era": era,
                           "target_positions": int(positions)})
            segments.append(metric)
        annual = annual_returns(curve)
        annual["arm"] = arm
        annual_frames.append(annual)
        print(json.dumps({
            "arm": arm, "return_pct": full["return_pct"],
            "cagr_pct": full["cagr_pct"],
            "max_drawdown_pct": full["max_drawdown_pct"],
            "average_exposure_pct": full["average_exposure_pct"],
            "filled_buys": result["filled_buys"],
        }), flush=True)
    results = pd.DataFrame(results)
    segments = pd.DataFrame(segments)
    annual = pd.concat(annual_frames, ignore_index=True)
    results.to_csv(args.output / "portfolio_results.csv", index=False)
    segments.to_csv(args.output / "segment_results.csv", index=False)
    annual.to_csv(args.output / "annual_returns.csv", index=False)
    base = results.set_index("arm").loc["K10"]
    lines = ["# Alpha158 持仓宽度结构敏感性", "",
             "本实验使用已观察历史，仅用于判断组合结构，不允许据此挑选并上线最佳K。", "",
             "所有版本固定 `all_mean_rank`、2%组合开放风险上限、0.25%单笔风险、25bp成本和原始个股退出。", "",
             "|目标持仓|累计收益|CAGR|最大回撤|平均仓位|单位暴露收益年率*|收益增量|",
             "|---:|---:|---:|---:|---:|---:|---:|"]
    for row in results.sort_values("target_positions").itertuples(index=False):
        lines.append(
            f"|{row.target_positions}|{row.return_pct:+.2f}%|{row.cagr_pct:+.2f}%|"
            f"{row.max_drawdown_pct:.2f}%|{row.average_exposure_pct:.2f}%|"
            f"{row.exposure_normalized_arithmetic_return_pct_pa:+.2f}%|"
            f"{row.return_pct-base.return_pct:+.2f}pp|")
    lines += ["", "\* 诊断指标，不是可直接实现的复利年化。", "",
              "下一候选只能依据结构稳定性预注册并进入前瞻模拟，不能把本表最优值当作历史验证通过。", ""]
    (args.output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(args.output / "completion.json", {
        "status": "COMPLETE", "research_status": config["research_status"],
        "new_strategy_selected": False, "automatic_admission": False,
    })


if __name__ == "__main__":
    main()
