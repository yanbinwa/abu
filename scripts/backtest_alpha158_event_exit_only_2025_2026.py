#!/usr/bin/env python3
"""Freeze and replay the Alpha158 event-exit-only candidate for 2025-2026."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuArtifactManifest import (  # noqa: E402
    runtime_environment, sha256_file,
)
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview  # noqa: E402
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config  # noqa: E402
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from scripts.backtest_alpha158_lite_low_turnover_v3 import (  # noqa: E402
    annual_returns, run_low_turnover,
)
from scripts.backtest_alpha158_lite_v1 import rank_frame  # noqa: E402


DEFAULT_OUTPUT = Path(
    "/Users/wjy/abu/backtests/alpha158_event_exit_only_2025_2026_frozen_20261004")
DEFAULT_PREDICTIONS = Path(
    "/Users/wjy/abu/backtests/current_strategy_comparison_20261004_v2/"
    "inputs/alpha.csv.gz")
DEFAULT_REPORT = Path(
    "/Users/wjy/abu/backtests/current_strategy_comparison_20261004_v2/"
    "inputs/alpha_report.json")
DEFAULT_SIGNAL = Path("/Users/wjy/abu/shadow/alpha158_forward_v1/data/signal")
DEFAULT_RESEARCH = Path(
    "/Users/wjy/abu/shadow/alpha158_forward_v1/data/research")


def write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")


def file_hashes(paths):
    return {str(path.resolve()): sha256_file(path)
            for path in sorted(set(paths)) if path.is_file()}


def data_paths(signal_dir, research_dir):
    paths = list(Path(signal_dir).glob("sh*")) + list(Path(signal_dir).glob("sz*"))
    for name in ("raw", "signal_extra"):
        paths.extend((Path(research_dir) / name).glob("*"))
    paths.extend(Path(research_dir) / name for name in (
        "security_master.csv", "corporate_actions.csv",
        "industry_changes.csv", "sz_name_changes.csv"))
    return paths


def freeze(args):
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    runtime = output / "runtime"
    runtime.mkdir()
    for name in ("abupy", "scripts", "configs/selection"):
        shutil.copytree(
            ROOT / name, runtime / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    inputs = output / "frozen_inputs"
    inputs.mkdir()
    prediction_copy = inputs / "oos_predictions.csv.gz"
    report_copy = inputs / "research_report.json"
    shutil.copy2(args.predictions, prediction_copy)
    shutil.copy2(args.research_report, report_copy)

    config_names = (
        "alpha158_event_exit_only_research_v1.json",
        "alpha158_lite_low_turnover_v3.json",
        "alpha158_lite_v1.json", "risk_v1.json")
    config_hashes = file_hashes(
        [runtime / "configs/selection" / name for name in config_names])
    runtime_hashes = file_hashes(runtime.rglob("*"))
    market_hashes = file_hashes(data_paths(args.signal_dir, args.research_dir))
    registration = {
        "frozen_at": datetime.now().astimezone().isoformat(),
        "strategy_id": "alpha158_price_only_no_rank_exit_v1",
        "research_status": "frozen_historical_replay",
        "paper_admitted": False,
        "live_admitted": False,
        "start_date": args.start_date,
        "end_date": args.end_date,
        "initial_cash": args.initial_cash,
        "slippage_bps": 25.0,
        "fresh_cash_start": True,
        "ranking_exits_enabled": False,
        "event_exits_enabled": True,
        "position_add_enabled": False,
        "scale_out_enabled": False,
        "dynamic_stop_sync": False,
        "parameter_search": False,
        "observed_history": True,
        "new_holdout": False,
        "git_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "git_status": subprocess.check_output(
            ["git", "status", "--short"], cwd=ROOT, text=True).splitlines(),
        "environment": runtime_environment(),
        "runtime_files": runtime_hashes,
        "config_files": config_hashes,
        "input_snapshots": {
            "predictions": {"path": str(prediction_copy),
                            "sha256": sha256_file(prediction_copy)},
            "research_report": {"path": str(report_copy),
                                "sha256": sha256_file(report_copy)},
        },
        "market_data_files": market_hashes,
    }
    write_json(output / "registration.json", registration)
    os.execv(sys.executable, [
        sys.executable, "-B",
        str(runtime / "scripts" / Path(__file__).name),
        "--frozen", "--output-dir", str(output),
        "--predictions", str(prediction_copy),
        "--research-report", str(report_copy),
        "--signal-dir", str(args.signal_dir),
        "--research-dir", str(args.research_dir),
        "--start-date", str(args.start_date),
        "--end-date", str(args.end_date),
        "--initial-cash", str(args.initial_cash),
    ])


def frame_from_records(records):
    rows = [asdict(value) if is_dataclass(value) else value for value in records]
    return pd.DataFrame(rows)


def save_audit(directory, audit):
    directory.mkdir(parents=True, exist_ok=False)
    mapping = {
        "curve": "daily_nav.csv", "fills": "fills.csv",
        "decisions": "decisions.csv", "exits": "exit_reasons.csv",
        "selection": "selection_decisions.csv", "orders": "orders.csv",
        "reservations": "reservations.csv",
        "policy_evaluations": "policy_evaluations.csv",
        "add_proposals": "add_proposals.csv",
        "fill_allocations": "fill_allocations.csv",
        "position_lots": "position_lots.csv",
        "lot_dispositions": "lot_dispositions.csv",
        "logical_trades": "logical_trades.csv",
        "position_events": "position_events.csv",
        "risk_states_daily": "risk_states_daily.csv",
        "risk_positions_daily": "risk_positions_daily.csv",
        "entry_sizing_evaluations": "entry_sizing_evaluations.csv",
    }
    for key, filename in mapping.items():
        value = audit.get(key, [])
        frame = value if isinstance(value, pd.DataFrame) else frame_from_records(value)
        frame.to_csv(directory / filename, index=False)
    # The operation-level visualizer deliberately uses explicit physical and
    # logical names.  Keep aliases beside the canonical research audit files.
    shutil.copy2(directory / "fills.csv", directory / "physical_fills.csv")
    shutil.copy2(directory / "orders.csv", directory / "logical_orders.csv")


def validate_frozen_contract(payload, source, policy, risk):
    expected = {
        "strategy_version": "alpha158_price_only_no_rank_exit_v1",
        "review_overlay": "suppress_rank_exits",
        "target_positions": 10,
        "dynamic_stop_sync": False,
        "event_exits_enabled": True,
        "position_add_enabled": False,
        "scale_out_enabled": False,
        "default_slippage_bps": 25.0,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError("frozen strategy mismatch for {}".format(key))
    if source.sha256 != policy.source_config_sha256:
        raise ValueError("source and low-turnover configuration mismatch")
    if policy.target_positions != 10 or policy.review_interval_sessions != 5:
        raise ValueError("low-turnover policy no longer matches frozen rules")
    if policy.entry_rank_limit != 50 or policy.entry_persistence_reviews != 2:
        raise ValueError("entry ranking rule no longer matches frozen rules")
    if not policy.event_exits_enabled:
        raise ValueError("event exits must remain enabled")
    if source.label_slippage_bps != 25.0:
        raise ValueError("primary slippage must remain 25bp")
    if risk.single_trade_risk_fraction != .0025:
        raise ValueError("single-trade risk must remain 0.25%")


def run(args):
    output = args.output_dir.resolve()
    registration = json.loads((output / "registration.json").read_text())
    changed = [path for path, digest in registration["runtime_files"].items()
               if not Path(path).is_file() or sha256_file(path) != digest]
    changed += [value["path"] for value in
                registration["input_snapshots"].values()
                if sha256_file(value["path"]) != value["sha256"]]
    changed += [path for path, digest in registration["market_data_files"].items()
                if not Path(path).is_file() or sha256_file(path) != digest]
    if changed:
        raise ValueError("frozen inputs changed before replay: {}".format(changed[:5]))

    config_dir = ROOT / "configs/selection"
    experiment = json.loads((
        config_dir / "alpha158_event_exit_only_research_v1.json").read_text())
    source = load_alpha158_lite_config(config_dir / "alpha158_lite_v1.json")
    policy = load_alpha158_lite_low_turnover_config(
        config_dir / "alpha158_lite_low_turnover_v3.json")
    risk = load_risk_config(config_dir / "risk_v1.json")
    validate_frozen_contract(experiment, source, policy, risk)
    report = json.loads(args.research_report.read_text())
    if report["config_sha256"] != source.sha256:
        raise ValueError("prediction research report does not match source config")

    predictions = pd.read_csv(
        args.predictions,
        usecols=["signal_asof", "symbol", "column", policy.score_column,
                 "train_end"], dtype={"symbol": str})
    predictions = predictions[
        (predictions.signal_asof >= args.start_date) &
        (predictions.signal_asof <= args.end_date)].copy()
    if predictions.empty or not (predictions.train_end < predictions.signal_asof).all():
        raise ValueError("frozen replay requires non-empty OOS predictions")
    scores = rank_frame(
        predictions, policy.score_column,
        max(policy.entry_rank_limit, policy.retention_rank_limit))
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20200101,
        end_date=args.end_date)
    for row in predictions[["symbol", "column"]].drop_duplicates().itertuples():
        if panel.symbols[int(row.column)] != row.symbol:
            raise ValueError("prediction symbol-column mapping mismatch")

    result, audit = run_low_turnover(
        panel, scores, source, policy, risk, args.end_date,
        initial_cash=args.initial_cash,
        review_overlay=CostAwareReview(suppress_rank_exits=True))
    backtest = output / "backtest"
    save_audit(backtest, audit)
    annual = annual_returns(audit["curve"], initial_cash=args.initial_cash)
    annual.to_csv(backtest / "annual_returns.csv", index=False)
    write_json(backtest / "metrics.json", result)

    curve = audit["curve"]
    fills = audit["fills"]
    filled = fills[fills.status.eq("filled")]
    allocations = frame_from_records(audit["fill_allocations"])
    trades = frame_from_records(audit["logical_trades"])
    event_reasons = set(frame_from_records(audit["exits"]).get(
        "reason", pd.Series(dtype=str)).dropna().astype(str))
    forbidden_rank_exits = int((
        frame_from_records(audit["exits"]).get(
            "reason", pd.Series(dtype=str)) == "PERSISTENT_RANK_EXIT").sum())
    accounting_error = float(
        (curve.capital - curve.cash - curve.stocks).abs().max())
    operation_count = int(len(allocations))
    open_trades = int((trades.status == "ACTIVE").sum()) if len(trades) else 0
    verification = {
        "status": "PASSED",
        "fresh_start": int(result["start"]) >= args.start_date,
        "end_date": int(result["end"]),
        "accounting_max_abs_error": accounting_error,
        "cash_nonnegative": bool(curve.cash.min() >= -1e-7),
        "filled_buy_count": int(((filled.side == "buy")).sum()),
        "filled_sell_count": int(((filled.side == "sell")).sum()),
        "logical_trade_count": int(len(trades)),
        "open_trade_count": open_trades,
        "operation_count": operation_count,
        "rank_exit_count": forbidden_rank_exits,
        "event_exit_reasons": sorted(event_reasons),
        "all_allocations_have_trade_id": bool(
            len(allocations) and allocations.trade_id.astype(str).str.len().gt(0).all()),
        "inputs_unchanged_after_replay": True,
    }
    if accounting_error > 1e-6 or not verification["cash_nonnegative"]:
        raise AssertionError("accounting validation failed")
    if forbidden_rank_exits:
        raise AssertionError("ranking exit found in event-exit-only replay")
    if operation_count != len(filled):
        raise AssertionError("filled operation and allocation counts differ")
    changed_after = [path for path, digest in
                     registration["market_data_files"].items()
                     if sha256_file(path) != digest]
    if changed_after:
        verification["status"] = "INVALIDATED_INPUT_CHANGED"
        verification["inputs_unchanged_after_replay"] = False
        write_json(output / "verification.json", verification)
        raise ValueError("market inputs changed during replay")
    write_json(output / "verification.json", verification)
    write_json(output / "summary.json", {
        "strategy": "Alpha158 收益探索主线：取消排名退出",
        "period": [int(result["start"]), int(result["end"])],
        "metrics": result,
        "annual_returns": annual.to_dict("records"),
        "audit": verification,
        "limitations": [
            "2025-2026 data has already been observed and is not a new holdout",
            "2026 ends at the local data boundary 2026-09-30",
            "open positions are marked to market and are not force-liquidated",
        ],
    })
    print(json.dumps({"metrics": result, "verification": verification},
                     ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--predictions", type=Path, default=DEFAULT_PREDICTIONS)
    parser.add_argument("--research-report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--signal-dir", type=Path, default=DEFAULT_SIGNAL)
    parser.add_argument("--research-dir", type=Path, default=DEFAULT_RESEARCH)
    parser.add_argument("--start-date", type=int, default=20250101)
    parser.add_argument("--end-date", type=int, default=20260930)
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument("--frozen", action="store_true")
    args = parser.parse_args()
    if args.start_date >= args.end_date:
        raise ValueError("start-date must be earlier than end-date")
    if args.frozen:
        run(args)
    else:
        freeze(args)


if __name__ == "__main__":
    main()
