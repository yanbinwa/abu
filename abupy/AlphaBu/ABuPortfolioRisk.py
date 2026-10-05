# -*- encoding: utf-8 -*-
"""Frozen portfolio risk sizing, stress approval and shadow decisions."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np

from .ABuPriceLimit import price_limit_rule
from .ABuTradeIntent import Reservation, TradeIntent, make_record_id


@dataclass(frozen=True)
class RiskConfig:
    risk_version: str = "risk_v1"
    single_trade_risk_fraction: float = 0.0025
    portfolio_open_risk_fraction: float = 0.0200
    industry_open_risk_fraction: float = 0.0060
    same_day_new_risk_fraction: float = 0.0075
    max_symbol_weight: float = 0.08
    max_gross_exposure: float = 0.80
    max_amount_participation: float = 0.05
    max_stress_loss_fraction: float = 0.06
    missing_beta: float = 1.50
    beta_clip: tuple[float, float] = (0.50, 2.00)
    unknown_industry_bucket: str = "UNKNOWN"
    unknown_st_policy: str = "exclude"
    unknown_limit_fraction: float = 0.05
    buy_order_valid_sessions: int = 1

    def __post_init__(self):
        if len(self.beta_clip) != 2 or self.beta_clip[0] > self.beta_clip[1]:
            raise ValueError("beta_clip must contain ordered lower and upper bounds")
        fractions = [
            self.single_trade_risk_fraction,
            self.portfolio_open_risk_fraction,
            self.industry_open_risk_fraction,
            self.same_day_new_risk_fraction,
            self.max_symbol_weight, self.max_gross_exposure,
            self.max_amount_participation, self.max_stress_loss_fraction,
            self.unknown_limit_fraction,
        ]
        if any(value <= 0 or value > 1 for value in fractions):
            raise ValueError("risk fractions must be in (0, 1]")
        if self.missing_beta <= 0 or self.buy_order_valid_sessions <= 0:
            raise ValueError("invalid beta or order validity")
        if self.unknown_st_policy not in ("exclude", "include"):
            raise ValueError("unknown_st_policy must be exclude or include")

    @property
    def sha256(self):
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_risk_config(path):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    expected = {item.name for item in fields(RiskConfig)}
    unknown = set(payload) - expected
    missing = expected - set(payload)
    if unknown or missing:
        raise ValueError("risk config fields mismatch: unknown={}, missing={}".format(
            sorted(unknown), sorted(missing)))
    payload["beta_clip"] = tuple(payload["beta_clip"])
    return RiskConfig(**payload)


@dataclass(frozen=True)
class RiskDecision:
    intent_id: str
    signal_asof: int
    symbol: str
    shadow: bool
    decision: str
    requested_quantity: int
    quantity_r: int
    quantity_symbol: int
    quantity_gross: int
    quantity_capacity: int
    quantity_cash: int
    quantity_portfolio_risk: int
    quantity_industry_risk: int
    quantity_same_day_risk: int
    quantity_stress: int
    final_quantity: int
    planned_risk_per_share: float
    equity: float
    gross_exposure: float
    open_risk_cash: float
    industry_open_risk_cash: float
    same_day_new_risk_cash: float
    max_stress_loss_cash: float
    decisive_scenario: str
    reason_codes: tuple[str, ...]
    config_sha256: str
    stress_losses: dict
    quantity_symbol_headroom: int = 0
    quantity_notional_cap: int = 0
    quantity_trade_add_risk: int = 0
    trade_add_risk_headroom_cash: float = 0.0

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class PostFillRiskReview:
    date: int
    equity: float
    gross_exposure: float
    open_risk_cash: float
    stress_losses: dict
    breach_codes: tuple[str, ...]
    de_risk_intents: tuple[TradeIntent, ...]


def _lots(value):
    if not np.isfinite(value) or value <= 0:
        return 0
    return max(0, int(np.floor(value / 100.0)) * 100)


class PortfolioRiskEngine(object):

    def __init__(self, panel, config=None):
        self.panel = panel
        self.config = config or RiskConfig()
        self.decisions = []

    def _mark(self, executor, day, column):
        value = float(self.panel.exec_close[day, column])
        if np.isfinite(value) and value > 0:
            return value
        fallback = float(executor.last_close[column])
        return fallback if np.isfinite(fallback) and fallback > 0 else 0.0

    def _industry(self, day, column):
        value = self.panel.industry[day, column]
        return (str(int(value)) if np.isfinite(value) and value >= 0
                else self.config.unknown_industry_bucket)

    def beta_for(self, day, column, lookback=120):
        start = max(0, day - lookback + 1)
        stock = self.panel.returns[start:day + 1, column].astype(float)
        market = self.panel.benchmark_returns[start:day + 1].astype(float)
        valid = np.isfinite(stock) & np.isfinite(market)
        if valid.sum() < 20:
            return self.config.missing_beta
        stock = stock[valid]; market = market[valid]
        variance = float(np.sum((market - market.mean()) ** 2))
        if variance <= 1e-18:
            return self.config.missing_beta
        beta = float(np.sum((stock - stock.mean()) *
                            (market - market.mean())) / variance)
        if not np.isfinite(beta):
            return self.config.missing_beta
        return float(np.clip(beta, *self.config.beta_clip))

    def _portfolio(self, executor, day, candidate=None):
        rows = []
        ledger = getattr(executor, "position_ledger", None)
        ledger_trades = ledger.active_trades() if ledger is not None else []
        if ledger_trades:
            for trade in ledger_trades:
                symbol = trade.symbol
                column = self.panel.symbol_index[symbol]
                mark = self._mark(executor, day, column)
                quantity = ledger.quantity_for_trade(trade.trade_id)
                stop = trade.current_stop_raw
                lots = ledger.lots_for_trade(trade.trade_id)
                rows.append({
                    "symbol": symbol, "trade_id": trade.trade_id,
                    "quantity": quantity, "market_value": mark * quantity,
                    "industry": self._industry(day, column),
                    "beta": self.beta_for(day, column),
                    "open_risk": max(0.0, mark-float(stop or mark))*quantity,
                    "initial_r_cash": trade.initial_r_cash_frozen,
                    "has_stop": stop is not None,
                    "limit_fraction": self._limit_fraction(day, column),
                    "pending": False,
                    "is_add": all(lot.position_effect == "INCREASE" for lot in lots),
                    "latest_fill_date": max((lot.fill_date for lot in lots), default=0),
                })
        else:
            for symbol, position in sorted(executor.positions.items()):
                column = self.panel.symbol_index[symbol]
                mark = self._mark(executor, day, column)
                executable_stop = (position.current_stop_raw
                                   if position.current_stop_raw is not None
                                   else position.initial_stop_raw)
                open_risk = max(0.0, mark -
                                float(executable_stop or mark)) * position.quantity
                rows.append({
                    "symbol": symbol, "trade_id": "", "quantity": position.quantity,
                    "market_value": mark * position.quantity,
                    "industry": self._industry(day, column),
                    "beta": self.beta_for(day, column), "open_risk": open_risk,
                    "initial_r_cash": position.initial_r_cash_frozen,
                    "has_stop": executable_stop is not None,
                    "limit_fraction": self._limit_fraction(day, column),
                    "pending": False, "is_add": False, "latest_fill_date": 0,
                })
        for order in sorted(executor.orders, key=lambda item: item.order_id):
            if order.side != "buy":
                continue
            reservation = executor.reservations.get(order.order_id)
            if reservation is None:
                continue
            column = self.panel.symbol_index[order.symbol]
            value = order.quantity * float(order.max_buy_price_raw)
            rows.append({
                "symbol": order.symbol, "quantity": order.quantity,
                "market_value": value,
                "industry": self._industry(day, column),
                "beta": self.beta_for(day, column),
                "open_risk": reservation.reserved_risk,
                "initial_r_cash": order.planned_initial_r_cash,
                "has_stop": order.initial_stop_raw is not None,
                "limit_fraction": self._limit_fraction(day, column),
                "pending": True,
                "trade_id": order.target_trade_id,
                "is_add": order.position_effect == "INCREASE",
                "latest_fill_date": 0,
            })
        if candidate is not None and candidate["quantity"] > 0:
            rows.append(candidate)
        return rows

    def _limit_fraction(self, day, column):
        if self.panel.list_date[column] < int(self.panel.dates[0]):
            listing_session = 6
        else:
            listing_session = int(self.panel.universe_mask[:day + 1, column].sum())
        rule = price_limit_rule(
            str(self.panel.board[column]), int(self.panel.dates[day]),
            bool(self.panel.st_status[day, column]), listing_session,
            bool(self.panel.st_status_known[day, column]),
        )
        return (float(rule.lower_fraction) if rule.lower_fraction is not None
                else self.config.unknown_limit_fraction)

    def stress_losses(self, executor, day, candidate=None):
        rows = self._portfolio(executor, day, candidate)
        return self._stress_from_rows(rows)

    def _stress_from_rows(self, rows):
        market7 = sum(row["market_value"] * 0.07 * row["beta"] for row in rows)
        industries = sorted(set(row["industry"] for row in rows))
        industry10 = max((sum(row["market_value"] * 0.10 for row in rows
                              if row["industry"] == industry)
                          for industry in industries), default=0.0)
        market5industry5 = max((
            sum(row["market_value"] * 0.05 * row["beta"] for row in rows) +
            sum(row["market_value"] * 0.05 for row in rows
                if row["industry"] == industry)
            for industry in industries), default=0.0)
        gap2r = sum(
            max(row["open_risk"], 2 * row["initial_r_cash"])
            if row["has_stop"] else row["market_value"] * 0.10
            for row in rows
        )
        limits = {
            days: sum(row["market_value"] *
                      (1 - (1 - row["limit_fraction"]) ** days)
                      for row in rows)
            for days in (1, 3, 5)
        }
        return {
            "MARKET_7": float(market7),
            "INDUSTRY_10": float(industry10),
            "MARKET_5_PLUS_INDUSTRY_5": float(market5industry5),
            "GAP_2R": float(gap2r),
            "LIMIT_DOWN_1": float(limits[1]),
            "LIMIT_DOWN_3": float(limits[3]),
            "LIMIT_DOWN_5": float(limits[5]),
        }

    def _state(self, executor, day):
        rows = self._portfolio(executor, day)
        market_value = sum(row["market_value"] for row in rows)
        equity = executor.cash + sum(row["market_value"] for row in rows
                                     if not row.get("pending", False))
        open_risk = sum(row["open_risk"] for row in rows)
        industries = {}
        for row in rows:
            industries[row["industry"]] = (industries.get(row["industry"], 0.0) +
                                            row["open_risk"])
        same_day = 0.0
        for order in executor.orders:
            reservation = executor.reservations.get(order.order_id)
            if reservation is None or order.side != "buy":
                continue
            if order.created_asof == int(self.panel.dates[day]):
                same_day += reservation.reserved_same_day_risk
        return equity, market_value, open_risk, industries, same_day

    def evaluate(self, executor, intent, day, entry_day,
                 requested_quantity=None, shadow=False):
        if intent.side != "buy":
            raise ValueError("risk sizing currently applies to buy intents")
        column = self.panel.symbol_index[intent.symbol]
        raw = float(intent.signal_price_raw or self.panel.exec_close[day, column])
        max_price = float(intent.metadata.get(
            "max_buy_price_raw", raw * (1 + float(
                intent.metadata.get("max_gap_fraction", 0.03)))))
        stop = intent.initial_stop_raw
        risk_per_share = max_price - float(stop) if stop is not None else 0.0
        equity, gross, open_risk, industries, same_day = self._state(executor, day)
        industry = self._industry(day, column)
        industry_risk = industries.get(industry, 0.0)
        reason = []
        if not np.isfinite(risk_per_share) or risk_per_share <= 0:
            reason.append("NO_EXECUTABLE_INITIAL_R")
        quantity_r = _lots(equity * self.config.single_trade_risk_fraction /
                           risk_per_share) if risk_per_share > 0 else 0
        requested = (_lots(requested_quantity) if requested_quantity is not None
                     else quantity_r)
        rows = self._portfolio(executor, day)
        existing_symbol_value = sum(row["market_value"] for row in rows
                                    if row["symbol"] == intent.symbol)
        quantity_symbol = _lots(max(
            0.0, equity * self.config.max_symbol_weight-existing_symbol_value
        ) / max_price)
        quantity_gross = _lots(max(0.0, equity * self.config.max_gross_exposure -
                                   gross) / max_price)
        start = max(0, day - 19)
        amount = self.panel.amount[start:day + 1, column].astype(float)
        average_amount = float(np.nanmean(amount)) if np.isfinite(amount).any() else np.nan
        quantity_capacity = _lots(average_amount * self.config.max_amount_participation /
                                  max_price) if np.isfinite(average_amount) else 0
        if not np.isfinite(average_amount):
            reason.append("MISSING_AMOUNT")
        quantity_cash = _lots(max(0.0, executor.available_cash) / max_price)
        while quantity_cash > 0:
            fees = sum(executor._fees(quantity_cash, max_price, "buy"))
            if quantity_cash * max_price + fees <= executor.available_cash + 1e-9:
                break
            quantity_cash -= 100
        if risk_per_share > 0:
            quantity_portfolio = _lots(max(
                0.0, equity * self.config.portfolio_open_risk_fraction - open_risk
            ) / risk_per_share)
            quantity_industry = _lots(max(
                0.0, equity * self.config.industry_open_risk_fraction - industry_risk
            ) / risk_per_share)
            quantity_same_day = _lots(max(
                0.0, equity * self.config.same_day_new_risk_fraction - same_day
            ) / risk_per_share)
        else:
            quantity_portfolio = quantity_industry = quantity_same_day = 0

        effect = intent.position_effect or "OPEN"
        quantity_notional = requested
        quantity_trade_add = requested
        trade_add_headroom = 0.0
        if effect == "INCREASE":
            notional_cap = float(intent.metadata.get("notional_cash_cap", 0.0))
            quantity_notional = _lots(notional_cap / max_price) if notional_cap > 0 else 0
            ledger = getattr(executor, "position_ledger", None)
            trade = (ledger.logical_trades.get(intent.trade_id)
                     if ledger is not None else None)
            if trade is None or trade.status != "ACTIVE":
                reason.append("TARGET_TRADE_NOT_ACTIVE")
                quantity_trade_add = 0
            else:
                proposed_budget = float(intent.metadata.get(
                    "risk_budget_cash_cap", trade.add_risk_budget_cash_frozen))
                if not trade.add_risk_budget_cash_frozen and proposed_budget > 0:
                    ledger.configure_add_risk_budget(intent.trade_id, proposed_budget)
                    trade = ledger.logical_trades[intent.trade_id]
                trade_add_headroom = max(
                    0.0, trade.add_risk_budget_cash_frozen -
                    trade.filled_add_risk_cash_frozen -
                    trade.reserved_add_risk_cash)
                quantity_trade_add = (_lots(trade_add_headroom / risk_per_share)
                                      if risk_per_share > 0 else 0)

        pre_stress = min(requested, quantity_r, quantity_symbol, quantity_gross,
                         quantity_capacity, quantity_cash, quantity_portfolio,
                         quantity_industry, quantity_same_day,
                         quantity_notional, quantity_trade_add)
        pre_stress = _lots(pre_stress)
        stress_budget = equity * self.config.max_stress_loss_fraction

        def losses_for(quantity):
            candidate = {
                "symbol": intent.symbol, "quantity": quantity,
                "market_value": quantity * max_price, "industry": industry,
                "beta": self.beta_for(day, column),
                "open_risk": quantity * max(0.0, max_price - float(stop or max_price)),
                "initial_r_cash": quantity * max(0.0, risk_per_share),
                "has_stop": stop is not None,
                "limit_fraction": self._limit_fraction(day, column),
                "pending": True,
            }
            return self.stress_losses(executor, day, candidate)

        lo, hi = 0, pre_stress // 100
        while lo < hi:
            middle = (lo + hi + 1) // 2
            losses = losses_for(middle * 100)
            if max(losses[key] for key in (
                    "MARKET_7", "INDUSTRY_10",
                    "MARKET_5_PLUS_INDUSTRY_5", "GAP_2R")) <= stress_budget + 1e-9:
                lo = middle
            else:
                hi = middle - 1
        quantity_stress = lo * 100
        stress = losses_for(quantity_stress)
        approval_losses = {key: stress[key] for key in (
            "MARKET_7", "INDUSTRY_10", "MARKET_5_PLUS_INDUSTRY_5", "GAP_2R")}
        decisive = max(approval_losses, key=approval_losses.get)
        final = min(pre_stress, quantity_stress)
        pre_stress_caps = [
            (quantity_r, "SINGLE_TRADE_RISK"),
            (quantity_symbol, "MAX_SYMBOL_WEIGHT"),
            (quantity_gross, "MAX_GROSS_EXPOSURE"),
            (quantity_capacity, "CAPACITY"),
            (quantity_cash, "CASH"),
            (quantity_portfolio, "PORTFOLIO_OPEN_RISK"),
            (quantity_industry, "INDUSTRY_OPEN_RISK"),
            (quantity_same_day, "SAME_DAY_NEW_RISK"),
            (quantity_notional, "ADD_NOTIONAL_CAP"),
            (quantity_trade_add, "TRADE_ADD_RISK_BUDGET"),
        ]
        reason.extend(code for quantity, code in pre_stress_caps
                      if quantity == pre_stress and quantity < requested)
        if pre_stress > 0 and quantity_stress < pre_stress:
            reason.append("STRESS_LOSS")
        decision_name = ("rejected" if final < 100 else
                         "reduced" if final < requested else "approved")
        if final < 100 and not reason:
            reason.append("BELOW_BOARD_LOT")
        result = RiskDecision(
            intent_id=intent.intent_id, signal_asof=int(intent.signal_asof),
            symbol=intent.symbol, shadow=bool(shadow), decision=decision_name,
            requested_quantity=requested, quantity_r=quantity_r,
            quantity_symbol=quantity_symbol, quantity_gross=quantity_gross,
            quantity_capacity=quantity_capacity, quantity_cash=quantity_cash,
            quantity_portfolio_risk=quantity_portfolio,
            quantity_industry_risk=quantity_industry,
            quantity_same_day_risk=quantity_same_day,
            quantity_stress=quantity_stress, final_quantity=final,
            planned_risk_per_share=risk_per_share, equity=equity,
            gross_exposure=gross, open_risk_cash=open_risk,
            industry_open_risk_cash=industry_risk,
            same_day_new_risk_cash=same_day,
            max_stress_loss_cash=max(approval_losses.values()),
            decisive_scenario=decisive, reason_codes=tuple(dict.fromkeys(reason)),
            config_sha256=self.config.sha256, stress_losses=stress,
            quantity_symbol_headroom=quantity_symbol,
            quantity_notional_cap=quantity_notional,
            quantity_trade_add_risk=quantity_trade_add,
            trade_add_risk_headroom_cash=trade_add_headroom,
        )
        self.decisions.append(result)
        return result

    def requested_quantity_for_risk_fraction(self, executor, intent, day,
                                             risk_fraction):
        """Return a board-lot quantity fixed from close-t information."""
        risk_fraction = float(risk_fraction)
        if not np.isfinite(risk_fraction) or risk_fraction <= 0 or \
                risk_fraction > self.config.single_trade_risk_fraction:
            raise ValueError("risk fraction exceeds configured sizing ceiling")
        column = self.panel.symbol_index[intent.symbol]
        raw = float(intent.signal_price_raw or self.panel.exec_close[day, column])
        max_price = float(intent.metadata.get(
            "max_buy_price_raw", raw * (1 + float(
                intent.metadata.get("max_gap_fraction", 0.03)))))
        stop = intent.initial_stop_raw
        risk_per_share = max_price-float(stop) if stop is not None else 0.0
        if not np.isfinite(risk_per_share) or risk_per_share <= 0:
            return 0
        equity = self._state(executor, day)[0]
        return _lots(equity*risk_fraction/risk_per_share)

    def post_fill_review(self, executor, day):
        """Record breaches and create next-session full-exit intents.

        Historical fills are never rolled back.  Suggested exits are processed
        by the normal sell-order lifecycle and can therefore be deferred.
        """
        rows = [row for row in self._portfolio(executor, day)
                if not row.get("pending", False)]
        equity = executor.cash + sum(row["market_value"] for row in rows)
        gross = sum(row["market_value"] for row in rows)
        open_risk = sum(row["open_risk"] for row in rows)

        def breaches(current):
            current_gross = sum(row["market_value"] for row in current)
            current_risk = sum(row["open_risk"] for row in current)
            industry = {}
            for row in current:
                industry[row["industry"]] = industry.get(row["industry"], 0.0) + row["open_risk"]
            stress = self._stress_from_rows(current)
            codes = []
            if current_gross > equity * self.config.max_gross_exposure + 1e-9:
                codes.append("POST_FILL_GROSS")
            if current_risk > equity * self.config.portfolio_open_risk_fraction + 1e-9:
                codes.append("POST_FILL_OPEN_RISK")
            if any(value > equity * self.config.industry_open_risk_fraction + 1e-9
                   for value in industry.values()):
                codes.append("POST_FILL_INDUSTRY_RISK")
            approval = [stress[key] for key in (
                "MARKET_7", "INDUSTRY_10", "MARKET_5_PLUS_INDUSTRY_5", "GAP_2R")]
            if max(approval, default=0.0) > equity * self.config.max_stress_loss_fraction + 1e-9:
                codes.append("POST_FILL_STRESS")
            return tuple(codes), stress

        initial_codes, initial_stress = breaches(rows)
        remaining = list(rows)
        selected = []
        for row in sorted(rows, key=lambda item: (
                0 if item.get("is_add") else 1,
                -item["open_risk"], -item.get("latest_fill_date", 0),
                item.get("trade_id", ""), item["symbol"])):
            codes, _ = breaches(remaining)
            if not codes:
                break
            selected.append(row)
            remaining = [item for item in remaining
                         if not (item["symbol"] == row["symbol"] and
                                 item.get("trade_id", "") == row.get("trade_id", ""))]
        intents = tuple(
            TradeIntent(
                intent_id=make_record_id("de-risk", int(self.panel.dates[day]),
                                         row["symbol"]),
                strategy_id="portfolio_risk", strategy_version=self.config.risk_version,
                signal_asof=int(self.panel.dates[day]), symbol=row["symbol"], side="sell",
                trade_id=row.get("trade_id", ""), position_effect="CLOSE",
                metadata={"reason_codes": initial_codes,
                          "requested_quantity": int(row["quantity"])},
            ) for row in selected
        )
        return PostFillRiskReview(
            date=int(self.panel.dates[day]), equity=equity,
            gross_exposure=gross, open_risk_cash=open_risk,
            stress_losses=initial_stress, breach_codes=initial_codes,
            de_risk_intents=intents,
        )

    def approve(self, executor, intent, day, entry_day,
                requested_quantity=None, shadow=False):
        decision = self.evaluate(
            executor, intent, day, entry_day,
            requested_quantity=requested_quantity, shadow=shadow,
        )
        quantity = (decision.requested_quantity if shadow
                    else decision.final_quantity)
        raw = float(intent.signal_price_raw)
        max_price = float(intent.metadata.get(
            "max_buy_price_raw", raw * (1 + float(
                intent.metadata.get("max_gap_fraction", 0.03)))))
        if quantity < 100:
            reservation = Reservation(
                reservation_id=make_record_id("risk-rejection", intent.intent_id),
                intent_id=intent.intent_id, reserved_cash=0.0,
                reserved_risk=0.0, reserved_industry_risk=0.0,
                reserved_same_day_risk=0.0, reserved_stress_loss=0.0,
                expires_on=int(self.panel.dates[entry_day]), decision="rejected",
                reason_codes=decision.reason_codes,
            )
            return None, reservation, decision
        order, reservation = executor.approve_order(
            intent, quantity, int(self.panel.dates[entry_day]),
            max_buy_price_raw=max_price,
            planned_risk_per_share=decision.planned_risk_per_share,
            portfolio_equity_asof=decision.equity,
        )
        return order, reservation, decision

    def approve_batch(self, executor, intents, day, entry_day,
                      requested_quantities=None, shadow=False):
        requested_quantities = requested_quantities or {}
        results = []
        for intent in sorted(intents, key=lambda item: (
                -item.score, item.strategy_id, item.symbol, item.intent_id)):
            results.append(self.approve(
                executor, intent, day, entry_day,
                requested_quantity=requested_quantities.get(intent.intent_id),
                shadow=shadow,
            ))
        return results

    def export_audit(self, output_dir):
        """Persist the exact config and append-only decision evidence."""
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        config_payload = asdict(self.config)
        config_payload["config_sha256"] = self.config.sha256
        (output_dir / "risk_config.json").write_text(
            json.dumps(config_payload, ensure_ascii=False, indent=2,
                       sort_keys=True) + "\n", encoding="utf-8"
        )
        with (output_dir / "risk_decisions.jsonl").open("w", encoding="utf-8") as output:
            for decision in self.decisions:
                output.write(json.dumps(
                    decision.to_dict(), ensure_ascii=False, sort_keys=True
                ) + "\n")
        return {
            "config": output_dir / "risk_config.json",
            "decisions": output_dir / "risk_decisions.jsonl",
        }
