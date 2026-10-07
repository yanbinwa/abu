#!/usr/bin/env python3
"""Replay industry-confirmed winner adds in an idle-cash-isolated sleeve."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
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
from abupy.AlphaBu.ABuAlphaIndustryGate import IndustryExcessHistory  # noqa: E402
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuPositionAddResearch import (  # noqa: E402
    replay_isolated_add_sleeve,
)
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
from scripts.backtest_position_add_v1 import _policy  # noqa: E402
from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval  # noqa: E402
from scripts.validate_alpha158_history_2015_v1 import segment_metrics  # noqa: E402


DEFAULT_SOURCE = Path(
    "/Users/wjy/abu/backtests/alpha158_causal_family_weight_v1_20261007")
DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_causal_industry_confirmed_add_sleeve_v1_20261008")


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(
        value, ensure_ascii=False, indent=2, sort_keys=True,
        default=str, allow_nan=False) + "\n", encoding="utf-8")


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


def filter_proposals(audit, variant, panel, industry_history,
                     minimum_industry_excess):
    if variant == "U_ungated":
        return list(audit["add_proposals"]), []
    evaluations = {item.evaluation_id: item
                   for item in audit["policy_evaluations"]}
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    accepted, decisions = [], []
    for proposal in audit["add_proposals"]:
        evaluation = evaluations[proposal.evaluation_id]
        values = evaluation.evaluated_inputs
        day = date_index.get(int(proposal.signal_asof))
        excess = np.nan
        if day is not None and proposal.symbol in panel.symbol_index:
            excess = float(industry_history.values(day)[
                panel.symbol_index[proposal.symbol]])
        industry_confirmed = bool(
            np.isfinite(excess) and
            excess >= float(minimum_industry_excess))
        benchmark = values.get("benchmark_close")
        ma200 = values.get("benchmark_ma200")
        market_up = (benchmark is not None and ma200 is not None and
                     np.isfinite(benchmark) and np.isfinite(ma200) and
                     float(benchmark) > float(ma200))
        passed = industry_confirmed and (
            variant == "I_industry_leader" or market_up)
        decisions.append({
            "proposal_id": proposal.proposal_id,
            "signal_asof": proposal.signal_asof,
            "symbol": proposal.symbol,
            "variant": variant,
            "stock_excess_industry_20d": excess,
            "minimum_industry_excess": float(minimum_industry_excess),
            "industry_confirmed": industry_confirmed,
            "benchmark_close": benchmark,
            "benchmark_ma200": ma200,
            "market_up": market_up,
            "passed": passed,
        })
        if passed:
            accepted.append(proposal)
    return accepted, decisions


def combine_idle_cash(base, sleeve, reserve):
    left = base[["date", "cash", "stocks", "capital"]].copy()
    right = sleeve[["date", "cash", "stocks", "capital"]].copy()
    merged = left.merge(
        right, on="date", suffixes=("_base", "_sleeve"),
        validate="one_to_one")
    merged["cash"] = merged.cash_base - reserve + merged.cash_sleeve
    if merged.cash.min() < -1e-6:
        raise ValueError("sleeve reserve competes with core cash")
    merged["stocks"] = merged.stocks_base + merged.stocks_sleeve
    merged["capital"] = merged.capital_base - reserve + merged.capital_sleeve
    merged["exposure"] = merged.stocks / merged.capital
    return merged


def daily_expected_shortfall_95(curve, start_date, end_date):
    work = curve[curve.date.astype(int).between(
        int(start_date), int(end_date))]
    returns = work.capital.astype(float).pct_change().dropna()
    if returns.empty:
        return 0.0
    threshold = returns.quantile(0.05)
    tail = returns[returns <= threshold]
    return float((tail.mean() if len(tail) else threshold) * 100)


def candidate_key(variant, sleeve_cash_fraction):
    return "{}|{:.6f}".format(variant, float(sleeve_cash_fraction))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path,
        default=ROOT / "configs/selection/alpha158_causal_industry_confirmed_add_sleeve_v1.json")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research_2015_v1"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    source_config_path = ROOT / "configs/selection/alpha158_lite_v1.json"
    policy_config_path = ROOT / "configs/selection/alpha158_lite_low_turnover_v3.json"
    risk_config_path = ROOT / "configs/selection/risk_v1.json"
    add_config_path = ROOT / "configs/selection/protected_winner_v1.json"
    prediction_path = args.source / "causal_family_rank_predictions.csv.gz"
    snapshots = [args.config, source_config_path, policy_config_path,
                 risk_config_path, add_config_path, Path(__file__)]
    inputs = [*snapshots, prediction_path,
              args.source / "causal_family_weights.csv",
              ROOT / "abupy/AlphaBu/ABuPositionAddResearch.py",
              ROOT / "abupy/AlphaBu/ABuAlphaIndustryGate.py",
              ROOT / "abupy/AlphaBu/ABuSelectionFeatures.py"]
    register(args.output, inputs, snapshots, config)

    source = load_alpha158_lite_config(source_config_path)
    policy = load_alpha158_lite_low_turnover_config(policy_config_path)
    risk = load_risk_config(risk_config_path)
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
    industry_history = IndustryExcessHistory(panel, source)
    total_initial = float(config["total_initial_cash"])
    rows, segments, annual_frames, comparisons, decisions = [], [], [], [], []
    for cost in config["costs_bps"]:
        run_source = replace(source, label_slippage_bps=float(cost))
        base_result, audit = run_low_turnover(
            panel, scores, run_source, policy, risk,
            config["evaluation_end_date"], sync_dynamic_stops=False,
            position_add_policy=_policy("protected_winner"),
            position_add_execution_mode="shadow",
            review_overlay=CostAwareReview(suppress_rank_exits=True))
        expected = pd.read_csv(
            args.source / "causal_{}bp".format(int(cost)) / "daily_nav.csv")
        pd.testing.assert_frame_equal(
            audit["curve"].reset_index(drop=True), expected.reset_index(drop=True),
            check_dtype=False, rtol=1e-11, atol=1e-7)
        base_dir = args.output / "base_{}bp".format(int(cost))
        save_audit(base_dir, audit)
        base_curve = audit["curve"]
        base_stat = segment_metrics(
            base_curve, config["evaluation_start_date"],
            config["evaluation_end_date"])
        base_account = segment_account(
            base_curve, config["evaluation_start_date"],
            config["evaluation_end_date"])
        min_core_cash = float(base_curve.cash.min())
        for variant in config["gate_variants"]:
            proposals, gate_rows = filter_proposals(
                audit, variant, panel, industry_history,
                config["industry_minimum_excess"])
            for item in gate_rows:
                item.update({"slippage_bps": float(cost)})
            decisions.extend(gate_rows)
            for fraction in [config["sleeve_cash_fraction"]]:
                reserve = total_initial * float(fraction)
                if reserve > min_core_cash:
                    raise ValueError("registered sleeve exceeds core idle cash")
                execution = ExecutionConfig(
                    initial_cash=reserve, slippage_bps=float(cost),
                    mode="pit_corrected")
                replay = replay_isolated_add_sleeve(
                    panel, proposals, audit["lot_dispositions"], execution,
                    reserve, start_date=int(base_curve.date.iloc[0]),
                    end_date=int(base_curve.date.iloc[-1]),
                    max_gross_exposure=float(
                        config["sleeve_max_gross_exposure"]))
                combined = combine_idle_cash(base_curve, replay["curve"], reserve)
                arm = "{}_S{}_{}bp".format(
                    variant, int(float(fraction) * 100), int(cost))
                directory = args.output / arm
                directory.mkdir()
                replay["curve"].to_csv(directory / "sleeve_daily_nav.csv", index=False)
                combined.to_csv(directory / "combined_daily_nav.csv", index=False)
                pd.DataFrame([asdict(item) for item in replay["entries"]]).to_csv(
                    directory / "sleeve_entries.csv", index=False)
                pd.DataFrame([asdict(item) for item in replay["dispositions"]]).to_csv(
                    directory / "sleeve_dispositions.csv", index=False)
                replay["rejections"].to_csv(
                    directory / "sleeve_rejections.csv", index=False)
                pd.DataFrame([asdict(item) for item in replay["open_lots"]]).to_csv(
                    directory / "sleeve_open_lots.csv", index=False)
                account = segment_account(
                    combined, config["evaluation_start_date"],
                    config["evaluation_end_date"])
                es95 = daily_expected_shortfall_95(
                    combined, config["evaluation_start_date"],
                    config["evaluation_end_date"])
                account.update({
                    "arm": arm, "gate_variant": variant,
                    "sleeve_cash_fraction": float(fraction),
                    "slippage_bps": float(cost),
                    "daily_expected_shortfall_95_pct": es95,
                    "triggered_proposals": len(proposals),
                    "filled_adds": len(replay["entries"]),
                    "closed_adds": len(replay["dispositions"]),
                    "open_adds_end": len(replay["open_lots"]),
                    "rejected_adds": len(replay["rejections"]),
                    "sleeve_ending_capital": float(
                        replay["curve"].capital.iloc[-1]),
                    "sleeve_return_pct": float(
                        (replay["curve"].capital.iloc[-1] / reserve - 1) * 100),
                    "minimum_adjusted_core_cash": float(
                        (base_curve.cash - reserve).min()),
                })
                rows.append(account)
                for era, bounds in (
                        ("2015_2019", (20150101, 20191231)),
                        ("2020_2026", (20200101, 20260930))):
                    metric = segment_account(combined, *bounds)
                    metric.update({
                        "arm": arm, "gate_variant": variant, "era": era,
                        "sleeve_cash_fraction": float(fraction),
                        "slippage_bps": float(cost),
                    })
                    segments.append(metric)
                annual = annual_returns(combined)
                annual["arm"] = arm
                annual["gate_variant"] = variant
                annual["sleeve_cash_fraction"] = float(fraction)
                annual["slippage_bps"] = float(cost)
                annual_frames.append(annual)
                interval = paired_block_interval(
                    base_curve.capital.to_numpy(float),
                    combined.capital.to_numpy(float))
                comparisons.append({
                    "arm": arm, "gate_variant": variant,
                    "sleeve_cash_fraction": float(fraction),
                    "slippage_bps": float(cost),
                    "return_delta_pp": (
                        account["return_pct"] - base_account["return_pct"]),
                    "max_drawdown_improvement_pp": (
                        account["max_drawdown_pct"] -
                        base_account["max_drawdown_pct"]),
                    "es95_improvement_pp": (
                        es95 -
                        base_stat["daily_expected_shortfall_95_pct"]),
                    **interval,
                })
                print(json.dumps({
                    "arm": arm, "return_pct": account["return_pct"],
                    "delta_pp": account["return_pct"] - base_account["return_pct"],
                    "max_drawdown_pct": account["max_drawdown_pct"],
                    "average_exposure_pct": account["average_exposure_pct"],
                    "filled_adds": len(replay["entries"]),
                }), flush=True)

    results = pd.DataFrame(rows)
    segment_frame = pd.DataFrame(segments)
    annual_frame = pd.concat(annual_frames, ignore_index=True)
    comparison = pd.DataFrame(comparisons)
    results.to_csv(args.output / "portfolio_results.csv", index=False)
    segment_frame.to_csv(args.output / "segment_results.csv", index=False)
    annual_frame.to_csv(args.output / "annual_returns.csv", index=False)
    comparison.to_csv(args.output / "comparisons.csv", index=False)
    pd.DataFrame(decisions).drop_duplicates().to_csv(
        args.output / "gate_decisions.csv", index=False)

    checks = config["admission_checks"]
    gates = {}
    for (variant, fraction), group in results.groupby(
            ["gate_variant", "sleeve_cash_fraction"], sort=True):
        primary_row = group[group.slippage_bps.eq(25.0)].iloc[0]
        comp = comparison[comparison.arm.eq(primary_row.arm)].iloc[0]
        eras = segment_frame[
            (segment_frame.gate_variant == variant) &
            segment_frame.sleeve_cash_fraction.eq(fraction) &
            segment_frame.slippage_bps.eq(25.0)]
        base_segments = {
            era: segment_account(
                pd.read_csv(args.source / "causal_25bp" / "daily_nav.csv"),
                *(20150101, 20191231) if era == "2015_2019" else
                (20200101, 20260930))["return_pct"]
            for era in ("2015_2019", "2020_2026")
        }
        gate = {
            "return_delta_positive_all_costs": bool(
                all(row.return_pct > segment_account(
                    pd.read_csv(args.source / "causal_{}bp".format(
                        int(row.slippage_bps)) / "daily_nav.csv"),
                    config["evaluation_start_date"],
                    config["evaluation_end_date"])["return_pct"]
                    for row in group.itertuples(index=False))),
            "return_delta_positive_both_eras_at_25bp": bool(all(
                row_.return_pct > base_segments[row_.era]
                for row_ in eras.itertuples(index=False))),
            "max_drawdown_degradation_within_limit_25bp": bool(
                comp.max_drawdown_improvement_pp >=
                -float(checks["max_drawdown_degradation_limit_pp"])),
            "es95_degradation_within_limit_25bp": bool(
                comp.es95_improvement_pp >=
                -float(checks["es95_degradation_limit_pp"])),
            "minimum_filled_adds": bool(
                primary_row.filled_adds >= int(checks["minimum_filled_adds"])),
        }
        gates[candidate_key(variant, fraction)] = {
            "checks": gate, "passed": bool(all(gate.values()))}
    write_json(args.output / "gates.json", gates)

    primary = results[results.slippage_bps.eq(25.0)].sort_values(
        ["sleeve_cash_fraction", "gate_variant"])
    comp25 = comparison.set_index("arm")
    lines = [
        "# Alpha158因果动态策略行业确认追加仓", "",
        f"核心账户路径保持不变；固定隔离{config['sleeve_cash_fraction']:.1%}现金。I要求个股20日收益不弱于行业均值，IM再要求沪深300位于因果MA200上方。", "",
        "|版本|隔离现金|累计收益|相对核心|最大回撤|ES95|平均仓位|追加成交|追加仓收益|门槛|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in primary.itertuples(index=False):
        comp = comp25.loc[row.arm]
        lines.append(
            f"|{row.gate_variant}|{row.sleeve_cash_fraction:.1%}|"
            f"{row.return_pct:+.2f}%|{comp.return_delta_pp:+.2f}pp|"
            f"{row.max_drawdown_pct:.2f}%|"
            f"{row.daily_expected_shortfall_95_pct:.3f}%|"
            f"{row.average_exposure_pct:.2f}%|{row.filled_adds}|"
            f"{row.sleeve_return_pct:+.2f}%|"
            f"{'通过' if gates[candidate_key(row.gate_variant, row.sleeve_cash_fraction)]['passed'] else '未通过'}|"
        )
    lines += ["", "全部历史均已观察；任何通过只表示历史结构门槛，不构成自动上线资格。", ""]
    (args.output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(args.output / "completion.json", {
        "status": "COMPLETE", "core_path_reproduced": True,
        "parameter_search_performed": False,
        "automatic_admission": False, "new_strategy_selected": False,
    })


if __name__ == "__main__":
    main()
