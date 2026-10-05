#!/usr/bin/env python3
# Research fork of 0b3c997; the default runner is deliberately untouched.
"""Run the frozen low-frequency Alpha158-lite portfolio diagnostic."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuAlpha158Lite import (  # noqa: E402
    Alpha158LiteExitEngine, Alpha158LiteFeatureEngine,
    Alpha158LiteLowTurnoverPolicy, load_alpha158_lite_config,
    load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuPortfolioExecutor import (  # noqa: E402
    ExecutionConfig, PortfolioExecutor,
)
from abupy.AlphaBu.ABuPortfolioRisk import (  # noqa: E402
    PortfolioRiskEngine, load_risk_config,
)
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2  # noqa: E402
from abupy.AlphaBu.ABuTradeIntent import TradeIntent, make_record_id  # noqa: E402
from abupy.AlphaBu.ABuTrialRegistry import (  # noqa: E402
    read_trial_registry, register_trial,
)
from scripts.backtest_alpha158_lite_v1 import (  # noqa: E402
    _save_audit, _seed_marks, fixed_path_cost_attribution, rank_frame,
)
from scripts.backtest_vcp_context_v1 import _trade_metrics  # noqa: E402


def _register_once(path, trial_id, hypothesis, configuration,
                   status="REGISTERED", observed_metrics=None):
    matches = [item for item in read_trial_registry(path)
               if item["trial_id"] == trial_id]
    if matches:
        if (matches[0]["hypothesis"] != hypothesis or
                matches[0]["configuration"] != configuration):
            raise ValueError("registered low-turnover trial differs")
        return matches[0]
    return register_trial(path, trial_id, hypothesis, configuration,
                          status=status, observed_metrics=observed_metrics)


def holding_session_statistics(fills, panel):
    if fills.empty:
        return {"median_holding_sessions": np.nan,
                "mean_holding_sessions": np.nan,
                "p90_holding_sessions": np.nan}
    buys = fills[(fills.side == "buy") & fills.status.eq("filled")]
    sells = fills[(fills.side == "sell") & fills.status.eq("filled")]
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    values = []
    for symbol, buy_group in buys.groupby("symbol"):
        sell_group = sells[sells.symbol.eq(symbol)].sort_values("date")
        for buy in buy_group.sort_values("date").itertuples():
            eligible = sell_group[sell_group.date >= buy.date]
            if eligible.empty:
                continue
            sell = eligible.iloc[0]
            sell_group = sell_group.drop(eligible.index[0])
            values.append(date_index[int(sell.date)]-date_index[int(buy.date)])
    if not values:
        return {"median_holding_sessions": np.nan,
                "mean_holding_sessions": np.nan,
                "p90_holding_sessions": np.nan}
    return {
        "median_holding_sessions": float(np.median(values)),
        "mean_holding_sessions": float(np.mean(values)),
        "p90_holding_sessions": float(np.quantile(values, .90)),
    }


def annual_returns(curve, initial_cash=1_000_000.0):
    rows = []
    previous = float(initial_cash)
    dated = curve.assign(year=curve.date.astype(int)//10000)
    for year, group in dated.groupby("year", sort=True):
        ending = float(group.capital.iloc[-1])
        rows.append({"year": int(year),
                     "return_pct": float((ending/previous-1)*100),
                     "end_capital": ending})
        previous = ending
    return pd.DataFrame(rows)


def run_low_turnover(panel, scores, source_config, policy_config, risk_config,
                     end_date, sync_dynamic_stops=False,
                     position_add_policy=None,
                     position_add_execution_mode="executable",
                     initial_cash=1_000_000.0, review_overlay=None,
                     scale_out_config=None, batch_allocator=None, batch_observer=None):
    grouped = {int(date): group.sort_values(
        ["daily_rank", "symbol"], kind="mergesort")
        for date, group in scores.groupby("signal_asof", sort=True)}
    date_index = {int(value): index for index, value in enumerate(panel.dates)}
    first_signal = int(scores.signal_asof.min())
    last_signal = min(int(scores.signal_asof.max()), int(end_date))
    first, last = date_index[first_signal], date_index[last_signal]
    strategy_config = replace(
        source_config, strategy_version=policy_config.strategy_version,
        entry_top_k=policy_config.target_positions,
        portfolio_score_depth=max(
            policy_config.entry_rank_limit,
            policy_config.retention_rank_limit))
    executor = PortfolioExecutor(panel, ExecutionConfig(
        initial_cash=float(initial_cash),
        slippage_bps=source_config.label_slippage_bps, mode="pit_corrected",
        max_positions=policy_config.target_positions))
    _seed_marks(executor, panel, first)
    risk = PortfolioRiskEngine(panel, risk_config)
    add_coordinator = None
    if position_add_policy is not None:
        from abupy.AlphaBu.ABuPositionAddResearch import PositionAddCoordinator
        add_coordinator = PositionAddCoordinator(
            panel, position_add_policy, risk,
            data_version="alpha158_position_add_v1",
            execution_mode=position_add_execution_mode)
    features = Alpha158LiteFeatureEngine(panel, strategy_config)
    exits = Alpha158LiteExitEngine(panel, strategy_config)
    scale_out = None
    if scale_out_config is not None:
        from abupy.AlphaBu.ABuScaleOutPolicy import RMultipleScaleOutPolicy
        scale_out = RMultipleScaleOutPolicy(scale_out_config)
    policy = Alpha158LiteLowTurnoverPolicy(policy_config)
    entry_intents, exit_intents = {}, {}
    decisions, exit_rows, selection_rows = [], [], []
    risk_state_rows, risk_position_rows = [], []
    pending_exits, pending_entries = [], []

    for day in range(first, last+1):
        signal_day = day-1
        if day > first:
            signal_date = int(panel.dates[signal_day])
            for symbol, reason, requested_quantity, position_effect in pending_exits:
                position = executor.positions.get(symbol)
                if position is None or any(
                        order.side == "sell" and order.symbol == symbol
                        for order in executor.orders):
                    continue
                entry = entry_intents[symbol]
                trades = executor.position_ledger.active_trades(symbol)
                trade = trades[0] if len(trades) == 1 else None
                quantity = (position.quantity if requested_quantity is None else
                            min(int(requested_quantity), position.quantity))
                sell = TradeIntent(
                    intent_id=make_record_id(
                        "alpha158-low-turnover-exit", signal_date,
                        symbol, reason),
                    strategy_id=entry.strategy_id, strategy_version="1",
                    signal_asof=signal_date, symbol=symbol, side="sell",
                    metadata={"exit_reason": reason},
                    trade_id=(trade.trade_id if trade is not None else ""),
                    allocation_id=(trade.allocation_id
                                   if trade is not None else "GLOBAL"),
                    position_effect=position_effect,
                )
                order, _ = executor.approve_order(
                    sell, quantity, int(panel.dates[day]))
                if order is None and position_effect == "REDUCE" and \
                        scale_out is not None:
                    scale_out.record_cancel(symbol)
                exit_intents[sell.intent_id] = sell
                exit_rows.append({"date": signal_date, "symbol": symbol,
                                  "reason": reason,
                                  "position_effect": position_effect,
                                  "quantity": quantity})
            held_or_ordered = set(executor.positions) | {
                order.symbol for order in executor.orders
                if order.side == "buy"}
            exiting = sum(symbol in executor.positions
                          for symbol, _, _, effect in pending_exits
                          if effect == "CLOSE")
            slots = max(
                0, policy_config.target_positions-len(executor.positions)+exiting)
            if batch_observer is not None:
                batch_observer.prepare(
                    pending_entries, slots, features, risk, executor,
                    signal_day, day, held_or_ordered, policy_config.score_column)
            if batch_allocator is not None:
                prepared = batch_allocator.prepare(
                    pending_entries, slots, features, risk, executor,
                    signal_day, day, held_or_ordered, policy_config.score_column)
            else:
                prepared = [(row, None, None) for row in pending_entries]
            for row, prepared_intent, requested_quantity in prepared:
                if slots <= 0:
                    break
                symbol = str(row["symbol"])
                if symbol in held_or_ordered:
                    continue
                intent = prepared_intent or features.make_intent(
                    signal_day, int(row["column"]),
                    float(row[policy_config.score_column]))
                if intent is None:
                    continue
                order, _, decision = risk.approve(
                    executor, intent, signal_day, day,
                    requested_quantity=requested_quantity)
                if batch_allocator is not None and batch_allocator.mode == "equal_risk":
                    actual_quantity = order.quantity if order is not None else 0
                    if actual_quantity != requested_quantity:
                        raise AssertionError("batch plan changed during final approval")
                decisions.append(decision)
                selection_rows.append({
                    "signal_asof": signal_date, "symbol": symbol,
                    "score": float(row[policy_config.score_column]),
                    "daily_rank": int(row["daily_rank"]),
                    "risk_decision": decision.decision,
                    "order_created": order is not None,
                })
                entry_intents[symbol] = intent
                held_or_ordered.add(symbol)
                if order is not None:
                    slots -= 1

        fills_today = executor.process_open(day)
        for fill in fills_today:
            if fill.status != "filled":
                if (fill.side == "sell" and fill.position_effect == "REDUCE" and
                        fill.status in ("rejected", "expired", "cancelled") and
                        scale_out is not None):
                    scale_out.record_cancel(fill.symbol)
                continue
            if fill.side == "sell":
                reason = str(exit_intents[fill.intent_id].metadata.get(
                    "exit_reason", ""))
                if fill.position_effect == "REDUCE":
                    if scale_out is not None:
                        scale_out.record_fill(fill.symbol, reason, fill.quantity)
                else:
                    exits.remove(fill.symbol)
                    entry_intents.pop(fill.symbol, None)
                    if scale_out is not None:
                        scale_out.remove(fill.symbol)
            else:
                if fill.position_effect == "INCREASE":
                    if scale_out is not None:
                        scale_out.register_add(fill.symbol, fill.quantity)
                    continue
                intent = entry_intents[fill.symbol]
                exits.register_entry(intent, fill, day)
                if scale_out is not None:
                    scale_out.register_entry(fill.symbol, fill.quantity)
        executor.process_close(day)

        pending_exits, pending_entries = [], []
        if day >= last:
            continue
        event_symbols = set()
        scale_out_candidates = []
        add_stop_overrides = {}
        for symbol in sorted(executor.positions):
            if any(order.side == "sell" and order.symbol == symbol
                   for order in executor.orders):
                continue
            reason = exits.signal(day, symbol)
            current_stop = (exits.current_stop_raw(day, symbol)
                            if sync_dynamic_stops or add_coordinator is not None
                            else None)
            if sync_dynamic_stops:
                if current_stop is not None:
                    executor.update_position_stop(symbol, current_stop)
            if add_coordinator is not None and current_stop is not None:
                add_stop_overrides[symbol] = current_stop
            if reason:
                event_symbols.add(symbol)
                pending_exits.append((symbol, reason, None, "CLOSE"))
            elif scale_out is not None:
                scale_out_candidates.append(symbol)
        if (day-first) % policy_config.review_interval_sessions == 0:
            daily = grouped.get(int(panel.dates[day]), pd.DataFrame())
            holding_sessions = {
                symbol: day-exits.states[symbol].entry_day+1
                for symbol in executor.positions if symbol in exits.states}
            rank_exits, entry_symbols = policy.review(
                daily, executor.positions, holding_sessions,
                blocked_exits=event_symbols)
            if review_overlay is not None:
                rank_exits, entry_symbols = review_overlay.filter_review(
                    panel, executor, day, rank_exits, entry_symbols)
            pending_exits.extend((symbol, "PERSISTENT_RANK_EXIT", None, "CLOSE")
                                 for symbol in rank_exits)
            lookup = {str(row.symbol): row._asdict()
                      for row in daily.itertuples(index=False)}
            pending_entries = [lookup[symbol] for symbol in entry_symbols
                               if symbol in lookup]
        full_exit_symbols = {
            symbol for symbol, _, _, effect in pending_exits if effect == "CLOSE"}
        for symbol in scale_out_candidates:
            if symbol in full_exit_symbols or symbol not in executor.positions:
                continue
            state = exits.states.get(symbol)
            if state is None:
                continue
            decision = scale_out.evaluate(
                symbol, float(panel.close[day, panel.symbol_index[symbol]]),
                state, executor.positions[symbol].quantity)
            if decision is not None:
                event_symbols.add(symbol)
                pending_exits.append((
                    symbol, decision["reason"], decision["quantity"],
                    decision["position_effect"]))
        if add_coordinator is not None:
            blocked_symbols = {symbol for symbol, _, _, _ in pending_exits}
            blocked_trades = {
                trade.trade_id
                for trade in executor.position_ledger.active_trades()
                if trade.symbol in blocked_symbols}
            add_results = add_coordinator.evaluate_active(
                executor, day, day+1, blocked_trades,
                add_stop_overrides)
            decisions.extend(item[3] for item in add_results
                             if item[3] is not None)

        risk_equity, risk_gross, open_risk, industries, same_day = \
            risk._state(executor, day)
        risk_state_rows.append({
            "date": int(panel.dates[day]), "equity": risk_equity,
            "gross_exposure_cash": risk_gross,
            "open_risk_cash": open_risk,
            "same_day_new_risk_cash": same_day,
            "industry_open_risk_cash": dict(industries),
        })
        for row in risk._portfolio(executor, day):
            if row.get("pending", False):
                continue
            risk_position_rows.append({
                "date": int(panel.dates[day]), **row})

    curve = executor.curve_frame()
    fills = executor.fills_frame()
    initial = executor.config.initial_cash
    capital = np.r_[initial, curve.capital.to_numpy()]
    liquidation_capital = np.r_[
        initial, curve.liquidation_nav_3_limits.to_numpy()]
    daily_return = capital[1:]/capital[:-1]-1
    cutoff = np.quantile(daily_return, .05)
    return_pct = float((capital[-1]/initial-1)*100)
    start_timestamp = pd.Timestamp(str(int(panel.dates[first])))
    end_timestamp = pd.Timestamp(str(int(panel.dates[last])))
    elapsed_years = max((end_timestamp-start_timestamp).days/365.25, 1/365.25)
    trades = _trade_metrics(fills)
    result = {
        "experiment": policy_config.strategy_version,
        "start": int(panel.dates[first]), "end": int(panel.dates[last]),
        "return_pct": return_pct,
        "benchmark_return_pct": float(
            (panel.benchmark_close[last]/panel.benchmark_close[first]-1)*100),
        "max_drawdown_pct": float(
            (capital/np.maximum.accumulate(capital)-1).min()*100),
        "liquidation_3_limits_return_pct": float(
            (liquidation_capital[-1]/initial-1)*100),
        "liquidation_3_limits_max_drawdown_pct": float(
            (liquidation_capital/np.maximum.accumulate(
                liquidation_capital)-1).min()*100),
        "daily_expected_shortfall_95_pct": float(
            daily_return[daily_return <= cutoff].mean()*100),
        "average_exposure_pct": float(curve.exposure.mean()*100),
        "filled_buys": int(((fills.side == "buy") &
                            fills.status.eq("filled")).sum()),
        "filled_sells": int(((fills.side == "sell") &
                             fills.status.eq("filled")).sum()),
        "buys_per_year": float(
            ((fills.side == "buy") & fills.status.eq("filled")).sum()/
            elapsed_years),
        "risk_rejected": sum(item.decision == "rejected" for item in decisions),
        "risk_reduced": sum(item.decision == "reduced" for item in decisions),
        "entry_clusters": int(fills[(fills.side == "buy") &
                                     fills.status.eq("filled")].date.nunique()),
        "open_positions_end": int(len(executor.positions)),
        "pending_orders_end": int(len(executor.orders)),
        "slippage_bps": float(source_config.label_slippage_bps),
        "dynamic_stop_sync": bool(sync_dynamic_stops),
        "scale_out_policy_id": (
            scale_out.config.policy_id if scale_out is not None else "unified_exit"),
        **trades,
        **holding_session_statistics(fills, panel),
    }
    result.update(fixed_path_cost_attribution(fills, return_pct, initial))
    audit = {
        "curve": curve, "fills": fills, "decisions": decisions,
        "exits": exit_rows, "selection": selection_rows,
        "orders": executor.order_history,
        "reservations": executor.reservation_history,
        "policy_evaluations": (
            list(add_coordinator.runner.evaluations.values())
            if add_coordinator is not None else []),
        "add_proposals": (
            list(add_coordinator.runner.proposals.values())
            if add_coordinator is not None else []),
        "fill_allocations": list(executor.position_ledger.fill_allocations),
        "position_lots": list(executor.position_ledger.lots.values()),
        "lot_dispositions": list(executor.position_ledger.lot_dispositions),
        "logical_trades": list(executor.position_ledger.logical_trades.values()),
        "risk_states_daily": risk_state_rows,
        "risk_positions_daily": risk_position_rows,
    }
    if batch_allocator is not None:
        audit["batch_plans"] = batch_allocator.plans
        audit["order_diagnostics"] = batch_allocator.order_diagnostics
    if batch_observer is not None:
        audit["observed_plans"] = batch_observer.plans
        audit["order_diagnostics"] = batch_observer.order_diagnostics
    return result, audit
