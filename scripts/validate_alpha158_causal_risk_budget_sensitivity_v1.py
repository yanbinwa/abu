#!/usr/bin/env python3
"""Test frozen causal-family ranks under wider portfolio risk budgets."""
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
    segment_account,
)
from scripts.backtest_alpha158_event_exit_only_2025_2026 import (  # noqa: E402
    save_audit,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.validate_alpha158_history_2015_v1 import segment_metrics  # noqa: E402


DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/alpha158_causal_family_weight_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_causal_risk_budget_sensitivity_v1_20261007")


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


def register(output, inputs, snapshots, config):
    if output.exists():
        raise FileExistsError(output)
    output.mkdir(parents=True)
    frozen = output / "frozen_inputs"
    frozen.mkdir()
    for path in snapshots:
        shutil.copy2(path, frozen / path.name)
    write_json(output / "registration.json", {
        "registered_at": datetime.now().astimezone().isoformat(),
        "experiment": config,
        "input_hashes": {str(path): digest(path) for path in inputs},
        "results_observed_at_registration": False,
        "parameter_search_performed": False,
        "automatic_admission": False,
    })


def marginal_trade_metrics(output, arms):
    rows = []
    for cost in sorted({cost for _, cost, _ in arms}):
        base_name = "R2.0_{}bp".format(int(cost))
        base_trades = pd.read_csv(output / base_name / "logical_trades.csv")
        base_keys = set(zip(
            base_trades.opened_at.fillna(-1).astype(int), base_trades.symbol))
        for risk, arm in [(risk, name) for risk, c, name in arms if c == cost]:
            trades = pd.read_csv(output / arm / "logical_trades.csv")
            trades["entry_date"] = trades.opened_at.fillna(-1).astype(int)
            trades["entry_key"] = list(zip(trades.entry_date, trades.symbol))
            trades["is_incremental_vs_2pct"] = ~trades.entry_key.isin(base_keys)
            dispositions = pd.read_csv(output / arm / "lot_dispositions.csv")
            pnl = dispositions.groupby("trade_id", as_index=False).agg(
                realized_pnl_cash=("realized_pnl_cash", "sum"))
            evaluated = trades.merge(pnl, on="trade_id", how="left")
            incremental = evaluated[evaluated.is_incremental_vs_2pct].copy()
            closed = incremental[incremental.status.eq("CLOSED") &
                                 incremental.realized_pnl_cash.notna()].copy()
            risk_cash = closed.initial_r_cash_frozen.sum()
            rows.append({
                "arm": arm,
                "slippage_bps": cost,
                "portfolio_open_risk_fraction": risk,
                "entry_trades": len(trades),
                "entry_overlap_with_2pct": int(trades.entry_key.isin(base_keys).sum()),
                "incremental_entry_trades": len(incremental),
                "incremental_closed_trades": len(closed),
                "incremental_win_rate_pct": (
                    float((closed.realized_pnl_cash > 0).mean() * 100)
                    if len(closed) else None),
                "incremental_realized_pnl_cash": float(
                    closed.realized_pnl_cash.sum()),
                "incremental_initial_r_cash": float(risk_cash),
                "incremental_realized_r_multiple": (
                    float(closed.realized_pnl_cash.sum() / risk_cash)
                    if risk_cash > 0 else None),
            })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=ROOT / "configs/selection/alpha158_causal_risk_budget_sensitivity_v1.json")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    args = parser.parse_args()

    config = json.loads(args.config.read_text())
    source_config = ROOT / "configs/selection/alpha158_lite_v1.json"
    policy_config = ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json"
    risk_config = ROOT / "configs/selection/risk_v1.json"
    prediction_path = args.source / "causal_family_rank_predictions.csv.gz"
    snapshots = [args.config, source_config, policy_config, risk_config,
                 Path(__file__)]
    inputs = [*snapshots, prediction_path,
              args.source / "causal_family_weights.csv",
              ROOT / "scripts/backtest_alpha158_lite_low_turnover_v3.py"]
    register(args.output, inputs, snapshots, config)

    source = load_alpha158_lite_config(source_config)
    policy = load_alpha158_lite_low_turnover_config(policy_config)
    base_risk = load_risk_config(risk_config)
    if policy.target_positions != config["target_positions"]:
        raise ValueError("target positions changed")
    if base_risk.single_trade_risk_fraction != \
            config["single_trade_risk_fraction"]:
        raise ValueError("single-trade risk changed")
    for field in ("max_symbol_weight", "max_gross_exposure",
                  "max_stress_loss_fraction"):
        if getattr(base_risk, field) != config[field]:
            raise ValueError(field + " changed")

    predictions = pd.read_csv(
        prediction_path,
        usecols=["signal_asof", "symbol", "column", "causal_family_rank"],
        dtype={"symbol": str})
    predictions = predictions[predictions.signal_asof.between(
        config["evaluation_start_date"], config["evaluation_end_date"])]
    scores = rank_frame(
        predictions.rename(columns={"causal_family_rank": "alpha_score"}),
        "alpha_score", max(policy.entry_rank_limit,
                           policy.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20110101,
        end_date=config["evaluation_end_date"])

    results, segments, annual_frames, arms = [], [], [], []
    for portfolio_risk in config["portfolio_open_risk_grid"]:
        industry_risk = (float(portfolio_risk) *
                         config["industry_to_portfolio_risk_ratio"])
        same_day_risk = (float(portfolio_risk) *
                         config["same_day_to_portfolio_risk_ratio"])
        risk = replace(
            base_risk,
            risk_version="causal_risk_budget_{:.1f}pct_v1".format(
                float(portfolio_risk) * 100),
            portfolio_open_risk_fraction=float(portfolio_risk),
            industry_open_risk_fraction=float(industry_risk),
            same_day_new_risk_fraction=float(same_day_risk))
        for cost in config["costs_bps"]:
            arm = "R{:.1f}_{}bp".format(
                float(portfolio_risk) * 100, int(cost))
            result, audit = run_low_turnover(
                panel, scores, replace(source, label_slippage_bps=float(cost)),
                policy, risk, config["evaluation_end_date"],
                review_overlay=CostAwareReview(suppress_rank_exits=True))
            save_audit(args.output / arm, audit)
            full = segment_account(
                audit["curve"], config["evaluation_start_date"],
                config["evaluation_end_date"])
            statistical = segment_metrics(
                audit["curve"], config["evaluation_start_date"],
                config["evaluation_end_date"])
            full["daily_expected_shortfall_95_pct"] = statistical[
                "daily_expected_shortfall_95_pct"]
            result.update(full)
            result.update({
                "arm": arm,
                "portfolio_open_risk_fraction": portfolio_risk,
                "industry_open_risk_fraction": industry_risk,
                "same_day_new_risk_fraction": same_day_risk,
                "slippage_bps": float(cost),
            })
            results.append(result)
            for era, bounds in (
                    ("2015_2019", (20150101, 20191231)),
                    ("2020_2026", (20200101, 20260930))):
                account = segment_account(audit["curve"], *bounds)
                stats = segment_metrics(audit["curve"], *bounds)
                account["daily_expected_shortfall_95_pct"] = stats[
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
            arms.append((float(portfolio_risk), float(cost), arm))
            print(json.dumps({
                "arm": arm, "return_pct": full["return_pct"],
                "cagr_pct": full["cagr_pct"],
                "max_drawdown_pct": full["max_drawdown_pct"],
                "es95": full["daily_expected_shortfall_95_pct"],
                "average_exposure_pct": full["average_exposure_pct"],
                "risk_rejected": result["risk_rejected"],
            }), flush=True)

    results = pd.DataFrame(results)
    segments = pd.DataFrame(segments)
    annual = pd.concat(annual_frames, ignore_index=True)
    marginal = marginal_trade_metrics(args.output, arms)
    results.to_csv(args.output / "portfolio_results.csv", index=False)
    segments.to_csv(args.output / "segment_results.csv", index=False)
    annual.to_csv(args.output / "annual_returns.csv", index=False)
    marginal.to_csv(args.output / "marginal_trade_results.csv", index=False)

    primary = results[results.slippage_bps.eq(25.0)].sort_values(
        "portfolio_open_risk_fraction")
    primary_marginal = marginal[marginal.slippage_bps.eq(25.0)].set_index("arm")
    lines = [
        "# Alpha158因果动态七因子族风险容量敏感性", "",
        "固定因果权重、10只目标持仓、单笔0.25%风险、个股退出、80%总仓位上限和6%压力损失上限，仅按原比例提高组合/行业/同日风险预算。", "",
        "全部历史已经被观察；本结果只能用于结构诊断，不能据此自动进入模拟盘。", "",
        "|组合风险上限|累计收益|CAGR|最大回撤|ES95|平均仓位|风险拒绝|新增成交|新增已结交易R|", 
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in primary.itertuples(index=False):
        marginal_row = primary_marginal.loc[row.arm]
        lines.append(
            f"|{row.portfolio_open_risk_fraction*100:.1f}%|"
            f"{row.return_pct:+.2f}%|{row.cagr_pct:+.2f}%|"
            f"{row.max_drawdown_pct:.2f}%|"
            f"{row.daily_expected_shortfall_95_pct:.3f}%|"
            f"{row.average_exposure_pct:.2f}%|{row.risk_rejected}|"
            f"{int(marginal_row.incremental_entry_trades)}|"
            f"{marginal_row.incremental_realized_r_multiple:+.2f}R|"
        )
    lines += [
        "", "2015-2019主诊断、2020-2026补充、逐年表现和40/60bp成本敏感性见CSV。",
        "4%为压力档；历史表现最好的档位不构成策略选择或上线依据。", "",
    ]
    (args.output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(args.output / "completion.json", {
        "status": "COMPLETE",
        "research_status": config["research_status"],
        "parameter_search_performed": False,
        "automatic_admission": False,
        "new_strategy_selected": False,
    })


if __name__ == "__main__":
    main()
