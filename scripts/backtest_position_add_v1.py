#!/usr/bin/env python3
"""Run frozen position-add policies in shadow overlay or executable mode."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import load_alpha158_lite_config
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config
from abupy.AlphaBu.ABuPositionAddAnalytics import build_trade_attribution
from abupy.AlphaBu.ABuPositionAddPolicy import (
    CompositePositionAddPolicy, MarketTrendGatePolicy, NoAddPolicy,
    ProtectedWinnerPolicy, RebreakoutPolicy, TurtleAtrPolicy,
    load_market_trend_gate_config, load_no_add_config,
    load_protected_winner_config, load_rebreakout_config,
    load_turtle_atr_config,
)
from abupy.AlphaBu.ABuPositionAddResearch import FixedPathOverlayBook
from abupy.AlphaBu.ABuScaleOutPolicy import ScaleOutConfig
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuVCPStrategy import (
    load_vcp_attention_config, load_vcp_core_config, load_vcp_residual_config,
    run_vcp_backtest,
)
from scripts.backtest_alpha158_lite_low_turnover_v3 import (
    load_alpha158_lite_low_turnover_config, rank_frame, run_low_turnover,
)


def _policy(name):
    if name == "no_add":
        return NoAddPolicy(load_no_add_config(
            ROOT/"configs/selection/no_add_v1.json"))
    protected = ProtectedWinnerPolicy(load_protected_winner_config(
        ROOT/"configs/selection/protected_winner_v1.json"))
    if name == "protected_winner":
        return protected
    rebreakout = RebreakoutPolicy(load_rebreakout_config(
        ROOT/"configs/selection/rebreakout_v1.json"))
    if name == "rebreakout":
        return rebreakout
    turtle = TurtleAtrPolicy(load_turtle_atr_config(
        ROOT/"configs/selection/turtle_atr_add_v1.json"))
    if name == "turtle_atr":
        return turtle
    if name == "turtle_atr_market_gate":
        return MarketTrendGatePolicy(
            turtle, load_market_trend_gate_config(
                ROOT/"configs/selection/market_trend_add_gate_v1.json"))
    return CompositePositionAddPolicy(
        (protected, rebreakout), mode="ALL_OF",
        policy_id="protected_winner_rebreakout_v1")


def _metrics(curve, fills, initial=1_000_000.0):
    capital = np.r_[initial, curve.capital.to_numpy(dtype=float)]
    return {
        "return_pct": float((capital[-1]/initial-1)*100),
        "max_drawdown_pct": float(
            (capital/np.maximum.accumulate(capital)-1).min()*100),
        "average_exposure_pct": float(curve.exposure.mean()*100),
        "filled_buys": int(((fills.side == "buy") &
                            fills.status.eq("filled")).sum()),
        "filled_adds": int(((fills.side == "buy") &
                            fills.status.eq("filled") &
                            fills.position_effect.eq("INCREASE")).sum())
            if "position_effect" in fills else 0,
        "filled_reduces": int(((fills.side == "sell") &
                               fills.status.eq("filled") &
                               fills.position_effect.eq("REDUCE")).sum())
            if "position_effect" in fills else 0,
        "filled_closes": int(((fills.side == "sell") &
                              fills.status.eq("filled") &
                              fills.position_effect.eq("CLOSE")).sum())
            if "position_effect" in fills else 0,
    }


def _write_audit(directory, result, curve, fills, audit):
    directory.mkdir(parents=True, exist_ok=True)
    curve.to_csv(directory/"daily_nav.csv", index=False)
    fills.to_csv(directory/"physical_fills.csv", index=False)
    mapping = {
        "policy_evaluations": "policy_evaluations.csv",
        "add_proposals": "add_proposals.csv",
        "orders": "logical_orders.csv",
        "fill_allocations": "fill_allocations.csv",
        "position_lots": "position_lots.csv",
        "lot_dispositions": "lot_dispositions.csv",
        "logical_trades": "logical_trades.csv",
        "exits": "exit_reasons.csv",
        "risk_states_daily": "risk_states_daily.csv",
        "risk_positions_daily": "risk_positions_daily.csv",
    }
    for key, name in mapping.items():
        records = audit.get(key, [])
        pd.DataFrame([asdict(item) if hasattr(item, "__dataclass_fields__") else item
                      for item in records]).to_csv(directory/name, index=False)
    facts, summaries = build_trade_attribution(
        audit.get("logical_trades", []), audit.get("position_lots", []),
        audit.get("lot_dispositions", []))
    facts.to_csv(directory/"trade_attribution.csv", index=False)
    for level, frame in summaries.items():
        frame.to_csv(directory/("attribution_{}.csv".format(level)), index=False)
    (directory/"summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def _materialize_overlay(panel, audit, directory):
    """Replay virtual ADDs on frozen base exits without touching base cash."""
    proposals = audit.get("add_proposals", [])
    dispositions = audit.get("lot_dispositions", [])
    exits = {}
    for item in dispositions:
        exits[item.trade_id] = max(exits.get(item.trade_id, 0), item.fill_date)
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    book = FixedPathOverlayBook(ExecutionConfig(slippage_bps=25.0))
    entries = []
    analyses = []
    for proposal in proposals:
        day = date_index.get(int(proposal.valid_session))
        if day is None:
            continue
        column = panel.symbol_index[proposal.symbol]
        per_share_risk = max(
            0.0, proposal.max_buy_price_raw-proposal.current_stop_raw_snapshot)
        risk_quantity = (int(proposal.risk_budget_cash_cap/per_share_risk/100)*100
                         if per_share_risk > 0 else 0)
        notional_quantity = int(
            proposal.notional_cash_cap/proposal.max_buy_price_raw/100)*100
        caps = [risk_quantity, notional_quantity]
        if proposal.quantity_cap_optional is not None:
            caps.append(int(proposal.quantity_cap_optional)//100*100)
        quantity = min(caps)
        if quantity < 100:
            continue
        lot = book.open(proposal, quantity, float(panel.exec_open[day, column]),
                        proposal.current_stop_raw_snapshot)
        if lot is None:
            continue
        entries.append(lot)
        exit_date = exits.get(proposal.trade_id)
        if exit_date is None:
            continue
        exit_day = date_index[exit_date]
        high = np.asarray(panel.exec_high[day:exit_day+1, column], dtype=float)
        low = np.asarray(panel.exec_low[day:exit_day+1, column], dtype=float)
        analyses.append({
            "overlay_lot_id": lot.overlay_lot_id,
            "trade_id": lot.trade_id,
            "mfe_pct": float((np.nanmax(high)/lot.entry_price_raw-1)*100),
            "mae_pct": float((np.nanmin(low)/lot.entry_price_raw-1)*100),
        })
        book.close_trade(proposal.trade_id, exit_date,
                         float(panel.exec_open[exit_day, column]))
    entry_rows = [asdict(item) for item in entries]
    exit_rows = [asdict(item) for item in book.dispositions]
    open_rows = []
    unrealized = 0.0
    for lot in book.lots.values():
        column = panel.symbol_index[lot.symbol]
        mark = float(panel.exec_close[-1, column])
        exit_fees = book._fees(lot.quantity, mark, "sell")
        pnl = (lot.quantity*(mark-lot.entry_price_raw)-lot.entry_fees_cash-
               exit_fees)
        open_rows.append({**asdict(lot), "mark_price_raw": mark,
                          "unrealized_pnl_cash": pnl})
        unrealized += pnl
    pd.DataFrame(entry_rows).to_csv(directory/"overlay_add_lots.csv", index=False)
    pd.DataFrame(exit_rows).to_csv(directory/"overlay_dispositions.csv", index=False)
    pd.DataFrame(analyses).to_csv(directory/"overlay_path_metrics.csv", index=False)
    pd.DataFrame(open_rows).to_csv(directory/"overlay_open_lots.csv", index=False)
    return {
        "overlay_filled_adds": len(entry_rows),
        "overlay_closed_adds": len(exit_rows),
        "overlay_incremental_pnl_cash": float(sum(
            item.realized_pnl_cash for item in book.dispositions)),
        "overlay_open_adds": len(open_rows),
        "overlay_unrealized_pnl_cash": float(unrealized),
    }


def run_vcp(panel, policy, mode, output, scale_out_config=None):
    risk = load_risk_config(ROOT/"configs/selection/risk_v1.json")
    audit = {}
    result, curve, fills, _, _ = run_vcp_backtest(
        panel, None, "h_residual_stop_trailing_stagnation", 25.0,
        load_vcp_core_config(ROOT/"configs/selection/vcp_core_v1.json"),
        load_vcp_attention_config(ROOT/"configs/selection/vcp_attention_v1.json"),
        risk,
        load_vcp_residual_config(ROOT/"configs/selection/vcp_residual_v2.json"),
        start_date=20220101, end_date=20261231, audit=audit,
        sync_dynamic_stops=(mode == "executable"), position_add_policy=policy,
        position_add_execution_mode=mode,
        scale_out_config=scale_out_config)
    result.update(_metrics(curve, fills))
    _write_audit(output, result, curve, fills, audit)
    if mode == "shadow":
        result.update(_materialize_overlay(panel, audit, output))
        (output/"summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)+"\n",
            encoding="utf-8")
    return result


def run_alpha(panel, policy, mode, output, predictions, scale_out_config=None):
    source = load_alpha158_lite_config(
        ROOT/"configs/selection/alpha158_lite_v1.json")
    low = load_alpha158_lite_low_turnover_config(
        ROOT/"configs/selection/alpha158_lite_low_turnover_v3.json")
    scores_raw = pd.read_csv(
        predictions, usecols=["signal_asof", "symbol", "column", low.score_column],
        dtype={"symbol": str})
    scores = rank_frame(scores_raw, low.score_column,
                        max(low.entry_rank_limit, low.retention_rank_limit))
    result, audit = run_low_turnover(
        panel, scores, source, low,
        load_risk_config(ROOT/"configs/selection/risk_v1.json"), 20260930,
        sync_dynamic_stops=(mode == "executable"), position_add_policy=policy,
        position_add_execution_mode=mode,
        scale_out_config=scale_out_config)
    result.update(_metrics(audit["curve"], audit["fills"]))
    _write_audit(output, result, audit["curve"], audit["fills"], audit)
    if mode == "shadow":
        result.update(_materialize_overlay(panel, audit, output))
        (output/"summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)+"\n",
            encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy", choices=("vcp", "alpha158", "all"),
                        default="all")
    parser.add_argument("--policy", choices=(
        "no_add", "protected_winner", "rebreakout", "turtle_atr",
        "turtle_atr_market_gate",
        "protected_rebreakout_all_of"),
                        default="protected_winner")
    parser.add_argument("--mode", choices=("shadow", "executable"),
                        default="executable")
    parser.add_argument("--exit-mode", choices=(
        "unified", "scale_out_1r", "scale_out_2r", "scale_out_2r_50",
        "scale_out_1r_2r"),
                        default="unified")
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--predictions", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_lite_v1_hardened_20261003/"
        "oos_predictions.csv.gz"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/position_add_v1"))
    args = parser.parse_args()
    policy = _policy(args.policy)
    scale_configs = {
        "scale_out_1r": ScaleOutConfig(
            policy_id="scale_out_1r_v1", trigger_r_multiples=(1.0,),
            cumulative_exit_fractions=(0.25,)),
        "scale_out_2r": ScaleOutConfig(
            policy_id="scale_out_2r_v1", trigger_r_multiples=(2.0,),
            cumulative_exit_fractions=(0.25,)),
        "scale_out_2r_50": ScaleOutConfig(
            policy_id="scale_out_2r_50_v1", trigger_r_multiples=(2.0,),
            cumulative_exit_fractions=(0.50,)),
        "scale_out_1r_2r": ScaleOutConfig(),
    }
    scale_out_config = scale_configs.get(args.exit_mode)
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20210101,
        end_date=20261231)
    results = []
    if args.strategy in ("vcp", "all"):
        result = run_vcp(
            panel, policy, args.mode, args.output_dir/"vcp", scale_out_config)
        results.append({"strategy": "vcp", **result})
        print("vcp", result["return_pct"], result["filled_adds"], flush=True)
    if args.strategy in ("alpha158", "all"):
        result = run_alpha(panel, policy, args.mode, args.output_dir/"alpha158",
                           args.predictions, scale_out_config)
        results.append({"strategy": "alpha158", **result})
        print("alpha158", result["return_pct"], result["filled_adds"], flush=True)
    pd.DataFrame(results).to_csv(args.output_dir/"strategy_summary.csv", index=False)


if __name__ == "__main__":
    main()
