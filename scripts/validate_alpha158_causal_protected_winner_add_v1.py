#!/usr/bin/env python3
"""Evaluate frozen protected-winner adds on causal-family Alpha158 ranks."""
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
from scripts.backtest_position_add_v1 import _materialize_overlay, _policy  # noqa: E402
from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval  # noqa: E402
from scripts.validate_alpha158_history_2015_v1 import segment_metrics  # noqa: E402


DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/alpha158_causal_family_weight_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_causal_protected_winner_add_v1_20261007")
VARIANTS = {
    "B0_current": (False, None),
    "B1_stop_sync_only": (True, None),
    "B2_protected_add_conservative": (False, "protected_winner"),
    "B3_protected_add_risk_released": (True, "protected_winner"),
}


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


def trade_metrics(directory):
    trades = pd.read_csv(directory / "logical_trades.csv")
    dispositions = pd.read_csv(directory / "lot_dispositions.csv")
    pnl = dispositions.groupby("trade_id", as_index=False).agg(
        realized_pnl_cash=("realized_pnl_cash", "sum"))
    closed = trades[trades.status.eq("CLOSED")].merge(
        pnl, on="trade_id", how="inner")
    wins = closed[closed.realized_pnl_cash > 0]
    losses = closed[closed.realized_pnl_cash < 0]
    gross_profit = wins.realized_pnl_cash.sum()
    gross_loss = -losses.realized_pnl_cash.sum()
    return {
        "closed_trades": len(closed),
        "win_rate_pct": float((closed.realized_pnl_cash > 0).mean() * 100),
        "average_win_cash": float(wins.realized_pnl_cash.mean()),
        "average_loss_cash": float(losses.realized_pnl_cash.mean()),
        "cash_payoff_ratio": float(
            wins.realized_pnl_cash.mean() /
            abs(losses.realized_pnl_cash.mean())),
        "profit_factor": float(gross_profit / gross_loss),
        "realized_pnl_cash": float(closed.realized_pnl_cash.sum()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=ROOT / "configs/selection/alpha158_causal_protected_winner_add_v1.json")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if config["variants"] != list(VARIANTS):
        raise ValueError("variant registration mismatch")

    source_config_path = ROOT / "configs/selection/alpha158_lite_v1.json"
    policy_config_path = ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json"
    risk_config_path = ROOT / "configs/selection/risk_v1.json"
    add_config_path = ROOT / "configs/selection/protected_winner_v1.json"
    prediction_path = args.source / "causal_family_rank_predictions.csv.gz"
    snapshots = [args.config, source_config_path, policy_config_path,
                 risk_config_path, add_config_path, Path(__file__)]
    inputs = [*snapshots, prediction_path,
              args.source / "causal_family_weights.csv",
              ROOT / "scripts/backtest_alpha158_lite_low_turnover_v3.py",
              ROOT / "abupy/AlphaBu/ABuPositionAddPolicy.py",
              ROOT / "abupy/AlphaBu/ABuPositionAddResearch.py"]
    register(args.output, inputs, snapshots, config)

    source = load_alpha158_lite_config(source_config_path)
    policy = load_alpha158_lite_low_turnover_config(policy_config_path)
    risk = load_risk_config(risk_config_path)
    if policy.target_positions != config["target_positions"] or \
            risk.single_trade_risk_fraction != config["single_trade_risk_fraction"] or \
            risk.portfolio_open_risk_fraction != config["portfolio_open_risk_fraction"]:
        raise ValueError("frozen portfolio configuration mismatch")

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

    results, segments, annual_frames, curves = [], [], [], {}
    for cost in config["costs_bps"]:
        for variant, (sync_stops, add_name) in VARIANTS.items():
            run_source = replace(source, label_slippage_bps=float(cost))
            result, audit = run_low_turnover(
                panel, scores, run_source, policy, risk,
                config["evaluation_end_date"],
                sync_dynamic_stops=sync_stops,
                position_add_policy=(_policy(add_name) if add_name else None),
                position_add_execution_mode="executable",
                review_overlay=CostAwareReview(suppress_rank_exits=True))
            arm = "{}_{}bp".format(variant, int(cost))
            directory = args.output / arm
            save_audit(directory, audit)
            full = segment_account(
                audit["curve"], config["evaluation_start_date"],
                config["evaluation_end_date"])
            statistical = segment_metrics(
                audit["curve"], config["evaluation_start_date"],
                config["evaluation_end_date"])
            full["daily_expected_shortfall_95_pct"] = statistical[
                "daily_expected_shortfall_95_pct"]
            filled = audit["fills"][audit["fills"].status.eq("filled")]
            effects = filled.position_effect.value_counts()
            row = {
                **result, **full, **trade_metrics(directory),
                "arm": arm, "variant": variant,
                "slippage_bps": float(cost),
                "dynamic_stop_sync": sync_stops,
                "filled_opens": int(effects.get("OPEN", 0)),
                "filled_adds": int(effects.get("INCREASE", 0)),
                "filled_closes": int(effects.get("CLOSE", 0)),
            }
            results.append(row)
            curves[(variant, float(cost))] = audit["curve"].capital.to_numpy(
                dtype=float)
            for era, bounds in (
                    ("2015_2019", (20150101, 20191231)),
                    ("2020_2026", (20200101, 20260930))):
                account = segment_account(audit["curve"], *bounds)
                stats = segment_metrics(audit["curve"], *bounds)
                account.update({
                    "arm": arm, "variant": variant, "era": era,
                    "slippage_bps": float(cost),
                    "daily_expected_shortfall_95_pct": stats[
                        "daily_expected_shortfall_95_pct"],
                })
                segments.append(account)
            annual = annual_returns(audit["curve"])
            annual["arm"] = arm
            annual["variant"] = variant
            annual["slippage_bps"] = float(cost)
            annual_frames.append(annual)
            print(json.dumps({
                "arm": arm, "return_pct": full["return_pct"],
                "max_drawdown_pct": full["max_drawdown_pct"],
                "average_exposure_pct": full["average_exposure_pct"],
                "win_rate_pct": row["win_rate_pct"],
                "filled_adds": row["filled_adds"],
            }), flush=True)

    existing = pd.read_csv(args.source / "causal_25bp" / "daily_nav.csv")
    reproduced = pd.read_csv(args.output / "B0_current_25bp" / "daily_nav.csv")
    pd.testing.assert_frame_equal(
        reproduced.reset_index(drop=True), existing.reset_index(drop=True),
        check_dtype=False, rtol=1e-11, atol=1e-7)

    shadow_result, shadow = run_low_turnover(
        panel, scores, replace(source, label_slippage_bps=25.0), policy, risk,
        config["evaluation_end_date"], sync_dynamic_stops=False,
        position_add_policy=_policy("protected_winner"),
        position_add_execution_mode="shadow",
        review_overlay=CostAwareReview(suppress_rank_exits=True))
    shadow_directory = args.output / "fixed_path_shadow_25bp"
    save_audit(shadow_directory, shadow)
    pd.testing.assert_frame_equal(
        shadow["curve"].reset_index(drop=True), existing.reset_index(drop=True),
        check_dtype=False, rtol=1e-11, atol=1e-7)
    fixed_path = _materialize_overlay(panel, shadow, shadow_directory)
    fixed_path["triggered_proposals"] = len(shadow.get("add_proposals", []))
    fixed_path["base_path_return_pct"] = shadow_result["return_pct"]
    fixed_path = {
        key: (None if isinstance(value, (float, np.floating)) and
              not np.isfinite(value) else value)
        for key, value in fixed_path.items()
    }
    write_json(args.output / "fixed_path_overlay_summary.json", fixed_path)

    results = pd.DataFrame(results)
    segments = pd.DataFrame(segments)
    annual = pd.concat(annual_frames, ignore_index=True)
    results.to_csv(args.output / "portfolio_results.csv", index=False)
    segments.to_csv(args.output / "segment_results.csv", index=False)
    annual.to_csv(args.output / "annual_returns.csv", index=False)

    comparisons = []
    for cost in config["costs_bps"]:
        for candidate, reference in (
                ("B1_stop_sync_only", "B0_current"),
                ("B2_protected_add_conservative", "B0_current"),
                ("B3_protected_add_risk_released", "B1_stop_sync_only"),
                ("B3_protected_add_risk_released", "B0_current")):
            c = results[(results.variant == candidate) &
                        results.slippage_bps.eq(cost)].iloc[0]
            b = results[(results.variant == reference) &
                        results.slippage_bps.eq(cost)].iloc[0]
            interval = paired_block_interval(
                curves[(reference, float(cost))],
                curves[(candidate, float(cost))])
            comparisons.append({
                "candidate": candidate, "reference": reference,
                "slippage_bps": cost,
                "return_delta_pp": c.return_pct - b.return_pct,
                "max_drawdown_improvement_pp": (
                    c.max_drawdown_pct - b.max_drawdown_pct),
                "es95_improvement_pp": (
                    c.daily_expected_shortfall_95_pct -
                    b.daily_expected_shortfall_95_pct),
                "exposure_delta_pp": (
                    c.average_exposure_pct - b.average_exposure_pct),
                **interval,
            })
    comparisons = pd.DataFrame(comparisons)
    comparisons.to_csv(args.output / "comparisons.csv", index=False)

    primary = results[results.slippage_bps.eq(25)].set_index("variant")
    lines = [
        "# Alpha158因果动态七因子族盈利持仓追加实验", "",
        "固定因果选股、2%组合开放风险、单笔0.25%风险及原退出规则；只分解动态止损风险释放与既有ProtectedWinner单次追加规则。", "",
        "|版本|累计收益|CAGR|最大回撤|ES95|平均仓位|胜率|盈亏比|利润因子|加仓|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in VARIANTS:
        row = primary.loc[variant]
        lines.append(
            f"|{variant}|{row.return_pct:+.2f}%|{row.cagr_pct:+.2f}%|"
            f"{row.max_drawdown_pct:.2f}%|"
            f"{row.daily_expected_shortfall_95_pct:.3f}%|"
            f"{row.average_exposure_pct:.2f}%|{row.win_rate_pct:.2f}%|"
            f"{row.cash_payoff_ratio:.2f}|{row.profit_factor:.2f}|"
            f"{int(row.filled_adds)}|"
        )
    lines += ["", "固定路径追加诊断：", "",
              f"- 触发提案：{fixed_path['triggered_proposals']}；实际回放追加：{fixed_path['overlay_filled_adds']}。",
              "- 已结与期末未结追加损益见 `fixed_path_overlay_summary.json`；缺失估值明确记录为null。",
              "", "所有历史均已观察，结果不得自动替换模拟盘策略。", ""]
    (args.output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(args.output / "completion.json", {
        "status": "COMPLETE",
        "baseline_reproduced": True,
        "shadow_fixed_path_reproduced": True,
        "research_status": config["research_status"],
        "parameter_search_performed": False,
        "automatic_admission": False,
        "new_strategy_selected": False,
    })


if __name__ == "__main__":
    main()
