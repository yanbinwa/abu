#!/usr/bin/env python3
"""Explore causal 200-session benchmark trend sizing for the wider sleeve."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
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
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.analyze_alpha158_long_cycle_robustness_v1 import (  # noqa: E402
    ERAS, segment_account,
)
from scripts.backtest_alpha158_event_exit_only_2025_2026 import save_audit  # noqa: E402
from scripts.backtest_alpha158_lite_low_turnover_v3 import run_low_turnover  # noqa: E402
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402


DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/alpha158_history_2015_validation_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_market_trend_risk_sensitivity_v1_20261007")


class MarketTrendRiskSizing(object):
    def __init__(self, panel, config):
        self.panel = panel
        self.window = int(config["trend_window_sessions"])
        self.risk_on = float(config["risk_on_fraction"])
        self.risk_off = float(config["risk_off_fraction"])
        self.config = SimpleNamespace(policy_id=config["experiment_id"])
        self.evaluations = []
        close = pd.Series(np.asarray(panel.benchmark_close, dtype=float))
        self.average = close.rolling(
            self.window, min_periods=self.window).mean().to_numpy(dtype=float)

    def evaluate(self, day, symbol):
        close = float(self.panel.benchmark_close[int(day)])
        average = float(self.average[int(day)])
        defensive = not np.isfinite(average) or close < average
        record = {
            "policy_id": self.config.policy_id,
            "signal_asof": int(self.panel.dates[int(day)]),
            "symbol": str(symbol), "selected": bool(defensive),
            "benchmark_close": close, "benchmark_ma200": average,
            "risk_fraction": self.risk_off if defensive else self.risk_on,
            "reason": "BELOW_MA200" if defensive else "AT_OR_ABOVE_MA200",
        }
        self.evaluations.append(record)
        return record


def write_json(path, value):
    Path(path).write_text(json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
        allow_nan=False, default=str) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=
                        ROOT / "configs/selection/alpha158_market_trend_risk_sensitivity_v1.json")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(args.output)
    args.output.mkdir(parents=True)
    config = json.loads(args.config.read_text())
    source = load_alpha158_lite_config(
        ROOT / "configs/selection/alpha158_lite_v1.json")
    policy = load_alpha158_lite_low_turnover_config(
        ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json")
    risk = load_risk_config(ROOT / "configs/selection/risk_v1.json")
    positions = int(config["target_positions"])
    candidate_source = replace(
        source, retention_top_k=max(source.retention_top_k, positions),
        portfolio_score_depth=max(source.portfolio_score_depth, positions))
    candidate_policy = replace(
        policy, target_positions=positions,
        strategy_version="alpha158_k30_market_trend_risk_v1")
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
    result_rows, segment_rows = [], []
    for cost in config["costs_bps"]:
        sizing = MarketTrendRiskSizing(panel, config)
        result, audit = run_low_turnover(
            panel, scores, replace(candidate_source, label_slippage_bps=cost),
            candidate_policy, risk, config["evaluation_end_date"],
            review_overlay=CostAwareReview(suppress_rank_exits=True),
            entry_sizing_policy=sizing)
        directory = args.output / "market_trend_{}bp".format(int(cost))
        save_audit(directory, audit)
        full = segment_account(
            audit["curve"], config["evaluation_start_date"],
            config["evaluation_end_date"])
        result.update(full)
        result.update({
            "slippage_bps": float(cost),
            "defensive_entry_evaluations": int(sum(
                row["selected"] for row in sizing.evaluations)),
            "total_entry_evaluations": int(len(sizing.evaluations)),
        })
        result_rows.append(result)
        for era, bounds in ERAS.items():
            segment = segment_account(audit["curve"], *bounds)
            segment.update({"era": era, "slippage_bps": float(cost)})
            segment_rows.append(segment)
        print(json.dumps({
            "cost": cost, "return_pct": full["return_pct"],
            "cagr_pct": full["cagr_pct"],
            "max_drawdown_pct": full["max_drawdown_pct"],
            "average_exposure_pct": full["average_exposure_pct"],
            "defensive_evaluations": result["defensive_entry_evaluations"],
        }), flush=True)
    results = pd.DataFrame(result_rows)
    segments = pd.DataFrame(segment_rows)
    results.to_csv(args.output / "portfolio_results.csv", index=False)
    segments.to_csv(args.output / "segment_results.csv", index=False)
    k10 = pd.read_csv(args.source / "portfolio_results.csv")
    k10 = k10[k10.arm.eq("all_mean_rank") &
              k10.slippage_bps.eq(25.0)].iloc[0]
    k30 = pd.read_csv(
        "/Users/wjy/abu/backtests/alpha158_breadth_sensitivity_v1_r2_20261007/portfolio_results.csv")
    k30 = k30[k30.arm.eq("K30")].iloc[0]
    primary = results[results.slippage_bps.eq(25.0)].iloc[0]
    lines = ["# Alpha158 K30＋沪深300 200日趋势新增风险控制", "",
             "本实验已观察全部历史，只能用于结构诊断和前瞻候选设计。", "",
             "规则固定为：沪深300收盘低于当日可见200日均线时，新开仓单笔风险由0.25%降至0.125%；已有持仓不强制退出。", "",
             "|版本|累计收益|CAGR|最大回撤|平均仓位|",
             "|---|---:|---:|---:|---:|",
             f"|K10基础|{k10.return_pct:+.2f}%|{k10.cagr_pct:+.2f}%|{k10.max_drawdown_pct:.2f}%|{k10.average_exposure_pct:.2f}%|",
             f"|K30宽度|{k30.return_pct:+.2f}%|{k30.cagr_pct:+.2f}%|{k30.max_drawdown_pct:.2f}%|{k30.average_exposure_pct:.2f}%|",
             f"|K30＋趋势风险|{primary.return_pct:+.2f}%|{primary.cagr_pct:+.2f}%|{primary.max_drawdown_pct:.2f}%|{primary.average_exposure_pct:.2f}%|",
             "", "成本敏感性和分时期结果见CSV。无论结果如何均不自动上线。", ""]
    (args.output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(args.output / "completion.json", {
        "status": "COMPLETE", "research_status": config["research_status"],
        "parameter_search_performed": False,
        "automatic_admission": False, "new_strategy_selected": False,
    })


if __name__ == "__main__":
    main()
