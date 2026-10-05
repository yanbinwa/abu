#!/usr/bin/env python3
"""Pre-registered historical validation of Alpha158 industry hierarchy v1."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    Alpha158LiteFeatureEngine, load_alpha158_lite_config,
    load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuAlphaIndustryHierarchy import (  # noqa: E402
    IndustryHierarchyHistory, IndustryHierarchyOverlay,
    load_industry_hierarchy_config,
)
from abupy.AlphaBu.ABuArtifactManifest import sha256_file  # noqa: E402
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuMatchedPlaceboV2 import (  # noqa: E402
    MatchedPlaceboV2, PlaceboConfig,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.backtest_alpha158_event_exit_only_2025_2026 import (  # noqa: E402
    data_paths, save_audit, write_json,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval  # noqa: E402


VARIANTS = (
    "H0_a0", "H1_bottom20_gate", "H2_top30_industry_first",
    "H3_persistent_leader", "H4_industry_cap")
LABELS = {
    "H0_a0": "冻结A0",
    "H1_bottom20_gate": "排除行业后20%",
    "H2_top30_industry_first": "行业前30%后按A0选股",
    "H3_persistent_leader": "H2＋龙头连续两期",
    "H4_industry_cap": "H3＋单行业最多2只",
}
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_industry_hierarchy_v1_20261004")
DEFAULT_PREDICTIONS = Path(
    "/Users/wjy/abu/backtests/alpha158_lite_v1_hardened_20261003/"
    "oos_predictions.csv.gz")
DEFAULT_REPORT = DEFAULT_PREDICTIONS.parent / "research_report.json"
DEFAULT_SIGNAL = Path("/Users/wjy/abu/shadow/alpha158_forward_v1/data/signal")
DEFAULT_RESEARCH = Path("/Users/wjy/abu/shadow/alpha158_forward_v1/data/research")
DEFAULT_GOLDEN = Path(
    "/Users/wjy/abu/backtests/current_strategy_comparison_20261004_v2/"
    "main/alpha_no_rank_exit_25bp")


def trade_metrics(fills):
    buys = fills[(fills.side == "buy") & fills.status.eq("filled")]
    sells = fills[(fills.side == "sell") & fills.status.eq("filled")]
    pnl, returns = [], []
    for symbol, group in buys.groupby("symbol"):
        remaining = sells[sells.symbol.eq(symbol)].sort_values("date")
        for buy in group.sort_values("date").itertuples():
            eligible = remaining[remaining.date >= buy.date]
            if eligible.empty:
                continue
            sell = eligible.iloc[0]
            remaining = remaining.drop(eligible.index[0])
            buy_cash = (buy.quantity*buy.fill_price_raw + buy.commission +
                        buy.transfer_fee + buy.stamp_tax)
            sell_cash = (sell.quantity*sell.fill_price_raw - sell.commission -
                         sell.transfer_fee - sell.stamp_tax)
            value = float(sell_cash-buy_cash)
            pnl.append(value)
            returns.append(value/buy_cash if buy_cash > 0 else np.nan)
    pnl = np.asarray(pnl, dtype=float)
    positive = float(pnl[pnl > 0].sum()) if len(pnl) else 0.
    negative = float(-pnl[pnl < 0].sum()) if len(pnl) else 0.
    return {
        "trade_win_rate_pct": float(np.mean(pnl > 0)*100) if len(pnl) else np.nan,
        "mean_trade_return_pct": float(np.nanmean(returns)*100)
        if returns else np.nan,
        "profit_factor": positive/negative if negative > 0 else np.nan,
    }


def golden_check(audit, golden):
    actual = audit["curve"].reset_index(drop=True)
    expected = pd.read_csv(golden / "daily_nav.csv")
    columns = ["date", "cash", "stocks", "capital", "exposure",
               "liquidation_nav_3_limits"]
    pd.testing.assert_frame_equal(
        actual[columns], expected[columns], check_dtype=False,
        rtol=1e-11, atol=1e-7)
    actual_fills = audit["fills"]
    expected_fills = pd.read_csv(golden / "fills.csv")
    columns = ["date", "symbol", "side", "status", "quantity",
               "reference_price", "fill_price_raw", "commission",
               "transfer_fee", "stamp_tax", "slippage_cost",
               "position_effect"]
    pd.testing.assert_frame_equal(
        actual_fills[columns].reset_index(drop=True),
        expected_fills[columns].reset_index(drop=True),
        check_dtype=False, rtol=1e-11, atol=1e-7)


def matched_placebo(panel, source, audit, variant, replicates=5000):
    orders = pd.DataFrame([asdict(item) for item in audit["orders"]])
    fills = audit["fills"]
    buys = fills[(fills.side == "buy") & fills.status.eq("filled")]
    if buys.empty or orders.empty:
        return {"variant": variant, "matched_trades": 0}
    opened = buys[["intent_id", "symbol"]].merge(
        orders[["intent_id", "created_asof"]], on="intent_id", how="left")
    runner = MatchedPlaceboV2(panel, PlaceboConfig(
        pool_size=50, replicates=replicates, seed=20261004))
    engine = Alpha158LiteFeatureEngine(panel, source)
    date_index = {int(date): position for position, date in enumerate(panel.dates)}
    target_values, pools = [], []
    for row in opened.itertuples():
        day = date_index[int(row.created_asof)]
        target = panel.symbol_index[str(row.symbol)]
        if day+source.label_horizon_sessions >= len(panel.dates):
            continue
        intent = SimpleNamespace(
            intent_id="{}-{}".format(variant, row.intent_id),
            signal_asof=int(row.created_asof), symbol=str(row.symbol))
        pool = runner.matching_pool(intent)
        industry = int(panel.industry[day, target])
        pool = pool[panel.industry[day, pool] == industry]
        if not len(pool):
            continue
        columns = np.r_[target, pool]
        labels, _ = engine._labels(
            day, columns, horizon=source.label_horizon_sessions)
        controls = labels[1:][np.isfinite(labels[1:])]
        if not np.isfinite(labels[0]) or not len(controls):
            continue
        target_values.append(float(labels[0]))
        pools.append(controls)
    if not target_values:
        return {"variant": variant, "matched_trades": 0}
    rng = np.random.default_rng(20261004)
    target_mean = float(np.mean(target_values))
    differences = np.empty(replicates, dtype=float)
    control_means = np.empty(replicates, dtype=float)
    for replicate in range(replicates):
        controls = [values[rng.integers(0, len(values))] for values in pools]
        control_means[replicate] = float(np.mean(controls))
        differences[replicate] = target_mean-control_means[replicate]
    return {
        "variant": variant, "matched_trades": len(target_values),
        "target_mean_excess20_pct": target_mean*100,
        "placebo_mean_excess20_pct": float(control_means.mean()*100),
        "observed_increment_pp": float(target_mean*100-control_means.mean()*100),
        "ci95_low_pp": float(np.quantile(differences, .025)*100),
        "ci95_high_pp": float(np.quantile(differences, .975)*100),
        "positive_increment_probability": float(np.mean(differences > 0)),
        "replicates": int(replicates),
    }


def make_report(output, results, uncertainty, placebo, admission):
    primary = results[results.slippage_bps.eq(25)].set_index("variant")
    lines = [
        "# Alpha158 行业分层选股 v1 历史验证", "",
        "本实验在查看结果前冻结H0—H4，使用相同滚动OOS预测、A0事件退出、"
        "0.25%单笔风险和统一执行器。历史区间已经被观察，结果只用于淘汰和诊断。",
        "", "## 25bp主结果", "",
        "|版本|规则|收益|最大回撤|平均仓位|胜率|平均单笔收益|平均R|压力收益|买入|",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in VARIANTS:
        row = primary.loc[variant]
        lines.append(
            "|{}|{}|{:+.2f}%|{:.2f}%|{:.2f}%|{:.2f}%|{:+.2f}%|{:+.3f}|"
            "{:+.2f}%|{}|".format(
                variant, LABELS[variant], row.return_pct,
                row.max_drawdown_pct, row.average_exposure_pct,
                row.trade_win_rate_pct, row.mean_trade_return_pct,
                row.mean_r, row.liquidation_3_limits_return_pct,
                int(row.filled_buys)))
    lines += ["", "## 相对H0的路径不确定性", "",
              "|版本|收益增量|20日配对区块95%区间|正增量概率|",
              "|---|---:|---:|---:|"]
    for row in uncertainty.itertuples():
        lines.append("|{}|{:+.2f}pp|[{:+.2f}, {:+.2f}]pp|{:.1f}%|".format(
            row.variant, row.observed_increment_pp, row.ci95_low_pp,
            row.ci95_high_pp,
            (1-row.resampled_nonpositive_fraction)*100))
    lines += ["", "## 同行业匹配置换", "",
              "目标为真实买入信号的次日可执行开盘至第20日收盘超额收益；"
              "替代股票只用信号日可见的行业、价格、流动性、市值、波动和beta匹配。"
              "这是信号层诊断，不重放完整组合现金路径。", "",
              "|版本|有效交易|目标均值|同行业替代均值|增量|95%区间|",
              "|---|---:|---:|---:|---:|---:|"]
    for row in placebo.itertuples():
        lines.append("|{}|{}|{:+.2f}%|{:+.2f}%|{:+.2f}pp|[{:+.2f}, {:+.2f}]pp|".format(
            row.variant, int(row.matched_trades),
            row.target_mean_excess20_pct, row.placebo_mean_excess20_pct,
            row.observed_increment_pp, row.ci95_low_pp, row.ci95_high_pp))
    lines += ["", "## 准入判断", "",
              "任何版本只有同时改善25/40/60bp收益、25bp回撤和压力风险、胜率与平均R，"
              "且配对区间和同行业匹配区间下界均大于零，才允许替换A0。", ""]
    for variant, payload in admission.items():
        lines.append("- {}：{}；未通过项：{}。".format(
            variant, "通过" if payload["passed"] else "未通过",
            "、".join(payload["failed"]) if payload["failed"] else "无"))
    lines += ["", "## 限制", "",
              "- 2023—2026历史已经参与多轮研究，不是新的独立留出样本。",
              "- 行业匹配检验不模拟替代组合的后续资金竞争和退出路径。",
              "- 结果不自动修改前瞻影子账户或模拟盘配置。", ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--research-report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--signal-dir", type=Path, default=DEFAULT_SIGNAL)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument("--start-date", type=int, default=20230727)
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)

    config_dir = ROOT / "configs/selection"
    config_paths = [config_dir / name for name in (
        "alpha158_industry_hierarchical_v1.json",
        "alpha158_event_exit_only_research_v1.json",
        "alpha158_lite_low_turnover_v3.json", "alpha158_lite_v1.json",
        "risk_v1.json")]
    code_paths = [Path(__file__),
                  ROOT / "abupy/AlphaBu/ABuAlphaIndustryHierarchy.py"]
    market_paths = data_paths(args.signal_dir, args.research_dir)
    registration = {
        "registered_at": datetime.now().astimezone().isoformat(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "period": [args.start_date, args.end_date],
        "variants": list(VARIANTS), "slippage_bps": [25, 40, 60],
        "initial_cash": 1_000_000.0, "placebo_replicates": 5000,
        "parameter_search": False, "research_only": True,
        "new_holdout": False, "automatic_admission": False,
        "admission_rule": (
            "all costs return improve; 25bp drawdown, stress return and stress "
            "drawdown improve; win rate and mean R improve; paired and same-industry "
            "placebo 95% lower bounds positive"),
        "prediction_sha256": sha256_file(args.predictions),
        "research_report_sha256": sha256_file(args.research_report),
        "config_hashes": {str(path): sha256_file(path) for path in config_paths},
        "code_hashes": {str(path): sha256_file(path) for path in code_paths},
        "market_hashes": {str(path): sha256_file(path)
                          for path in market_paths if path.is_file()},
    }
    write_json(args.output / "experiment_registration.json", registration)

    source = load_alpha158_lite_config(config_dir / "alpha158_lite_v1.json")
    policy = load_alpha158_lite_low_turnover_config(
        config_dir / "alpha158_lite_low_turnover_v3.json")
    risk = load_risk_config(config_dir / "risk_v1.json")
    hierarchy = load_industry_hierarchy_config(
        config_dir / "alpha158_industry_hierarchical_v1.json")
    research = json.loads(args.research_report.read_text())
    if research["config_sha256"] != source.sha256:
        raise ValueError("prediction/configuration mismatch")
    predictions = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", policy.score_column,
                 "train_end"], dtype={"symbol": str})
    predictions = predictions[
        predictions.signal_asof.between(args.start_date, args.end_date)].copy()
    if predictions.empty or not (predictions.train_end < predictions.signal_asof).all():
        raise ValueError("non-empty OOS predictions required")
    scores = rank_frame(
        predictions, policy.score_column,
        max(policy.entry_rank_limit, policy.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    for row in predictions[["symbol", "column"]].drop_duplicates().itertuples():
        if panel.symbols[int(row.column)] != row.symbol:
            raise ValueError("prediction symbol-column mapping mismatch")
    history = IndustryHierarchyHistory(panel, hierarchy)

    rows, curves, primary_audits, overlays = [], {}, {}, {}
    for cost in (25, 40, 60):
        for variant in VARIANTS:
            print("RUN {} {}bp".format(variant, cost), flush=True)
            overlay = (CostAwareReview(suppress_rank_exits=True)
                       if variant == "H0_a0"
                       else IndustryHierarchyOverlay(history, variant))
            run_policy = replace(
                policy, strategy_version="{}_{}_{}bp".format(
                    hierarchy.strategy_version, variant.lower(), cost))
            result, audit = run_low_turnover(
                panel, scores, replace(source, label_slippage_bps=float(cost)),
                run_policy, risk, args.end_date, review_overlay=overlay)
            result.update(variant=variant, label=LABELS[variant],
                          **trade_metrics(audit["fills"]))
            rows.append(result)
            curves[(variant, cost)] = audit["curve"].capital.to_numpy(float)
            directory = args.output / "{}bp".format(cost) / variant
            save_audit(directory, audit)
            annual_returns(audit["curve"]).to_csv(
                directory / "annual_returns.csv", index=False)
            write_json(directory / "metrics.json", result)
            if variant != "H0_a0":
                decisions = pd.DataFrame(overlay.entry_decisions)
                decisions.to_csv(directory / "industry_hierarchy_decisions.csv",
                                 index=False)
            if cost == 25:
                primary_audits[variant] = audit
                overlays[variant] = overlay
            if variant == "H0_a0" and cost == 25:
                golden_check(audit, args.golden)
    results = pd.DataFrame(rows)
    results.to_csv(args.output / "results.csv", index=False)

    uncertainty_rows = []
    for variant in VARIANTS[1:]:
        uncertainty_rows.append({
            "variant": variant,
            **paired_block_interval(curves[("H0_a0", 25)],
                                    curves[(variant, 25)])})
    uncertainty = pd.DataFrame(uncertainty_rows)
    uncertainty.to_csv(args.output / "paired_uncertainty.csv", index=False)

    placebo = pd.DataFrame([
        matched_placebo(panel, source, primary_audits[variant], variant)
        for variant in VARIANTS[1:]])
    placebo.to_csv(args.output / "matched_industry_placebo.csv", index=False)

    by_variant_cost = results.set_index(["variant", "slippage_bps"])
    base = by_variant_cost.loc[("H0_a0", 25)]
    interval = uncertainty.set_index("variant")
    placebo_index = placebo.set_index("variant")
    admission = {}
    for variant in VARIANTS[1:]:
        candidate = by_variant_cost.loc[(variant, 25)]
        checks = {
            "return_all_costs": all(
                by_variant_cost.loc[(variant, cost), "return_pct"] >
                by_variant_cost.loc[("H0_a0", cost), "return_pct"]
                for cost in (25, 40, 60)),
            "max_drawdown": candidate.max_drawdown_pct >= base.max_drawdown_pct,
            "stress_return": candidate.liquidation_3_limits_return_pct >=
            base.liquidation_3_limits_return_pct,
            "stress_drawdown": candidate.liquidation_3_limits_max_drawdown_pct >=
            base.liquidation_3_limits_max_drawdown_pct,
            "win_rate": candidate.trade_win_rate_pct > base.trade_win_rate_pct,
            "mean_r": candidate.mean_r >= base.mean_r,
            "paired_ci": interval.loc[variant, "ci95_low_pp"] > 0,
            "placebo_ci": (placebo_index.loc[variant, "matched_trades"] > 0 and
                            placebo_index.loc[variant, "ci95_low_pp"] > 0),
        }
        admission[variant] = {
            "checks": checks, "failed": [key for key, value in checks.items()
                                           if not bool(value)],
            "passed": all(checks.values())}
    write_json(args.output / "admission.json", admission)
    make_report(args.output, results, uncertainty, placebo, admission)

    changed = [path for path, digest in registration["market_hashes"].items()
               if not Path(path).is_file() or sha256_file(path) != digest]
    verification = {
        "status": "PASSED" if not changed else "INVALIDATED_INPUT_CHANGED",
        "h0_25bp_golden_match": True, "completed_runs": len(results),
        "oos_rows_valid": True, "inputs_unchanged": not changed,
        "changed_inputs": changed, "automatic_admission": False,
    }
    write_json(args.output / "verification.json", verification)
    if changed:
        raise ValueError("market inputs changed during validation")
    print(results[results.slippage_bps.eq(25)][[
        "variant", "return_pct", "max_drawdown_pct", "average_exposure_pct",
        "trade_win_rate_pct", "mean_r", "filled_buys"]].to_string(index=False))
    print(json.dumps(verification, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
