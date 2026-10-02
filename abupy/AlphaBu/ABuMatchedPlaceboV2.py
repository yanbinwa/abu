# -*- encoding: utf-8 -*-
"""Point-in-time matched placebo intents executed by PortfolioExecutor."""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from .ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor
from .ABuTradeIntent import TradeIntent, make_record_id


@dataclass(frozen=True)
class PlaceboConfig:
    pool_size: int = 50
    min_history: int = 120
    lookback_liquidity: int = 60
    lookback_beta: int = 120
    replicates: int = 1000
    seed: int = 20261002
    unknown_st_policy: str = "exclude"


class MatchedPlaceboV2(object):

    def __init__(self, panel, config=None):
        self.panel = panel
        self.config = config or PlaceboConfig()
        self.date_index = {int(date): position
                           for position, date in enumerate(panel.dates)}

    def matching_pool(self, intent: TradeIntent):
        """Build a pool using signal-day and earlier observations only."""
        day = self.date_index[int(intent.signal_asof)]
        target = self.panel.symbol_index[intent.symbol]
        start_liq = max(0, day - self.config.lookback_liquidity + 1)
        start_beta = max(0, day - self.config.lookback_beta + 1)
        with np.errstate(divide="ignore", invalid="ignore"):
            price = self.panel.close[day].astype(np.float64)
            traded = self.panel.amount[start_liq:day + 1].astype(np.float64)
            fallback = (self.panel.close[start_liq:day + 1].astype(np.float64) *
                        self.panel.volume[start_liq:day + 1].astype(np.float64))
            liquidity = np.nanmean(np.where(np.isfinite(traded), traded, fallback), axis=0)
            volatility = np.nanstd(
                self.panel.returns[start_liq:day + 1].astype(np.float64), axis=0
            )
            stocks = self.panel.returns[start_beta:day + 1].astype(np.float64)
            market = self.panel.benchmark_returns[start_beta:day + 1].astype(np.float64)
            market_centered = market - np.nanmean(market)
            denominator = np.nansum(market_centered ** 2)
            beta = np.full(len(self.panel.symbols), np.nan)
            if denominator > 0:
                beta = np.nansum(
                    (stocks - np.nanmean(stocks, axis=0)) * market_centered[:, None],
                    axis=0,
                ) / denominator
        cap = self.panel.market_cap[day]
        industry = self.panel.industry[day]
        eligible = self.panel.signal_eligible(
            self.config.min_history,
            unknown_st_policy=self.config.unknown_st_policy,
        )[day].copy()
        eligible &= np.isfinite(price) & (price > 1)
        eligible &= np.isfinite(liquidity) & (liquidity > 0)
        eligible &= np.isfinite(volatility)
        eligible[target] = False
        candidates = np.flatnonzero(eligible)
        if len(candidates) == 0:
            return np.array([], dtype=np.int32)
        target_industry = industry[target]
        if target_industry >= 0:
            same = candidates[industry[candidates] == target_industry]
            if len(same) >= min(10, self.config.pool_size):
                candidates = same

        distance = np.zeros(len(candidates), dtype=np.float64)
        used = np.zeros(len(candidates), dtype=np.float64)
        for values, logarithmic in (
            (price, True), (liquidity, True), (cap, True),
            (volatility, False), (beta, False),
        ):
            target_value = values[target]
            candidate_values = values[candidates]
            valid = np.isfinite(candidate_values)
            if not np.isfinite(target_value):
                continue
            if logarithmic:
                valid &= candidate_values > 0
                if target_value <= 0:
                    continue
                delta = np.zeros(len(candidates))
                delta[valid] = np.abs(np.log(candidate_values[valid] / target_value))
            else:
                scale = np.nanstd(candidate_values[valid]) if valid.any() else np.nan
                scale = scale if np.isfinite(scale) and scale > 1e-12 else 1.0
                delta = np.zeros(len(candidates))
                delta[valid] = np.abs(candidate_values[valid] - target_value) / scale
            distance[valid] += delta[valid]
            used[valid] += 1
        distance = np.divide(
            distance, used, out=np.full_like(distance, np.inf), where=used > 0
        )
        order = np.lexsort((candidates, distance))
        return candidates[order[:self.config.pool_size]].astype(np.int32)

    def substitute_intents(self, intents, seed=None, rebuild=None):
        rng = np.random.default_rng(self.config.seed if seed is None else seed)
        substitutes = []
        diagnostics = []
        for position, intent in enumerate(intents):
            pool = self.matching_pool(intent)
            if len(pool) == 0:
                diagnostics.append({
                    "intent_id": intent.intent_id, "status": "NO_MATCH",
                    "candidate_count": 0,
                })
                continue
            chosen = int(pool[rng.integers(0, len(pool))])
            symbol = self.panel.symbols[chosen]
            day = self.date_index[int(intent.signal_asof)]
            if rebuild is None:
                raw = float(self.panel.exec_close[day, chosen])
                adjusted = float(self.panel.close[day, chosen])
                factor = raw / adjusted
                stop_fraction = float(intent.metadata.get("stop_fraction", 0.08))
                substitute = replace(
                    intent,
                    intent_id=make_record_id(
                        "placebo-intent", intent.intent_id, seed, position, symbol
                    ),
                    symbol=symbol,
                    signal_price_adjusted=adjusted,
                    signal_price_raw=raw,
                    adjustment_factor_signal=factor,
                    initial_stop_adjusted=adjusted * (1 - stop_fraction),
                    initial_stop_raw=raw * (1 - stop_fraction),
                    metadata={**dict(intent.metadata),
                              "placebo_target": intent.symbol},
                )
            else:
                substitute = rebuild(intent, symbol, day, self.panel)
            substitutes.append(substitute)
            diagnostics.append({
                "intent_id": intent.intent_id, "status": "MATCHED",
                "candidate_count": len(pool), "chosen_symbol": symbol,
            })
        return substitutes, pd.DataFrame(diagnostics)

    def run_once(self, intents, seed=None, rebuild=None,
                 execution_config=None, approve=None,
                 exit_policy_factory=None):
        substitutes, diagnostics = self.substitute_intents(
            intents, seed=seed, rebuild=rebuild
        )
        by_day = {}
        for intent in substitutes:
            day = self.date_index[int(intent.signal_asof)]
            by_day.setdefault(day, []).append(intent)
        executor = PortfolioExecutor(
            self.panel, execution_config or ExecutionConfig()
        )
        active = {}
        pending_exits = []
        risk_decisions = []
        exit_policy = (exit_policy_factory(self.panel)
                       if exit_policy_factory is not None else None)
        for day in range(len(self.panel.dates)):
            for item in pending_exits:
                if item["symbol"] not in executor.positions:
                    continue
                if any(order.side == "sell" and order.symbol == item["symbol"]
                       for order in executor.orders):
                    continue
                sell = TradeIntent(
                    intent_id=make_record_id("placebo-sell", item["intent_id"], day),
                    strategy_id=item["strategy_id"],
                    strategy_version=item["strategy_version"],
                    signal_asof=int(self.panel.dates[max(0, day - 1)]),
                    symbol=item["symbol"], side="sell",
                )
                executor.approve_order(
                    sell, executor.positions[item["symbol"]].quantity,
                    int(self.panel.dates[day])
                )
            pending_exits = []
            fills = executor.process_open(day)
            for fill in fills:
                if fill.side == "buy" and fill.status == "filled":
                    source = next(item for item in substitutes
                                  if item.intent_id == fill.intent_id)
                    active[fill.symbol] = {
                        "intent_id": source.intent_id,
                        "strategy_id": source.strategy_id,
                        "strategy_version": source.strategy_version,
                        "symbol": source.symbol, "quantity": fill.quantity,
                        "entry_day": day, "source": source,
                    }
                    if exit_policy is not None:
                        exit_policy.register_entry(source, fill, day)
                elif fill.side == "sell" and fill.status == "filled":
                    active.pop(fill.symbol, None)
                    if exit_policy is not None:
                        exit_policy.remove(fill.symbol)
            executor.process_close(day)
            if day < len(self.panel.dates) - 1:
                pending_sell_symbols = {
                    order.symbol for order in executor.orders if order.side == "sell"
                }
                for symbol, item in sorted(active.items()):
                    if symbol in pending_sell_symbols:
                        continue
                    if exit_policy is None:
                        hold = int(item["source"].metadata.get("hold_sessions", 20))
                        reason = ("FIXED_HOLD" if day-item["entry_day"]+1 >= hold
                                  else None)
                    else:
                        reason = exit_policy.signal(day, symbol, fixed_hold=False)
                    if reason:
                        pending_exits.append({**item, "reason": reason})
            if day >= len(self.panel.dates) - 1:
                continue
            for intent in sorted(
                    by_day.get(day, []), key=lambda item: (-item.score, item.symbol)):
                if approve is not None:
                    approval = approve(executor, intent, day, day + 1)
                    if isinstance(approval, tuple) and len(approval) >= 3:
                        risk_decisions.append(approval[2])
                    continue
                raw = float(intent.signal_price_raw)
                max_gap_fraction = float(intent.metadata.get("max_gap_fraction", 0.03))
                max_price = raw * (1 + max_gap_fraction)
                target_notional = float(intent.metadata.get("target_notional", 40_000))
                quantity = int(target_notional / max_price / 100) * 100
                if quantity < 100:
                    continue
                planned_risk = max_price - float(intent.initial_stop_raw or max_price)
                executor.approve_order(
                    intent, quantity, int(self.panel.dates[day + 1]),
                    max_buy_price_raw=max_price,
                    planned_risk_per_share=max(0.0, planned_risk),
                )
        curve = executor.curve_frame()
        fills = executor.fills_frame()
        result = {
            "seed": self.config.seed if seed is None else int(seed),
            "matched_intents": int((diagnostics.status == "MATCHED").sum())
            if not diagnostics.empty else 0,
            "unmatched_intents": int((diagnostics.status == "NO_MATCH").sum())
            if not diagnostics.empty else 0,
            "filled_buys": int(((fills.side == "buy") &
                                (fills.status == "filled")).sum())
            if not fills.empty else 0,
            "rejected_buys": int(((fills.side == "buy") &
                                  (fills.status != "filled")).sum())
            if not fills.empty else 0,
            "open_positions": len(executor.positions),
            "unable_exit_positions": sum(
                1 for order in executor.orders if order.side == "sell"
            ),
            "risk_rejected": sum(getattr(item, "decision", "") == "rejected"
                                 for item in risk_decisions),
            "risk_reduced": sum(getattr(item, "decision", "") == "reduced"
                                for item in risk_decisions),
            "return_pct": (curve.capital.iloc[-1] /
                           executor.config.initial_cash - 1) * 100,
            "max_drawdown_pct": (
                np.r_[executor.config.initial_cash, curve.capital.to_numpy()] /
                np.maximum.accumulate(
                    np.r_[executor.config.initial_cash, curve.capital.to_numpy()]
                ) - 1
            ).min() * 100,
        }
        return result, curve, fills, diagnostics

    def run_distribution(self, intents, replicates=None, replicate_ids=None,
                         rebuild=None, execution_config=None, approve=None,
                         exit_policy_factory=None):
        count = int(replicates or self.config.replicates)
        indices = (range(count) if replicate_ids is None
                   else sorted(set(int(item) for item in replicate_ids)))
        rows = []
        for replicate in indices:
            if replicate < 0:
                raise ValueError("replicate ids must be non-negative")
            seed = self.config.seed + replicate
            result, _, _, _ = self.run_once(
                intents, seed=seed, rebuild=rebuild,
                execution_config=execution_config, approve=approve,
                exit_policy_factory=exit_policy_factory,
            )
            result["replicate"] = replicate
            rows.append(result)
        return pd.DataFrame(rows)


