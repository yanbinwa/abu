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
    exit_reason: str = "BASE_EXIT"


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


def replay_residual_add_overlay(panel, proposals, base_dispositions,
                                base_curve, base_orders,
                                base_risk_positions, execution_config,
                                risk_engine, start_date=None, end_date=None):
    """Replay fixed-quantity ADD orders behind a frozen base portfolio.

    Base orders always have priority.  ADD approval uses only close-time cash
    remaining after the next-session base reservations and the portfolio's
    residual gross, symbol, industry, open-risk, capacity and stress budgets.
    Quantity is frozen with the proposal's maximum buy price before the next
    open is observed.  The returned combined curve preserves the base path.
    """
    dates = np.asarray(panel.dates, dtype=int)
    date_index = {int(value): index for index, value in enumerate(dates)}
    base_curve = pd.DataFrame(base_curve).set_index("date", drop=False)
    first_date = int(base_curve.date.min()) if start_date is None else int(start_date)
    last_date = int(base_curve.date.max()) if end_date is None else int(end_date)
    first, last = date_index[first_date], date_index[last_date]
    config = risk_engine.config
    helper = PortfolioExecutor(panel, replace(
        execution_config, initial_cash=float(base_curve.iloc[0].capital)))

    def value(item, name):
        return getattr(item, name) if hasattr(item, name) else item[name]

    def fees(quantity, price, side):
        gross = quantity*price
        return (max(gross*execution_config.broker_rate,
                    execution_config.min_commission) +
                gross*execution_config.transfer_rate +
                (gross*execution_config.sell_stamp_rate
                 if side == "sell" else 0.0))

    exits = {}
    for item in base_dispositions:
        exits[value(item, "trade_id")] = max(
            int(value(item, "fill_date")),
            exits.get(value(item, "trade_id"), 0))
    proposals_by_signal = {}
    for proposal in proposals:
        if proposal.trade_id in exits:
            proposals_by_signal.setdefault(int(proposal.signal_asof), []).append(
                proposal)
    for rows in proposals_by_signal.values():
        rows.sort(key=lambda item: (-item.priority, item.symbol,
                                    item.proposal_id))

    base_reservations = {}
    base_new_risk = {}
    for order in base_orders:
        if value(order, "side") != "buy" or value(order, "position_effect") == "INCREASE":
            continue
        created = int(value(order, "created_asof"))
        price = float(value(order, "max_buy_price_raw"))
        quantity = int(value(order, "quantity"))
        base_reservations[created] = base_reservations.get(created, 0.0) + \
            quantity*price+fees(quantity, price, "buy")
        base_new_risk[created] = base_new_risk.get(created, 0.0) + float(
            value(order, "planned_initial_r_cash"))

    positions_by_date = {}
    for row in base_risk_positions:
        record = dict(row) if isinstance(row, dict) else asdict(row)
        positions_by_date.setdefault(int(record.pop("date")), []).append(record)

    overlay_cash_delta = 0.0
    active = {}
    approved_by_date = {}
    entries, dispositions, approvals, rejections, curve = [], [], [], [], []
    pending_cash, pending_stock, dividends_by_lot = {}, {}, {}

    def mark(lot, day):
        column = panel.symbol_index[lot.symbol]
        close = float(panel.exec_close[day, column])
        if np.isfinite(close) and close > 0:
            return close
        prior = np.asarray(panel.exec_close[:day+1, column], dtype=float)
        valid = prior[np.isfinite(prior) & (prior > 0)]
        return float(valid[-1]) if len(valid) else 0.0

    def add_rows(day):
        rows = []
        for lot in active.values():
            column = panel.symbol_index[lot.symbol]
            price = mark(lot, day)
            initial_r_per_share = (lot.risk_cash_frozen/lot.quantity
                                   if lot.quantity else 0.0)
            stop = lot.entry_price_raw-initial_r_per_share
            rows.append({
                "symbol": lot.symbol, "trade_id": lot.trade_id,
                "quantity": lot.quantity,
                "market_value": lot.quantity*price,
                "industry": risk_engine._industry(day, column),
                "beta": risk_engine.beta_for(day, column),
                "open_risk": max(0.0, price-stop)*lot.quantity,
                "initial_r_cash": lot.risk_cash_frozen,
                "has_stop": True,
                "limit_fraction": risk_engine._limit_fraction(day, column),
                "pending": False, "is_add": True,
                "latest_fill_date": lot.entry_date,
            })
        return rows

    def close_lot(lot_id, day, reason):
        nonlocal overlay_cash_delta
        lot = active.get(lot_id)
        if lot is None:
            return
        column = panel.symbol_index[lot.symbol]
        opening = float(panel.exec_open[day, column])
        if not np.isfinite(opening) or opening <= 0:
            return
        price = opening*(1-execution_config.slippage_bps/10000.0)
        exit_fees = fees(lot.quantity, price, "sell")
        proceeds = lot.quantity*price-exit_fees
        overlay_cash_delta += proceeds
        dividend = float(dividends_by_lot.pop(lot_id, 0.0))
        dispositions.append(IsolatedSleeveDisposition(
            lot_id=lot_id, trade_id=lot.trade_id,
            exit_date=int(dates[day]), exit_price_raw=price,
            quantity=lot.quantity, exit_fees_cash=exit_fees,
            dividend_cash=dividend,
            realized_pnl_cash=proceeds+dividend-lot.entry_cost_cash,
            exit_reason=reason))
        active.pop(lot_id)

    for day in range(first, last+1):
        date = int(dates[day])
        overlay_cash_delta += float(pending_cash.pop(day, 0.0))
        for lot_id, factor in pending_stock.pop(day, []):
            lot = active.get(lot_id)
            if lot is not None:
                active[lot_id] = replace(
                    lot, quantity=int(np.floor(lot.quantity*factor)))

        base = base_curve.loc[date]
        # If a base order consumed more cash than its conservative reservation,
        # release ADD capital first. Latest ADD lots leave first.
        for lot_id, _ in sorted(
                active.items(), key=lambda item: (-item[1].entry_date,
                                                  item[0])):
            if float(base.cash)+overlay_cash_delta >= -1e-9:
                break
            close_lot(lot_id, day, "BASE_PRIORITY_CASH_RELEASE")

        for lot_id, lot in sorted(list(active.items())):
            if exits.get(lot.trade_id) == date:
                close_lot(lot_id, day, "BASE_EXIT")

        for item in approved_by_date.get(date, ()):
            proposal, quantity = item["proposal"], int(item["quantity"])
            column = panel.symbol_index[proposal.symbol]
            opening = float(panel.exec_open[day, column])
            reason = None
            if (not panel.buy_tradable_mask[day, column] or
                    not np.isfinite(opening) or opening <= 0):
                reason = "NOT_BUY_TRADABLE"
            else:
                _, upper, _, _, _, _ = helper._limits(day, column)
                if not can_buy_at_open(opening, upper):
                    reason = "OPEN_AT_LIMIT_UP"
            price = opening*(1+execution_config.slippage_bps/10000.0)
            if reason is None and price > proposal.max_buy_price_raw+1e-12:
                reason = "ABOVE_MAX_BUY_PRICE"
            cost = quantity*price+fees(quantity, price, "buy")
            if reason is None and cost > float(base.cash)+overlay_cash_delta+1e-9:
                reason = "RESIDUAL_CASH_CHANGED"
            if reason is not None:
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date, "stage": "execution",
                                   "reason": reason})
                continue
            lot_id = make_record_id("residual-add-lot", proposal.proposal_id)
            per_share_risk = max(
                0.0, price-float(proposal.current_stop_raw_snapshot))
            lot = IsolatedSleeveLot(
                lot_id=lot_id, trade_id=proposal.trade_id,
                proposal_id=proposal.proposal_id, symbol=proposal.symbol,
                quantity=quantity, entry_date=date, entry_price_raw=price,
                entry_fees_cash=fees(quantity, price, "buy"),
                entry_cost_cash=cost,
                risk_cash_frozen=per_share_risk*quantity)
            active[lot_id] = lot
            entries.append(lot)
            overlay_cash_delta -= cost

        for event in panel.corporate_actions.get(day, []):
            symbol = panel.symbols[event["symbol"]]
            for lot_id, lot in tuple(active.items()):
                if lot.symbol != symbol:
                    continue
                if event["cash_per_share"] and event["cash_day"] is not None:
                    amount = lot.quantity*float(event["cash_per_share"])
                    pending_cash[event["cash_day"]] = \
                        pending_cash.get(event["cash_day"], 0.0)+amount
                    dividends_by_lot[lot_id] = \
                        dividends_by_lot.get(lot_id, 0.0)+amount
                if event["stock_per_share"] and event["stock_day"] is not None:
                    pending_stock.setdefault(event["stock_day"], []).append(
                        (lot_id, 1.0+float(event["stock_per_share"])))

        add_market = sum(lot.quantity*mark(lot, day)
                         for lot in active.values())
        combined_cash = float(base.cash)+overlay_cash_delta
        combined_stocks = float(base.stocks)+add_market
        combined_capital = combined_cash+combined_stocks
        curve.append({
            "date": date, "cash": combined_cash,
            "stocks": combined_stocks, "capital": combined_capital,
            "exposure": (combined_stocks/combined_capital
                         if combined_capital > 0 else np.nan),
            "base_capital": float(base.capital),
            "add_market_value": add_market,
            "overlay_cash_delta": overlay_cash_delta,
            "add_holdings": len(active),
        })

        if day >= last:
            continue
        base_rows = [dict(row) for row in positions_by_date.get(date, ())]
        current_add_rows = add_rows(day)
        all_rows = base_rows+current_add_rows
        base_equity = float(base.capital)
        equity = base_equity+overlay_cash_delta+add_market
        gross = sum(float(row["market_value"]) for row in all_rows)
        open_risk = sum(float(row["open_risk"]) for row in all_rows)
        cash_available = max(
            0.0, float(base.cash)+overlay_cash_delta-
            base_reservations.get(date, 0.0))
        same_day_risk = base_new_risk.get(date, 0.0)
        reserved_add_cash = 0.0
        reserved_add_risk = 0.0
        pending_candidates = []
        for proposal in proposals_by_signal.get(date, ()):
            if exits[proposal.trade_id] <= int(proposal.valid_session):
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date, "stage": "approval",
                                   "reason": "BASE_EXIT_NOT_AFTER_ADD"})
                continue
            column = panel.symbol_index[proposal.symbol]
            max_price = float(proposal.max_buy_price_raw)
            stop = float(proposal.current_stop_raw_snapshot)
            per_share_risk = max_price-stop
            if not np.isfinite(per_share_risk) or per_share_risk <= 0:
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date, "stage": "approval",
                                   "reason": "NO_EXECUTABLE_INITIAL_R"})
                continue
            industry = risk_engine._industry(day, column)
            existing_symbol = sum(
                float(row["market_value"]) for row in all_rows+pending_candidates
                if row["symbol"] == proposal.symbol)
            industry_risk = sum(
                float(row["open_risk"]) for row in all_rows+pending_candidates
                if row["industry"] == industry)
            start = max(0, day-19)
            amount = np.asarray(panel.amount[start:day+1, column], dtype=float)
            average_amount = (float(np.nanmean(amount))
                              if np.isfinite(amount).any() else np.nan)
            caps = {
                "PROPOSAL_RISK": int(
                    proposal.risk_budget_cash_cap/per_share_risk/100)*100,
                "PROPOSAL_NOTIONAL": int(
                    proposal.notional_cash_cap/max_price/100)*100,
                "SYMBOL_WEIGHT": int(max(
                    0.0, equity*config.max_symbol_weight-existing_symbol) /
                    max_price/100)*100,
                "GROSS_EXPOSURE": int(max(
                    0.0, equity*config.max_gross_exposure-gross-
                    sum(row["market_value"] for row in pending_candidates)) /
                    max_price/100)*100,
                "PORTFOLIO_OPEN_RISK": int(max(
                    0.0, equity*config.portfolio_open_risk_fraction-open_risk-
                    reserved_add_risk)/per_share_risk/100)*100,
                "INDUSTRY_OPEN_RISK": int(max(
                    0.0, equity*config.industry_open_risk_fraction-
                    industry_risk)/per_share_risk/100)*100,
                "SAME_DAY_NEW_RISK": int(max(
                    0.0, equity*config.same_day_new_risk_fraction-
                    same_day_risk-reserved_add_risk)/per_share_risk/100)*100,
                "CAPACITY": (int(
                    average_amount*config.max_amount_participation/
                    max_price/100)*100 if np.isfinite(average_amount) else 0),
                "CASH": int(max(
                    0.0, cash_available-reserved_add_cash)/max_price/100)*100,
            }
            if proposal.quantity_cap_optional is not None:
                caps["POLICY_QUANTITY"] = int(
                    proposal.quantity_cap_optional)//100*100
            quantity = min(caps.values())
            while quantity >= 100:
                maximum_cost = quantity*max_price+fees(
                    quantity, max_price, "buy")
                if maximum_cost <= cash_available-reserved_add_cash+1e-9:
                    break
                quantity -= 100
            pre_stress_quantity = quantity
            candidate = {
                "symbol": proposal.symbol, "trade_id": proposal.trade_id,
                "quantity": quantity,
                "market_value": quantity*max_price,
                "industry": industry,
                "beta": risk_engine.beta_for(day, column),
                "open_risk": quantity*per_share_risk,
                "initial_r_cash": quantity*per_share_risk,
                "has_stop": True,
                "limit_fraction": risk_engine._limit_fraction(day, column),
                "pending": True, "is_add": True,
                "latest_fill_date": 0,
            }
            while quantity >= 100:
                candidate["quantity"] = quantity
                candidate["market_value"] = quantity*max_price
                candidate["open_risk"] = quantity*per_share_risk
                candidate["initial_r_cash"] = quantity*per_share_risk
                losses = risk_engine._stress_from_rows(
                    all_rows+pending_candidates+[candidate])
                if max(losses.values(), default=0.0) <= \
                        equity*config.max_stress_loss_fraction+1e-9:
                    break
                quantity -= 100
            if quantity < 100:
                if pre_stress_quantity >= 100:
                    binding = ("STRESS_LOSS",)
                else:
                    minimum = min(caps.values())
                    binding = tuple(sorted(
                        name for name, cap in caps.items() if cap == minimum))
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date, "stage": "approval",
                                   "reason": "NO_RESIDUAL_"+binding[0],
                                   "binding_caps": binding})
                continue
            maximum_cost = quantity*max_price+fees(
                quantity, max_price, "buy")
            if maximum_cost > cash_available-reserved_add_cash+1e-9:
                rejections.append({"proposal_id": proposal.proposal_id,
                                   "date": date, "stage": "approval",
                                   "reason": "NO_RESIDUAL_CASH_AFTER_FEES",
                                   "binding_caps": ("CASH",)})
                continue
            candidate = dict(candidate)
            candidate["quantity"] = quantity
            candidate["market_value"] = quantity*max_price
            candidate["open_risk"] = quantity*per_share_risk
            candidate["initial_r_cash"] = quantity*per_share_risk
            pending_candidates.append(candidate)
            reserved_add_cash += maximum_cost
            reserved_add_risk += quantity*per_share_risk
            approved_by_date.setdefault(int(proposal.valid_session), []).append({
                "proposal": proposal, "quantity": quantity})
            approvals.append({
                "proposal_id": proposal.proposal_id,
                "signal_asof": date,
                "valid_session": int(proposal.valid_session),
                "symbol": proposal.symbol, "quantity": quantity,
                "max_buy_price_raw": max_price,
                "reserved_cash": maximum_cost,
                "reserved_risk_cash": quantity*per_share_risk,
                "base_cash_reserved": base_reservations.get(date, 0.0),
                "quantity_caps": dict(caps),
            })

    return {
        "curve": pd.DataFrame(curve), "entries": entries,
        "dispositions": dispositions, "approvals": pd.DataFrame(approvals),
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
