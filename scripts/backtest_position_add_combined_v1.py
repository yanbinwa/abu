#!/usr/bin/env python3
"""Conservative shared-account replay of frozen VCP and Alpha158 orders.

The replay combines already-approved historical logical signals. It applies one
cash/risk account and preserves strategy-scoped trade IDs. It deliberately does
not resurrect candidates rejected in either source run.
"""
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

from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor
from abupy.AlphaBu.ABuPortfolioRisk import PortfolioRiskEngine, load_risk_config
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuTradeIntent import TradeIntent, make_record_id


def _read_orders(path, source):
    frame = pd.read_csv(path)
    frame["source_strategy"] = source
    return frame


def run(panel, order_frames, risk_config):
    orders = pd.concat(order_frames, ignore_index=True).sort_values(
        ["valid_session", "side", "strategy_id", "symbol", "order_id"],
        kind="mergesort")
    executor = PortfolioExecutor(
        panel, ExecutionConfig(slippage_bps=25.0, mode="pit_corrected"))
    risk = PortfolioRiskEngine(panel, risk_config)
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    first = min(date_index[int(value)] for value in orders.valid_session)
    last = max(date_index[int(value)] for value in orders.valid_session)
    for column in range(len(panel.symbols)):
        history = panel.exec_close[:first, column]
        valid = history[np.isfinite(history) & (history > 0)]
        if len(valid):
            executor.last_close[column] = valid[-1]
    grouped = {int(date): frame for date, frame in orders.groupby("valid_session")}
    decisions = []
    for day in range(first, last+1):
        date = int(panel.dates[day])
        scheduled = grouped.get(date, pd.DataFrame())
        if len(scheduled):
            scheduled = scheduled.sort_values(
                ["side", "source_strategy", "symbol"], ascending=[False, True, True])
        for row in scheduled.itertuples(index=False):
            matching = [trade for trade in executor.position_ledger.active_trades(
                str(row.symbol)) if trade.selection_strategy_id == str(row.strategy_id)]
            if str(row.side) == "sell":
                if len(matching) != 1:
                    continue
                trade = matching[0]
                quantity = executor.position_ledger.quantity_for_trade(trade.trade_id)
                intent = TradeIntent(
                    intent_id="combined-"+str(row.intent_id),
                    strategy_id=str(row.strategy_id),
                    strategy_version=str(row.strategy_version),
                    signal_asof=int(row.created_asof), symbol=str(row.symbol),
                    side="sell", trade_id=trade.trade_id,
                    allocation_id="GLOBAL", position_effect="CLOSE",
                    metadata={"source_order_id": str(row.order_id)})
                executor.approve_order(intent, quantity, date)
                continue
            trade_id = make_record_id(
                "combined-trade", row.source_strategy, row.intent_id)
            intent = TradeIntent(
                intent_id="combined-"+str(row.intent_id),
                strategy_id=str(row.strategy_id),
                strategy_version=str(row.strategy_version),
                signal_asof=int(row.created_asof), symbol=str(row.symbol),
                side="buy", signal_price_raw=float(row.max_buy_price_raw)/1.03,
                initial_stop_adjusted=(float(row.initial_stop_adjusted)
                                       if pd.notna(row.initial_stop_adjusted) else None),
                initial_stop_raw=(float(row.initial_stop_raw)
                                  if pd.notna(row.initial_stop_raw) else None),
                trade_id=trade_id, allocation_id="GLOBAL", position_effect="OPEN",
                metadata={"max_buy_price_raw": float(row.max_buy_price_raw),
                          "source_order_id": str(row.order_id)})
            _, _, decision = risk.approve(
                executor, intent, max(0, day-1), day,
                requested_quantity=int(row.quantity))
            decisions.append(decision)
        executor.process_open(day)
        executor.process_close(day)
    curve = executor.curve_frame()
    return executor, risk, curve, decisions


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vcp-orders", type=Path, default=Path(
        "/Users/wjy/abu/backtests/vcp_dynamic_stop_sync_v1_baseline_20261004/"
        "h_residual_stop_trailing_stagnation_continuous/orders.csv"))
    parser.add_argument("--alpha-orders", type=Path, default=Path(
        "/Users/wjy/abu/backtests/alpha158_dynamic_stop_sync_v1_baseline_20261004/"
        "alpha158_lite_low_turnover_v3/orders.csv"))
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, default=Path(
        "/Users/wjy/abu/backtests/position_add_v1_combined_20261004"))
    args = parser.parse_args()
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20210101,
        end_date=20261231)
    executor, risk, curve, decisions = run(panel, [
        _read_orders(args.vcp_orders, "vcp"),
        _read_orders(args.alpha_orders, "alpha158")],
        load_risk_config(ROOT/"configs/selection/risk_v1.json"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    curve.to_csv(args.output_dir/"daily_nav.csv", index=False)
    executor.fills_frame().to_csv(args.output_dir/"physical_fills.csv", index=False)
    executor.fill_allocations_frame().to_csv(
        args.output_dir/"fill_allocations.csv", index=False)
    executor.position_lots_frame().to_csv(args.output_dir/"position_lots.csv", index=False)
    executor.lot_dispositions_frame().to_csv(
        args.output_dir/"lot_dispositions.csv", index=False)
    executor.logical_trades_frame().to_csv(
        args.output_dir/"logical_trades.csv", index=False)
    with (args.output_dir/"risk_decisions.jsonl").open("w") as output:
        for item in decisions:
            output.write(json.dumps(asdict(item), ensure_ascii=False)+"\n")
    capital = np.r_[executor.config.initial_cash, curve.capital.to_numpy()]
    summary = {
        "mode": "fixed_approved_signal_combined_replay",
        "return_pct": float((capital[-1]/capital[0]-1)*100),
        "max_drawdown_pct": float(
            (capital/np.maximum.accumulate(capital)-1).min()*100),
        "average_exposure_pct": float(curve.exposure.mean()*100),
        "filled_buys": int(((executor.fills_frame().side == "buy") &
                            executor.fills_frame().status.eq("filled")).sum()),
        "source_limitation": "does not resurrect source-run rejected candidates",
    }
    (args.output_dir/"summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