def summarize_placebos(distribution, actual_return_pct=None):
    """Create a deterministic inference summary from completed replicates."""
    frame = pd.DataFrame(distribution).copy()
    if frame.empty:
        return {"replicates": 0, "actual_return_pct": actual_return_pct,
                "actual_percentile": np.nan}
    returns = pd.to_numeric(frame.return_pct, errors="coerce")
    valid = returns[np.isfinite(returns)]
    summary = {
        "replicates": int(len(frame)),
        "valid_replicates": int(len(valid)),
        "return_mean_pct": float(valid.mean()),
        "return_median_pct": float(valid.median()),
        "return_p05_pct": float(valid.quantile(0.05)),
        "return_p95_pct": float(valid.quantile(0.95)),
        "max_drawdown_median_pct": float(pd.to_numeric(
            frame.max_drawdown_pct, errors="coerce").median()),
        "matched_intents": int(frame.matched_intents.sum()),
        "unmatched_intents": int(frame.unmatched_intents.sum()),
        "filled_buys": int(frame.filled_buys.sum()),
        "rejected_buys": int(frame.rejected_buys.sum()),
        "unable_exit_positions": int(frame.unable_exit_positions.sum()),
        "risk_rejected": int(frame.risk_rejected.sum()),
        "risk_reduced": int(frame.risk_reduced.sum()),
        "actual_return_pct": actual_return_pct,
    }
    attempts = summary["filled_buys"] + summary["rejected_buys"]
    matches = summary["matched_intents"] + summary["unmatched_intents"]
    summary["fill_rate"] = (summary["filled_buys"] / attempts
                            if attempts else np.nan)
    summary["unmatched_rate"] = (summary["unmatched_intents"] / matches
                                 if matches else np.nan)
    if actual_return_pct is None or not len(valid):
        summary["actual_percentile"] = np.nan
    else:
        below = int((valid < float(actual_return_pct)).sum())
        equal = int((valid == float(actual_return_pct)).sum())
        summary["actual_percentile"] = (below + 0.5 * equal) / len(valid)
    return summary
