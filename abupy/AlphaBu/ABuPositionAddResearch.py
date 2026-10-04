# -*- encoding: utf-8 -*-
"""Policy orchestration plus fixed-path overlay accounting."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

import numpy as np

from .ABuPositionAddPolicy import PositionAddContext, PositionAddPolicyRunner
from .ABuTradeIntent import TradeIntent, make_record_id


@dataclass(frozen=True)
class OverlayAddLot:
    overlay_lot_id: str
    trade_id: str
    proposal_id: str
    symbol: str
    quantity: int
    entry_date: int
    entry_price_raw: float
    entry_fees_cash: float
    risk_cash_frozen: float


@dataclass(frozen=True)
class OverlayDisposition:
    overlay_lot_id: str
    trade_id: str
    exit_date: int
    exit_price_raw: float
    exit_fees_cash: float
    realized_pnl_cash: float


class FixedPathOverlayBook(object):
    """Virtual ADD cashflows that cannot alter the base account or exits."""

    def __init__(self, execution_config):
        self.config = execution_config
        self.lots = {}
        self.dispositions = []

    def _fees(self, quantity, price, side):
        gross = quantity*price
        commission = max(gross*self.config.broker_rate,
                         self.config.min_commission)
        transfer = gross*self.config.transfer_rate
        stamp = gross*self.config.sell_stamp_rate if side == "sell" else 0.0
        return commission+transfer+stamp

    def open(self, proposal, quantity, reference_open_raw, current_stop_raw):
        if quantity <= 0 or quantity % 100:
            raise ValueError("overlay quantity must be a positive board lot")
        price = float(reference_open_raw)*(1+self.config.slippage_bps/10000.0)
        if price > proposal.max_buy_price_raw+1e-12:
            return None
        identity = make_record_id("overlay-lot", proposal.proposal_id)
        lot = OverlayAddLot(
            overlay_lot_id=identity, trade_id=proposal.trade_id,
            proposal_id=proposal.proposal_id, symbol=proposal.symbol,
            quantity=int(quantity), entry_date=proposal.valid_session,
            entry_price_raw=price,
            entry_fees_cash=self._fees(quantity, price, "buy"),
            risk_cash_frozen=max(0.0, price-current_stop_raw)*quantity,
        )
        self.lots[identity] = lot
        return lot

    def close_trade(self, trade_id, exit_date, reference_open_raw):
        rows = []
        for identity, lot in sorted(list(self.lots.items())):
            if lot.trade_id != trade_id:
                continue
            price = float(reference_open_raw)*(1-self.config.slippage_bps/10000.0)
            exit_fees = self._fees(lot.quantity, price, "sell")
            pnl = (lot.quantity*(price-lot.entry_price_raw)-
                   lot.entry_fees_cash-exit_fees)
            row = OverlayDisposition(
                overlay_lot_id=identity, trade_id=trade_id,
                exit_date=int(exit_date), exit_price_raw=price,
                exit_fees_cash=exit_fees, realized_pnl_cash=pnl)
            rows.append(row)
            self.dispositions.append(row)
            self.lots.pop(identity)
        return tuple(rows)


class PositionAddCoordinator(object):
    """Build PIT contexts and route triggered proposals through portfolio risk."""

    def __init__(self, panel, policy, risk_engine, data_version="position_add_v1",
                 execution_mode="executable"):
        if execution_mode not in ("executable", "shadow"):
            raise ValueError("unknown position-add execution mode")
        self.panel = panel
        self.runner = PositionAddPolicyRunner(policy)
        self.risk_engine = risk_engine
        self.data_version = data_version
        self.execution_mode = execution_mode
        self.intents = []
        self.risk_decisions = []
        self.shadow_triggered_trades = set()

    def _equity(self, executor, day):
        value = executor.cash
        for symbol, position in executor.positions.items():
            column = self.panel.symbol_index[symbol]
            mark = float(self.panel.exec_close[day, column])
            if not np.isfinite(mark):
                mark = float(executor.last_close[column])
            value += position.quantity*mark
        return value

    def build_context(self, executor, trade, day, next_day,
                      base_signal_status="HOLD", current_stop_override=None):
        if current_stop_override is not None:
            trade = replace(trade, current_stop_raw=float(current_stop_override))
        column = self.panel.symbol_index[trade.symbol]
        lots = tuple(executor.position_ledger.lots_for_trade(trade.trade_id))
        last_lot = max(lots, key=lambda item: (item.fill_date, item.lot_id))
        date_to_index = {int(value): index
                         for index, value in enumerate(self.panel.dates)}
        fill_index = date_to_index[last_lot.fill_date]
        fill_raw_close = float(self.panel.exec_close[fill_index, column])
        fill_adjusted_close = float(self.panel.close[fill_index, column])
        factor = (fill_adjusted_close/fill_raw_close
                  if np.isfinite(fill_raw_close) and fill_raw_close > 0 else np.nan)
        last_fill_adjusted = last_lot.fill_price_raw*factor
        physical = executor.position_ledger._refresh_physical(
            trade.symbol, int(self.panel.dates[day]))
        return PositionAddContext(
            signal_asof=int(self.panel.dates[day]),
            valid_session=int(self.panel.dates[next_day]),
            trade_snapshot=trade, physical_position_snapshot=physical,
            lot_snapshots=lots,
            pending_order_snapshots=tuple(
                order for order in executor.orders
                if order.target_trade_id == trade.trade_id),
            adjusted_market_window={
                "close_adjusted": float(self.panel.close[day, column]),
                "atr21_adjusted": float(self.panel.atr21[day, column]),
                "last_fill_price_adjusted": float(last_fill_adjusted),
                "prior20_high_adjusted": float(np.nanmax(
                    self.panel.high[max(0, day-20):day, column]))
                if day > 0 else np.nan,
            },
            raw_execution_snapshot={
                "close_raw": float(self.panel.exec_close[day, column])},
            portfolio_risk_snapshot={
                "portfolio_equity_asof": self._equity(executor, day),
                "session_index": int(day)},
            base_signal_status=base_signal_status,
            field_coverage={"adjusted": True, "raw": True},
            data_version=self.data_version,
        )

    def evaluate_active(self, executor, day, next_day, blocked_trade_ids=(),
                        stop_overrides_by_symbol=None):
        results = []
        blocked = set(blocked_trade_ids)
        stop_overrides_by_symbol = stop_overrides_by_symbol or {}
        for trade in executor.position_ledger.active_trades():
            if (self.execution_mode == "shadow" and
                    trade.trade_id in self.shadow_triggered_trades):
                continue
            status = "EXIT" if trade.trade_id in blocked else "HOLD"
            context = self.build_context(
                executor, trade, day, next_day, status,
                stop_overrides_by_symbol.get(trade.symbol))
            evaluation = self.runner.evaluate(context)
            order = reservation = decision = None
            if (evaluation.triggered and evaluation.proposal is not None and
                    self.execution_mode == "executable"):
                proposal = evaluation.proposal
                intent = TradeIntent(
                    intent_id=make_record_id("add-intent", proposal.proposal_id),
                    strategy_id=trade.selection_strategy_id,
                    strategy_version=trade.selection_strategy_version,
                    signal_asof=proposal.signal_asof, symbol=trade.symbol,
                    side="buy", signal_price_raw=float(
                        context.raw_execution_snapshot["close_raw"]),
                    initial_stop_raw=proposal.current_stop_raw_snapshot,
                    trade_id=trade.trade_id, allocation_id=trade.allocation_id,
                    position_effect="INCREASE",
                    source_policy_id=evaluation.policy_id,
                    source_policy_version=evaluation.policy_version,
                    policy_evaluation_id=evaluation.evaluation_id,
                    proposal_id=proposal.proposal_id,
                    logical_order_id=proposal.logical_order_id,
                    metadata={
                        "max_buy_price_raw": proposal.max_buy_price_raw,
                        "risk_budget_cash_cap": proposal.risk_budget_cash_cap,
                        "notional_cash_cap": proposal.notional_cash_cap,
                    })
                order, reservation, decision = self.risk_engine.approve(
                    executor, intent, day, next_day,
                    requested_quantity=proposal.quantity_cap_optional)
                self.intents.append(intent)
                self.risk_decisions.append(decision)
            elif (evaluation.triggered and evaluation.proposal is not None and
                  self.execution_mode == "shadow"):
                self.shadow_triggered_trades.add(trade.trade_id)
            results.append((evaluation, order, reservation, decision))
        return results

    def evaluations_frame(self):
        import pandas as pd
        return pd.DataFrame([asdict(item) for item in self.runner.evaluations.values()])

    def proposals_frame(self):
        import pandas as pd
        return pd.DataFrame([asdict(item) for item in self.runner.proposals.values()])
