#!/usr/bin/env python3
"""Build signal-time matched ADD candidates from a shadow replay audit."""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2


def _bucket_age(value):
    return "0-5" if value <= 5 else "6-10" if value <= 10 else \
        "11-20" if value <= 20 else "21+"


def _bucket_r(value):
    return "LT0" if value < 0 else "0-1" if value < 1 else \
        "1-2" if value < 2 else "2+"


def _fees(config, quantity, price, side):
    gross = quantity*price
    return (max(gross*config.broker_rate, config.min_commission) +
            gross*config.transfer_rate +
            (gross*config.sell_stamp_rate if side == "sell" else 0.0))


def build(panel, shadow_dir):
    shadow_dir = Path(shadow_dir)
    evaluations = pd.read_csv(shadow_dir/"policy_evaluations.csv")
    trades = pd.read_csv(shadow_dir/"logical_trades.csv").set_index("trade_id")
    dispositions = pd.read_csv(shadow_dir/"lot_dispositions.csv")
    exits = dispositions.groupby("trade_id").fill_date.max().to_dict()
    proposals = pd.read_csv(shadow_dir/"add_proposals.csv")
    overlay_lots = pd.read_csv(shadow_dir/"overlay_add_lots.csv")
    overlay_closed = pd.read_csv(shadow_dir/"overlay_dispositions.csv")
    closed_lots = set(overlay_closed.overlay_lot_id.astype(str))
    closed_proposals = set(overlay_lots[
        overlay_lots.overlay_lot_id.astype(str).isin(closed_lots)
    ].proposal_id.astype(str))
    actual_ids = proposals[
        proposals.proposal_id.astype(str).isin(closed_proposals)
    ].evaluation_id.astype(str).tolist()
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    config = ExecutionConfig(slippage_bps=25.0)
    rows = []
    for evaluation in evaluations.itertuples(index=False):
        trade_id = str(evaluation.trade_id)
        if trade_id not in trades.index or trade_id not in exits:
            continue
        signal_day = date_index.get(int(evaluation.signal_asof))
        exit_day = date_index.get(int(exits[trade_id]))
        if signal_day is None or exit_day is None or signal_day+1 > exit_day:
            continue
        trade = trades.loc[trade_id]
        symbol = str(trade.symbol); column = panel.symbol_index[symbol]
        entry_day = signal_day+1
        if not panel.buy_tradable_mask[entry_day, column]:
            continue
        entry_ref = float(panel.exec_open[entry_day, column])
        exit_ref = float(panel.exec_open[exit_day, column])
        if not all(np.isfinite(value) and value > 0 for value in (entry_ref, exit_ref)):
            continue
        entry = entry_ref*(1+config.slippage_bps/10000.0)
        exit_price = exit_ref*(1-config.slippage_bps/10000.0)
        quantity = 100
        outcome = (quantity*(exit_price-entry)-_fees(config, quantity, entry, "buy")-
                   _fees(config, quantity, exit_price, "sell"))
        inputs = ast.literal_eval(str(evaluation.evaluated_inputs))
        age = signal_day-int(trade.entry_session_index)
        initial_entry = float(trade.initial_entry_price_adjusted)
        initial_r = float(trade.initial_r_per_share_adjusted)
        close = float(panel.close[signal_day, column])
        floating_r = ((close-initial_entry)/initial_r if initial_r > 0 else 0.0)
        amounts = np.asarray(panel.amount[max(0, signal_day-19):signal_day+1],
                             dtype=float)
        average = np.nanmean(amounts, axis=0)
        eligible = average[np.isfinite(average) & (average > 0)]
        if not len(eligible) or not np.isfinite(average[column]):
            continue
        low, high = np.quantile(eligible, [1/3, 2/3])
        liquidity = ("LOW" if average[column] <= low else
                     "MID" if average[column] <= high else "HIGH")
        industry = panel.industry[signal_day, column]
        industry = str(int(industry)) if np.isfinite(industry) else "UNKNOWN"
        market_state = ("UP" if np.isfinite(panel.market_ma200[signal_day]) and
                        panel.benchmark_close[signal_day] >=
                        panel.market_ma200[signal_day] else "DOWN")
        rows.append({
            "candidate_id": str(evaluation.evaluation_id),
            "signal_asof": int(evaluation.signal_asof),
            "industry_asof": industry, "market_state": market_state,
            "holding_age_bucket": _bucket_age(age),
            "floating_r_bucket": _bucket_r(floating_r),
            "liquidity_bucket": liquidity, "outcome_pnl_cash": outcome,
            "trade_id": trade_id, "symbol": symbol,
            "future_exit_date": int(exits[trade_id]),
        })
    return pd.DataFrame(rows), actual_ids


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--shadow-dir", type=Path, required=True)
    parser.add_argument("--signal-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--research-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/selection_research"))
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    panel = SelectionPanelV2.from_research_data(
        args.signal_dir, args.research_dir, start_date=20210101,
        end_date=20261231)
    candidates, actual_ids = build(panel, args.shadow_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(args.output_dir/"placebo_candidates.csv", index=False)
    usable = [item for item in actual_ids
              if item in set(candidates.candidate_id.astype(str))]
    (args.output_dir/"actual_ids.txt").write_text(
        "\n".join(usable)+"\n", encoding="utf-8")
    print("candidates", len(candidates), "actual", len(usable), flush=True)


if __name__ == "__main__":
    main()
