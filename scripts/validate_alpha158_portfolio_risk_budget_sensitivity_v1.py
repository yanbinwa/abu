#!/usr/bin/env python3
"""Run the frozen K30 portfolio-risk budget sensitivity experiment."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
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
from scripts.validate_alpha158_history_2015_v1 import segment_metrics  # noqa: E402


DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_portfolio_risk_budget_sensitivity_v1_20261007")


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")


def register(output, inputs, config):
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    frozen = output / "frozen_inputs"
    frozen.mkdir()
    for path in inputs:
        shutil.copy2(path, frozen / path.name)
    registration = {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": config,
        "input_hashes": {str(path): digest(path) for path in inputs},
        "results_observed_at_registration": False,
        "parameter_search_performed": False,
        "automatic_admission": False,
    }
    write_json(output / "registration.json", registration)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/alpha158_portfolio_risk_budget_sensitivity_v1.json")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    predictions_path = args.source / "ensemble_oos_predictions.csv.gz"
    inputs = [
        args.config, ROOT / "configs/selection/alpha158_lite_v1.json",
        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json",
        ROOT / "configs/selection/risk_v1.json", Path(__file__),
        ROOT / "scripts/backtest_alpha158_lite_low_turnover_v3.py",
        predictions_path,
    ]
    register(args.output, inputs, config)
    source = load_alpha158_lite_config(inputs[1])
    policy = load_alpha158_lite_low_turnover_config(inputs[2])
    base_risk = load_risk_config(inputs[3])
    if base_risk.single_trade_risk_fraction != \
            config["single_trade_risk_fraction"]:
        raise ValueError("single-trade risk changed")
    positions = int(config["target_positions"])
    candidate_source = replace(
        source, retention_top_k=max(source.retention_top_k, positions),
        portfolio_score_depth=max(source.portfolio_score_depth, positions))
    candidate_policy = replace(
        policy, target_positions=positions,
        strategy_version="alpha158_k30_risk_budget_sensitivity_v1")
    predictions = pd.read_csv(
        predictions_path,
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
    for portfolio_risk in config["portfolio_open_risk_grid"]:
        industry_risk = (float(portfolio_risk) *
                         config["industry_to_portfolio_risk_ratio"])
        same_day_risk = (float(portfolio_risk) *
                         config["same_day_to_portfolio_risk_ratio"])
        risk = replace(
            base_risk,
            risk_version="risk_budget_{:.1f}pct_v1".format(
                float(portfolio_risk) * 100),
            portfolio_open_risk_fraction=float(portfolio_risk),
            industry_open_risk_fraction=float(industry_risk),
            same_day_new_risk_fraction=float(same_day_risk))
        for cost in config["costs_bps"]:
            arm = "R{:.1f}_{}bp".format(
                float(portfolio_risk) * 100, int(cost))
            result, audit = run_low_turnover(
                panel, scores, replace(
                    candidate_source, label_slippage_bps=float(cost)),
                candidate_policy, risk, config["evaluation_end_date"],
                review_overlay=CostAwareReview(suppress_rank_exits=True))
            save_audit(args.output / arm, audit)
            full = segment_account(
                audit["curve"], config["evaluation_start_date"],
                config["evaluation_end_date"])
            result.update(full)
            result.update({
                "arm": arm, "portfolio_open_risk_fraction": portfolio_risk,
                "industry_open_risk_fraction": industry_risk,
                "same_day_new_risk_fraction": same_day_risk,
                "slippage_bps": float(cost),
            })
            results.append(result)
            for era, bounds in ERAS.items():
                account = segment_account(audit["curve"], *bounds)
                statistical = segment_metrics(audit["curve"], *bounds)
                account["daily_expected_shortfall_95_pct"] = statistical[
                    "daily_expected_shortfall_95_pct"]
                account.update({
                    "arm": arm, "era": era,
                    "portfolio_open_risk_fraction": portfolio_risk,
                    "slippage_bps": float(cost),
                })
                segments.append(account)
            annual = annual_returns(audit["curve"])
            annual["arm"] = arm
            annual["portfolio_open_risk_fraction"] = portfolio_risk
            annual["slippage_bps"] = float(cost)
            annual_frames.append(annual)
            print(json.dumps({
                "arm": arm, "return_pct": full["return_pct"],
                "cagr_pct": full["cagr_pct"],
                "max_drawdown_pct": full["max_drawdown_pct"],
                "es95": result["daily_expected_shortfall_95_pct"],
                "average_exposure_pct": full["average_exposure_pct"],
                "risk_rejected": result["risk_rejected"],
            }), flush=True)
    results = pd.DataFrame(results)
    segments = pd.DataFrame(segments)
    annual = pd.concat(annual_frames, ignore_index=True)
    results.to_csv(args.output / "portfolio_results.csv", index=False)
    segments.to_csv(args.output / "segment_results.csv", index=False)
    annual.to_csv(args.output / "annual_returns.csv", index=False)
    primary = results[results.slippage_bps.eq(25.0)].sort_values(
        "portfolio_open_risk_fraction")
    lines = ["# Alpha158 K30组合风险预算敏感性", "",
             "固定选股、K30、单笔0.25%风险和原始个股退出，仅提高组合/行业/同日风险预算。",
             "本实验使用已观察历史，不允许自动选择或上线结果。", "",
             "|组合风险上限|累计收益|CAGR|最大回撤|ES95|平均仓位|风险拒绝|",
             "|---:|---:|---:|---:|---:|---:|---:|"]
    for row in primary.itertuples(index=False):
        lines.append(
            f"|{row.portfolio_open_risk_fraction*100:.1f}%|"
            f"{row.return_pct:+.2f}%|{row.cagr_pct:+.2f}%|"
            f"{row.max_drawdown_pct:.2f}%|"
            f"{row.daily_expected_shortfall_95_pct:.3f}%|"
            f"{row.average_exposure_pct:.2f}%|{row.risk_rejected}|"
        )
    lines += ["", "分时期、年度和成本敏感性见CSV。历史最优档位不能据此进入模拟盘。", ""]
    (args.output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(args.output / "completion.json", {
        "status": "COMPLETE", "research_status": config["research_status"],
        "parameter_search_performed": False,
        "automatic_admission": False, "new_strategy_selected": False,
    })


if __name__ == "__main__":
    main()
