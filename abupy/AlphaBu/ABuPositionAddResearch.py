# -*- encoding: utf-8 -*-
"""Policy orchestration plus fixed-path overlay accounting."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd

from .ABuPortfolioExecutor import PortfolioExecutor
from .ABuPriceLimit import can_buy_at_open
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


@dataclass(frozen=True)
class IsolatedSleeveLot:
    """One cash-backed ADD lot that cannot consume base-strategy cash."""

    lot_id: str
    trade_id: str
    proposal_id: str
    symbol: str
    quantity: int
    entry_date: int
    entry_price_raw: float
    entry_fees_cash: float
    entry_cost_cash: float
    risk_cash_frozen: float


@dataclass(frozen=True)
class IsolatedSleeveDisposition:
    lot_id: str
    trade_id: str
    exit_date: int
    exit_price_raw: float
    quantity: int
    exit_fees_cash: float
    dividend_cash: float
    realized_pnl_cash: float


def replay_isolated_add_sleeve(panel, proposals, base_dispositions,
                               execution_config, initial_cash,
                               start_date=None, end_date=None,
                               max_gross_exposure=0.80):
    """Replay fixed ADD signals through an independent, cash-backed sleeve.

    The base account is never mutated.  Sells are processed before buys, ADDs
    use the same next-open slippage and daily price-limit checks as the common
    executor, and every proposal remains valid for one session only.  Base
    trade exits determine ADD exits so this experiment isolates capital
    competition without changing the frozen signal or exit path.
    """
    initial_cash = float(initial_cash)
    if initial_cash <= 0:
        raise ValueError("isolated sleeve initial_cash must be positive")
    if not 0 < float(max_gross_exposure) <= 1:
        raise ValueError("max_gross_exposure must be in (0, 1]")

    dates = np.asarray(panel.dates, dtype=int)
    first = 0 if start_date is None else int(np.searchsorted(dates, int(start_date)))
    last = len(dates)-1 if end_date is None else int(
        np.searchsorted(dates, int(end_date), side="right")-1)
    if first < 0 or last < first or last >= len(dates):
        raise ValueError("invalid sleeve replay period")
    date_index = {int(value): index for index, value in enumerate(dates)}

    exit_by_trade = {}
    for item in base_dispositions:
        trade_id = item.trade_id if hasattr(item, "trade_id") else item["trade_id"]
        fill_date = item.fill_date if hasattr(item, "fill_date") else item["fill_date"]
        exit_by_trade[trade_id] = max(
            int(fill_date), exit_by_trade.get(trade_id, 0))
    proposals_by_date = {}
    for proposal in proposals:
        valid_session = int(proposal.valid_session)
        if valid_session in date_index and proposal.trade_id in exit_by_trade:
            proposals_by_date.setdefault(valid_session, []).append(proposal)
    for rows in proposals_by_date.values():
        rows.sort(key=lambda item: (-item.priority, item.symbol,
                                    item.proposal_id))

    helper = PortfolioExecutor(panel, replace(
        execution_config, initial_cash=initial_cash))
    cash = initial_cash
    active = {}
    entries, dispositions, rejections, curve = [], [], [], []
    dividends_by_lot = {}
    pending_cash = {}
    pending_stock = {}

    def fees(quantity, price, side):
        gross = quantity*price
        commission = max(gross*execution_config.broker_rate,
                         execution_config.min_commission)
        transfer = gross*execution_config.transfer_rate
        stamp = gross*execution_config.sell_stamp_rate if side == "sell" else 0.0
        return commission+transfer+stamp

    def mark_for(lot, day):
        column = panel.symbol_index[lot.symbol]
        close = float(panel.exec_close[day, column])
        if np.isfinite(close) and close > 0:
            return close
        previous = np.asarray(panel.exec_close[:day+1, column], dtype=float)
        valid = previous[np.isfinite(previous) & (previous > 0)]
        return float(valid[-1]) if len(valid) else 0.0

    for day in range(first, last+1):
        date = int(dates[day])
        cash += float(pending_cash.pop(day, 0.0))
        for lot_id, factor in pending_stock.pop(day, []):
            lot = active.get(lot_id)
            if lot is None:
                continue
            quantity = int(np.floor(lot.quantity*factor))
            active[lot_id] = replace(lot, quantity=quantity)

        # The common executor processes sells before buys at the open.
        for lot_id, lot in sorted(list(active.items())):
            if exit_by_trade.get(lot.trade_id) != date:
                continue
            column = panel.symbol_index[lot.symbol]
            opening = float(panel.exec_open[day, column])
            if not np.isfinite(opening) or opening <= 0:
                # The base disposition date is executable by construction;
                # retaining the lot is safer than inventing a price.
                continue
            price = opening*(1-execution_config.slippage_bps/10000.0)
            exit_fees = fees(lot.quantity, price, "sell")
            proceeds = lot.quantity*price-exit_fees
            cash += proceeds
            dividend = float(dividends_by_lot.pop(lot_id, 0.0))
            row = IsolatedSleeveDisposition(
                lot_id=lot_id, trade_id=lot.trade_id, exit_date=date,
                exit_price_raw=price, quantity=lot.quantity,
                exit_fees_cash=exit_fees, dividend_cash=dividend,
                realized_pnl_cash=(proceeds+dividend-lot.entry_cost_cash))
            dispositions.append(row)
            active.pop(lot_id)

        for proposal in proposals_by_date.get(date, ()):
            if exit_by_trade[proposal.trade_id] <= date:
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date,
                                   "reason": "BASE_EXIT_NOT_AFTER_ADD"})
                continue
            column = panel.symbol_index[proposal.symbol]
            opening = float(panel.exec_open[day, column])
            if (not panel.buy_tradable_mask[day, column] or
                    not np.isfinite(opening) or opening <= 0):
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date,
                                   "reason": "NOT_BUY_TRADABLE"})
                continue
            lower, upper, _, _, _, _ = helper._limits(day, column)
            if not can_buy_at_open(opening, upper):
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date,
                                   "reason": "OPEN_AT_LIMIT_UP"})
                continue
            price = opening*(1+execution_config.slippage_bps/10000.0)
            if price > proposal.max_buy_price_raw+1e-12:
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date,
                                   "reason": "ABOVE_MAX_BUY_PRICE"})
                continue
            per_share_risk = max(
                0.0, price-float(proposal.current_stop_raw_snapshot))
            risk_quantity = (int(proposal.risk_budget_cash_cap/
                                 per_share_risk/100)*100
                             if per_share_risk > 0 else 0)
            notional_quantity = int(
                proposal.notional_cash_cap/price/100)*100
            cash_quantity = int(
                max(0.0, cash-execution_config.min_commission)/price/100)*100
            holdings_open = sum(
                item.quantity*(float(panel.exec_open[day,
                    panel.symbol_index[item.symbol]])
                    if np.isfinite(panel.exec_open[
                        day, panel.symbol_index[item.symbol]])
                    else mark_for(item, day))
                for item in active.values())
            equity_open = cash+holdings_open
            gross_headroom = max(
                0.0, equity_open*max_gross_exposure-holdings_open)
            gross_quantity = int(gross_headroom/price/100)*100
            caps = [risk_quantity, notional_quantity,
                    cash_quantity, gross_quantity]
            if proposal.quantity_cap_optional is not None:
                caps.append(int(proposal.quantity_cap_optional)//100*100)
            quantity = min(caps)
            if quantity < 100:
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date,
                                   "reason": "SLEEVE_CAPACITY_LT_BOARD_LOT"})
                continue
            entry_fees = fees(quantity, price, "buy")
            cost = quantity*price+entry_fees
            while quantity >= 100 and cost > cash+1e-9:
                quantity -= 100
                entry_fees = fees(quantity, price, "buy") if quantity else 0.0
                cost = quantity*price+entry_fees
            if quantity < 100:
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date,
                                   "reason": "INSUFFICIENT_SLEEVE_CASH"})
                continue
            lot_id = make_record_id("isolated-sleeve-lot",
                                    proposal.proposal_id)
            lot = IsolatedSleeveLot(
                lot_id=lot_id, trade_id=proposal.trade_id,
                proposal_id=proposal.proposal_id, symbol=proposal.symbol,
                quantity=quantity, entry_date=date, entry_price_raw=price,
                entry_fees_cash=entry_fees, entry_cost_cash=cost,
                risk_cash_frozen=quantity*per_share_risk)
            active[lot_id] = lot
            entries.append(lot)
            cash -= cost

        # Match the common executor's close-time corporate-action scheduling.
        for event in panel.corporate_actions.get(day, []):
            symbol = panel.symbols[event["symbol"]]
            for lot_id, lot in tuple(active.items()):
                if lot.symbol != symbol:
                    continue
                if event["cash_per_share"] and event["cash_day"] is not None:
                    amount = lot.quantity*float(event["cash_per_share"])
                    pending_cash[event["cash_day"]] = (
                        pending_cash.get(event["cash_day"], 0.0)+amount)
                    dividends_by_lot[lot_id] = (
                        dividends_by_lot.get(lot_id, 0.0)+amount)
                if event["stock_per_share"] and event["stock_day"] is not None:
                    pending_stock.setdefault(event["stock_day"], []).append(
                        (lot_id, 1.0+float(event["stock_per_share"])))

        stocks = sum(lot.quantity*mark_for(lot, day)
                     for lot in active.values())
        capital = cash+stocks
        curve.append({"date": date, "cash": cash, "stocks": stocks,
                      "capital": capital,
                      "exposure": stocks/capital if capital > 0 else np.nan,
                      "holdings": len(active)})

    return {
        "curve": pd.DataFrame(curve),
        "entries": entries,
        "dispositions": dispositions,
        "rejections": pd.DataFrame(rejections),
        "open_lots": tuple(active.values()),
    }


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
                "benchmark_close": float(self.panel.benchmark_close[day]),
                "benchmark_ma200": float(self.panel.market_ma200[day]),
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
