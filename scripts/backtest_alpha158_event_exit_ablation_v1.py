#!/usr/bin/env python3
"""A0-A5 ablation for no-rank-exit Alpha158 position management."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, fields, replace
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuAlphaExitPolicy import (  # noqa: E402
    Alpha158ExitOverlayEngine, AlphaExitOverlayConfig,
)
from abupy.AlphaBu.ABuArtifactManifest import sha256_file  # noqa: E402
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuScaleOutPolicy import ScaleOutConfig  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.backtest_alpha158_event_exit_only_2025_2026 import (  # noqa: E402
    data_paths, save_audit, write_json,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402
from scripts.backtest_position_add_v1 import _policy  # noqa: E402
from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval  # noqa: E402


DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_event_exit_ablation_v1_20261004")
DEFAULT_BASELINE = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_event_exit_only_2025_2026_frozen_20261004/backtest")
DEFAULT_PREDICTIONS = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_event_exit_only_2025_2026_frozen_20261004/"
    "frozen_inputs/oos_predictions.csv.gz")
DEFAULT_REPORT = Path(
    "/Users/wjy/abu/backtests/"
    "alpha158_event_exit_only_2025_2026_frozen_20261004/"
    "frozen_inputs/research_report.json")
DEFAULT_SIGNAL = Path("/Users/wjy/abu/shadow/alpha158_forward_v1/data/signal")
DEFAULT_RESEARCH = Path(
    "/Users/wjy/abu/shadow/alpha158_forward_v1/data/research")


VARIANTS = {
    "A0_frozen_event_exit_only": "冻结对照：取消排名退出",
    "A1_breakeven_floor": "1R后成本保护止损",
    "A2_scale_out_2r_50": "2R卖出50%",
    "A3_protected_winner_add": "保护盈利加仓",
    "A4_combined": "成本保护＋2R减半＋保护盈利加仓",
    "A5_combined_rolling_stagnation": "A4＋滚动停滞退出",
}


def load_overlay(path):
    payload = json.loads(Path(path).read_text())
    expected = {item.name for item in fields(AlphaExitOverlayConfig)}
    if set(payload) != expected:
        raise ValueError("exit overlay configuration fields mismatch")
    return AlphaExitOverlayConfig(**payload)


def load_scale(path):
    payload = json.loads(Path(path).read_text())
    return ScaleOutConfig(
        policy_id=payload["policy_id"],
        trigger_r_multiples=tuple(payload["trigger_r_multiples"]),
        cumulative_exit_fractions=tuple(payload["cumulative_exit_fractions"]),
        lot_size=int(payload["lot_size"]))


def factory(overlay):
    return lambda panel, config: Alpha158ExitOverlayEngine(
        panel, config, overlay)


def golden_check(audit, baseline):
    expected_curve = pd.read_csv(baseline / "daily_nav.csv")
    pd.testing.assert_frame_equal(
        audit["curve"].reset_index(drop=True), expected_curve,
        check_dtype=False, rtol=1e-11, atol=1e-7)
    columns = [
        "date", "symbol", "side", "status", "quantity",
        "reference_price", "fill_price_raw", "commission", "transfer_fee",
        "stamp_tax", "slippage_cost", "position_effect"]
    expected_fills = pd.read_csv(baseline / "fills.csv")
    actual_business = audit["fills"][columns].reset_index(drop=True).copy()
    expected_business = expected_fills[columns].reset_index(drop=True).copy()
    for column in ("symbol", "side", "status", "position_effect"):
        actual_business[column] = actual_business[column].fillna("")
        expected_business[column] = expected_business[column].fillna("")
    pd.testing.assert_frame_equal(
        actual_business, expected_business,
        check_dtype=False, rtol=1e-11, atol=1e-7)


def operation_metrics(audit):
    fills = audit["fills"]
    filled = fills[fills.status.eq("filled")]
    effects = filled.position_effect.value_counts()
    events = audit.get("position_events", [])
    dividend = sum(float(item.cash_delta) for item in events
                   if getattr(item, "event_type", "") == "CASH_DIVIDEND")
    exits = pd.DataFrame(audit["exits"])
    return {
        "open_fills": int(effects.get("OPEN", 0)),
        "increase_fills": int(effects.get("INCREASE", 0)),
        "reduce_fills": int(effects.get("REDUCE", 0)),
        "close_fills": int(effects.get("CLOSE", 0)),
        "cash_dividends": dividend,
        "breakeven_or_trailing_exits": int(
            exits.reason.eq("TRAILING_STOP").sum()) if len(exits) else 0,
        "rolling_stagnation_exits": int(
            exits.reason.eq("ROLLING_STAGNATION").sum()) if len(exits) else 0,
        "initial_stop_exits": int(
            exits.reason.eq("INITIAL_STOP").sum()) if len(exits) else 0,
    }


def markdown_report(results, uncertainty, output):
    ordered = results.set_index("variant").loc[list(VARIANTS)].reset_index()
    lines = [
        "# Alpha158 收益探索主线持仓管理消融 v1", "",
        "本实验使用2025-01-02至2026-09-30同一批OOS信号、100万元初始资金、"
        "25bp单边滑点，并在所有实验中关闭排名退出。历史已经参与研究，结果不是新留出验证。",
        "", "|版本|规则|累计收益|最大回撤|平均仓位|三跌停压力收益|加仓|部分退出|滚动停滞退出|",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in ordered.itertuples():
        lines.append(
            "|{}|{}|{:+.2f}%|{:.2f}%|{:.2f}%|{:+.2f}%|{}|{}|{}|".format(
                row.variant, VARIANTS[row.variant], row.return_pct,
                row.max_drawdown_pct, row.average_exposure_pct,
                row.liquidation_3_limits_return_pct, row.increase_fills,
                row.reduce_fills, row.rolling_stagnation_exits))
    lines += ["", "## 相对A0", "",
              "|版本|收益增量|回撤变化|仓位变化|配对区块95%区间|正增量概率|",
              "|---|---:|---:|---:|---:|---:|"]
    base = ordered.iloc[0]
    intervals = uncertainty.set_index("variant")
    for row in ordered.iloc[1:].itertuples():
        interval = intervals.loc[row.variant]
        lines.append(
            "|{}|{:+.2f}pp|{:+.2f}pp|{:+.2f}pp|[{:+.2f}, {:+.2f}]pp|{:.1f}%|".format(
                row.variant, row.return_pct-base.return_pct,
                row.max_drawdown_pct-base.max_drawdown_pct,
                row.average_exposure_pct-base.average_exposure_pct,
                interval.ci95_low_pp, interval.ci95_high_pp,
                (1-interval.resampled_nonpositive_fraction)*100))
    lines += ["", "## 解释限制", "",
              "- A1—A5来自已观察交易的诊断，属于事后研究版本。",
              "- 只有增量置信区间下界大于零且压力风险不恶化，才具备替换冻结基线的统计条件。",
              "- 现金分红已进入组合净值，并单独导出 position_events.csv。",
              ""]
    (output / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--research-report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--signal-dir", type=Path, default=DEFAULT_SIGNAL)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--start-date", type=int, default=20250101)
    parser.add_argument("--end-date", type=int, default=20260930)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)

    config_dir = ROOT / "configs/selection"
    config_paths = [config_dir / name for name in (
        "alpha158_event_exit_only_research_v1.json",
        "alpha158_lite_low_turnover_v3.json", "alpha158_lite_v1.json",
        "risk_v1.json", "alpha158_exit_overlay_v1.json",
        "protected_winner_v1.json", "scale_out_2r_50_v1.json")]
    market = data_paths(args.signal_dir, args.research_dir)
    registration = {
        "created_at": datetime.now().astimezone().isoformat(),
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "start_date": args.start_date, "end_date": args.end_date,
        "initial_cash": 1_000_000.0, "slippage_bps": 25.0,
        "variants": VARIANTS, "parameter_search": False,
        "research_only": True, "new_holdout": False,
        "prediction_sha256": sha256_file(args.predictions),
        "research_report_sha256": sha256_file(args.research_report),
        "config_files": {str(path): sha256_file(path) for path in config_paths},
        "market_data_files": {str(path): sha256_file(path)
                              for path in market if path.is_file()},
    }
    write_json(args.output / "registration.json", registration)
    (args.output / "frozen_configs").mkdir()
    for path in config_paths:
        shutil.copy2(path, args.output / "frozen_configs" / path.name)

    source = load_alpha158_lite_config(config_dir / "alpha158_lite_v1.json")
    policy = load_alpha158_lite_low_turnover_config(
        config_dir / "alpha158_lite_low_turnover_v3.json")
    risk = load_risk_config(config_dir / "risk_v1.json")
    report = json.loads(args.research_report.read_text())
    if source.sha256 != policy.source_config_sha256 or \
            report["config_sha256"] != source.sha256:
        raise ValueError("prediction/configuration mismatch")
    predictions = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", policy.score_column,
                 "train_end"], dtype={"symbol": str})
    predictions = predictions[
        (predictions.signal_asof >= args.start_date) &
        (predictions.signal_asof <= args.end_date)].copy()
    if not (predictions.train_end < predictions.signal_asof).all():
        raise ValueError("non-OOS predictions are forbidden")
    scores = rank_frame(
        predictions, policy.score_column,
        max(policy.entry_rank_limit, policy.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir,
        start_date=20200101, end_date=args.end_date)
    overlay_all = load_overlay(config_dir / "alpha158_exit_overlay_v1.json")
    overlay_breakeven = replace(
        overlay_all, rolling_stagnation_enabled=False,
        policy_id="alpha158_breakeven_floor_v1")
    scale = load_scale(config_dir / "scale_out_2r_50_v1.json")

    rows, curves = [], {}
    for variant in VARIANTS:
        print("RUN {} {}".format(variant, VARIANTS[variant]), flush=True)
        options = {
            "review_overlay": CostAwareReview(suppress_rank_exits=True),
            "initial_cash": 1_000_000.0,
        }
        if variant in ("A1_breakeven_floor", "A4_combined"):
            options["exit_engine_factory"] = factory(overlay_breakeven)
        if variant == "A5_combined_rolling_stagnation":
            options["exit_engine_factory"] = factory(overlay_all)
        if variant in ("A2_scale_out_2r_50", "A4_combined",
                       "A5_combined_rolling_stagnation"):
            options["scale_out_config"] = scale
        if variant in ("A3_protected_winner_add", "A4_combined",
                       "A5_combined_rolling_stagnation"):
            options["position_add_policy"] = _policy("protected_winner")
            options["sync_dynamic_stops"] = True
        run_policy = replace(
            policy, strategy_version="alpha158_event_exit_{}".format(
                variant.lower()))
        result, audit = run_low_turnover(
            panel, scores, source, run_policy, risk, args.end_date, **options)
        if variant == "A0_frozen_event_exit_only":
            golden_check(audit, args.baseline)
        directory = args.output / variant
        save_audit(directory, audit)
        annual_returns(audit["curve"]).to_csv(
            directory / "annual_returns.csv", index=False)
        result.update(variant=variant, label=VARIANTS[variant],
                      **operation_metrics(audit))
        write_json(directory / "metrics.json", result)
        rows.append(result)
        curves[variant] = audit["curve"].capital.to_numpy(dtype=float)
        print(json.dumps({key: result[key] for key in (
            "return_pct", "max_drawdown_pct", "average_exposure_pct",
            "increase_fills", "reduce_fills", "rolling_stagnation_exits")},
            ensure_ascii=False), flush=True)

    results = pd.DataFrame(rows)
    results.to_csv(args.output / "results.csv", index=False)
    base = curves["A0_frozen_event_exit_only"]
    uncertainty_rows = []
    for variant in list(VARIANTS)[1:]:
        interval = paired_block_interval(base, curves[variant])
        uncertainty_rows.append({"variant": variant, **interval})
    uncertainty = pd.DataFrame(uncertainty_rows)
    uncertainty.to_csv(args.output / "paired_uncertainty.csv", index=False)
    markdown_report(results, uncertainty, args.output)

    changed = [path for path, digest in
               registration["market_data_files"].items()
               if not Path(path).is_file() or sha256_file(path) != digest]
    verification = {
        "status": "PASSED" if not changed else "INVALIDATED_INPUT_CHANGED",
        "a0_golden_curve_and_fills": True,
        "variants_completed": len(rows),
        "inputs_unchanged": not changed,
        "changed_inputs": changed,
    }
    write_json(args.output / "verification.json", verification)
    if changed:
        raise ValueError("market inputs changed during replay")
    print(json.dumps(verification, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
