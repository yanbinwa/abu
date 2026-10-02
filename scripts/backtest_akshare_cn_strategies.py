#!/usr/bin/env python3
"""Reproducible, local-only exploratory backtest of ABU's A-share factors.

Runs each scenario in an independent ABU account.  The sample is selected
before inspecting returns and shared across every scenario.  Results are
written outside the repository by default so source data remain untouched.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.errors import PerformanceWarning

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from abupy import abu, env, EMarketTargetType
from abupy.CoreBu.ABuEnv import EMarketDataFetchMode
from abupy.FactorBuyBu.ABuFactorBuyBreak import AbuFactorBuyBreak
from abupy.FactorBuyBu.ABuFactorBuyDM import AbuDoubleMaBuy
from abupy.FactorBuyBu.ABuFactorBuyTrend import (
    AbuDownUpTrend, AbuUpDownGolden, AbuUpDownTrend,
)
from abupy.FactorBuyBu.ABuFactorBuyWD import AbuFactorBuyWD
from abupy.FactorBuyBu.ABuFactorBuyDemo import (
    AbuSDBreak, AbuTwoDayBuy, AbuWeekMonthBuy,
)
from abupy.FactorBuyBu.ABuFactorBuyVolatilityHybrid import build_volatility_blend
from abupy.FactorSellBu.ABuFactorAtrNStop import AbuFactorAtrNStop
from abupy.FactorSellBu.ABuFactorCloseAtrNStop import AbuFactorCloseAtrNStop
from abupy.FactorSellBu.ABuFactorPreAtrNStop import AbuFactorPreAtrNStop
from abupy.FactorSellBu.ABuFactorSellBreak import AbuFactorSellBreak
from abupy.FactorSellBu.ABuFactorSellDM import AbuDoubleMaSell
from abupy.FactorSellBu.ABuFactorSellNDay import AbuFactorSellNDay
from abupy.BetaBu.ABuKellyPosition import AbuKellyPosition
from abupy.BetaBu.ABuPtPosition import AbuPtPosition
from abupy.PickStockBu.ABuPickStockPriceMinMax import AbuPickStockPriceMinMax
from abupy.PickStockBu.ABuPickRegressAngMinMax import AbuPickRegressAngMinMax
from abupy.PickStockBu.ABuPickStockDemo import AbuPickStockShiftDistance
from abupy.MarketBu.ABuDataFeedAkShare import use_akshare


START = "2025-10-02"
END = "2026-10-02"
INITIAL_CASH = 1_000_000
INDICES = ["sh000001", "sh000300", "sz399001", "sz399006"]


def prepare_snapshot(source_dir, snapshot_dir, symbols):
    """Freeze the requested interval even if another downloader updates the cache."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    start_date = int(START.replace("-", ""))
    end_date = int(END.replace("-", ""))
    total_rows = 0
    digest = hashlib.sha256()
    for symbol in symbols:
        destination = snapshot_dir / f"{symbol}_{start_date}_{end_date}"
        if not destination.exists():
            last_error = None
            for _ in range(10):
                candidates = sorted(source_dir.glob(f"{symbol}_*"))
                try:
                    if not candidates:
                        raise FileNotFoundError(symbol)
                    # Source may be replaced atomically while a longer download runs.
                    frame = pd.read_csv(candidates[-1])
                    frame = frame.loc[frame["date"].between(start_date, end_date)]
                    if frame.empty:
                        raise ValueError(f"no bars in requested interval: {symbol}")
                    temporary = destination.with_name(destination.name + ".tmp")
                    frame.to_csv(temporary, index=False)
                    os.replace(temporary, destination)
                    break
                except (FileNotFoundError, pd.errors.EmptyDataError) as error:
                    last_error = error
                    time.sleep(0.2)
            else:
                raise RuntimeError(f"could not snapshot {symbol}: {last_error}")
        data = destination.read_bytes()
        digest.update(symbol.encode())
        digest.update(data)
        total_rows += data.count(b"\n") - 1
    return {"files": len(symbols), "rows": total_rows,
            "sha256": digest.hexdigest(), "directory": str(snapshot_dir)}


def scenarios():
    hold20 = [{"class": AbuFactorSellNDay, "sell_n": 20}]
    hold60 = [{"class": AbuFactorSellNDay, "sell_n": 60}]
    break20 = [{"class": AbuFactorBuyBreak, "xd": 20}]
    entries = [
        ("break20", break20),
        ("break60", [{"class": AbuFactorBuyBreak, "xd": 60}]),
        ("ma_5_60", [{"class": AbuDoubleMaBuy, "fast": 5, "slow": 60}]),
        ("ma_dynamic", [{"class": AbuDoubleMaBuy}]),
        ("up_down", [{"class": AbuUpDownTrend, "xd": 20}]),
        ("up_down_golden", [{"class": AbuUpDownGolden, "xd": 20}]),
        ("down_up", [{"class": AbuDownUpTrend, "xd": 20}]),
        ("weekday", [{"class": AbuFactorBuyWD}]),
        ("sd_break", [{"class": AbuSDBreak, "xd": 20, "poly": 2}]),
        ("two_day", [{"class": AbuTwoDayBuy, "poly": 2}]),
        ("monthly", [{"class": AbuWeekMonthBuy, "is_buy_month": True}]),
        ("weekly", [{"class": AbuWeekMonthBuy, "is_buy_month": False}]),
    ]
    for name, buy in entries:
        yield "entry", name, buy, hold20

    exits = [
        ("hold60", hold60),
        ("break10", [{"class": AbuFactorSellBreak, "xd": 10}] + hold60),
        ("ma_5_60", [{"class": AbuDoubleMaSell, "fast": 5, "slow": 60}] + hold60),
        ("atr_loss1_win3", [{"class": AbuFactorAtrNStop,
                              "stop_loss_n": 1.0, "stop_win_n": 3.0}] + hold60),
        ("pre_atr_1_5", [{"class": AbuFactorPreAtrNStop,
                          "pre_atr_n": 1.5}] + hold60),
        ("close_atr_3", [{"class": AbuFactorCloseAtrNStop,
                          "close_atr_n": 3.0}] + hold60),
    ]
    for name, sell in exits:
        yield "exit", name, break20, sell

    positions = [
        ("kelly_default", {"class": AbuKellyPosition}),
        ("price_rank_20", {"class": AbuPtPosition, "past_day_cnt": 20}),
    ]
    for name, position in positions:
        yield "position", name, [{"class": AbuFactorBuyBreak, "xd": 20,
                                   "position": position}], hold20

    pickers = [
        ("price_5_100", {"class": AbuPickStockPriceMinMax, "xd": 20,
                         "threshold_price_min": 5, "threshold_price_max": 100}),
        ("positive_angle", {"class": AbuPickRegressAngMinMax, "xd": 20,
                            "threshold_ang_min": 0}),
        ("shift_distance", {"class": AbuPickStockShiftDistance, "xd": 20}),
    ]
    for name, picker in pickers:
        yield "picker", name, [{"class": AbuFactorBuyBreak, "xd": 20,
                                "stock_pickers": [picker]}], hold20

    for name, variable_size in (("volatility_blend", True),
                                ("volatility_blend_fixed", False)):
        buy, sell = build_volatility_blend(variable_size)
        yield "hybrid", name, buy, sell


def cn_commission(quantity, price):
    value = quantity * price
    return max(value * 0.00025, 5.0) + value * 0.0003


def simulate_orders(result, snapshot_dir, skip_locked_buys=False, slippage_bps=0,
                    max_gross_exposure=None, max_symbol_weight=None):
    """Execute paired order IDs, preventing sales of rejected buy orders."""
    calendar = result.benchmark.kl_pd["date"].astype(int).tolist()
    calendar_set = set(calendar)
    orders = result.orders_pd
    if orders is None or orders.empty:
        empty_curve = pd.DataFrame({"cash": INITIAL_CASH, "stocks": 0.0,
                                    "capital": INITIAL_CASH}, index=calendar)
        return empty_curve, 0, 0, 0.0, None
    orders = orders.reset_index(drop=True)
    if not (orders["buy_type_str"] == "call").all():
        raise ValueError("corrected account supports long-only A-share orders")

    events = {}
    for order_id, order in orders.iterrows():
        buy_date = int(order["buy_date"])
        if buy_date in calendar_set:
            events.setdefault(buy_date, []).append((1, order_id))
        if pd.notna(order["sell_date"]):
            sell_date = int(order["sell_date"])
            if sell_date in calendar_set:
                events.setdefault(sell_date, []).append((0, order_id))

    close_lookup = {}
    locked_lookup = {}
    for symbol in orders["symbol"].unique():
        frame = pd.read_csv(snapshot_dir / f"{symbol}_{START.replace('-', '')}_{END.replace('-', '')}",
                            usecols=["date", "close", "high", "low"])
        close_lookup[symbol] = frame.set_index("date")["close"].reindex(calendar).ffill().to_numpy()
        if skip_locked_buys:
            locked_lookup[symbol] = (frame.set_index("date")["high"]
                                     .eq(frame.set_index("date")["low"])
                                     .reindex(calendar).fillna(False).to_numpy())

    cash = float(INITIAL_CASH)
    active = {}  # order ID -> (symbol, quantity, total buy cost)
    records = []
    buys = sells = 0
    fees = 0.0
    realized_wins = []
    for day_i, date in enumerate(calendar):
        # On a shared date, close existing orders before admitting new ones.
        for action, order_id in sorted(events.get(date, [])):
            order = orders.iloc[order_id]
            symbol = order["symbol"]
            quantity = float(order["buy_cnt"])
            if action == 1:
                if skip_locked_buys and locked_lookup[symbol][day_i]:
                    continue
                buy_price = float(order["buy_price"]) * (1 + slippage_bps / 10000)
                fee = cn_commission(quantity, buy_price)
                cost = quantity * buy_price + fee
                if max_gross_exposure is not None or max_symbol_weight is not None:
                    holdings_value = 0.0
                    symbol_value = 0.0
                    for held_symbol, held_quantity, held_cost in active.values():
                        prior_close = close_lookup[held_symbol][day_i - 1] if day_i else np.nan
                        price = prior_close if np.isfinite(prior_close) else held_cost / held_quantity
                        value = held_quantity * price
                        holdings_value += value
                        if held_symbol == symbol:
                            symbol_value += value
                    equity = cash + holdings_value
                    if equity <= 0:
                        continue
                    if max_gross_exposure is not None and \
                            (holdings_value + quantity * buy_price) / equity > max_gross_exposure:
                        continue
                    if max_symbol_weight is not None and \
                            (symbol_value + quantity * buy_price) / equity > max_symbol_weight:
                        continue
                if quantity > 0 and cash >= cost:
                    cash -= cost
                    active[order_id] = (symbol, quantity, cost)
                    buys += 1
                    fees += fee
            elif order_id in active:
                symbol, quantity, cost = active.pop(order_id)
                sell_price = float(order["sell_price"]) * (1 - slippage_bps / 10000)
                fee = cn_commission(quantity, sell_price)
                proceeds = quantity * sell_price - fee
                cash += proceeds
                sells += 1
                fees += fee
                realized_wins.append(proceeds > cost)
        stocks = sum(quantity * (close_lookup[symbol][day_i]
                                 if np.isfinite(close_lookup[symbol][day_i]) else cost / quantity)
                     for symbol, quantity, cost in active.values())
        records.append((cash, stocks, cash + stocks))
    curve = pd.DataFrame(records, columns=["cash", "stocks", "capital"], index=calendar)
    win_rate = float(np.mean(realized_wins) * 100) if realized_wins else None
    return curve, buys, sells, fees, win_rate


def summarize(result, group, name, elapsed, sample_size, snapshot_dir,
              max_gross_exposure=None, max_symbol_weight=None):
    corrected, buys, sells, costs, realized_win_rate = simulate_orders(
        result, snapshot_dir, max_gross_exposure=max_gross_exposure,
        max_symbol_weight=max_symbol_weight)
    curve = corrected["capital"]
    peak = curve.cummax()
    midpoint = len(curve) // 2
    native_actions = result.action_pd
    native_buys = native_sells = 0
    if native_actions is not None and not native_actions.empty:
        native_deals = native_actions[native_actions["deal"].astype(bool)]
        native_buys = int((native_deals["action"] == "buy").sum())
        native_sells = int((native_deals["action"] == "sell").sum())
    orders = result.orders_pd
    closed = pd.DataFrame() if orders is None else orders.dropna(subset=["sell_price"])
    gross_win = float((closed["result"] == 1).mean()) if len(closed) else None
    native_curve = result.capital.capital_pd.get("capital_blance")
    annual = {}
    for year, values in curve.groupby(curve.index.astype(str).str[:4]):
        previous = INITIAL_CASH if year == str(curve.index[0])[:4] else curve.iloc[curve.index.get_loc(values.index[0]) - 1]
        annual[f"return_{year}_pct"] = round((values.iloc[-1] / previous - 1) * 100, 3)
    return {
        "group": group, "scenario": name, "sample_size": sample_size,
        "days": len(curve), "start": str(pd.to_datetime(str(curve.index[0])).date()),
        "end": str(pd.to_datetime(str(curve.index[-1])).date()),
        "portfolio_return_pct": round((curve.iloc[-1] / curve.iloc[0] - 1) * 100, 3),
        "max_drawdown_pct": round(((curve / peak) - 1).min() * 100, 3),
        "first_half_pct": round((curve.iloc[midpoint] / curve.iloc[0] - 1) * 100, 3),
        "second_half_pct": round((curve.iloc[-1] / curve.iloc[midpoint] - 1) * 100, 3),
        "executed_buys": buys, "executed_sells": sells,
        "executed_closed_win_rate_pct": None if realized_win_rate is None
        else round(realized_win_rate, 2),
        "generated_orders": 0 if orders is None else len(orders),
        "generated_closed_win_rate_pct": None if gross_win is None else round(gross_win * 100, 2),
        "end_stock_exposure_pct": round(corrected["stocks"].iloc[-1]
                                         / curve.iloc[-1] * 100, 3),
        "commission_cny": round(costs, 2), "elapsed_sec": round(elapsed, 2),
        "native_portfolio_return_pct": None if native_curve is None else
        round((native_curve.iloc[-1] / native_curve.iloc[0] - 1) * 100, 3),
        "native_executed_buys": native_buys, "native_executed_sells": native_sells,
        **annual,
        "error": None,
    }, corrected


def main():
    global START, END
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=400)
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--seed", type=int, default=20261002)
    parser.add_argument("--output-dir", type=Path,
                        default=Path("/Users/wjy/abu/backtests/akshare_cn_2025_2026"))
    parser.add_argument("--source-dir", type=Path,
                        default=Path("/Users/wjy/abu/data/csv"))
    parser.add_argument("--snapshot-dir", type=Path,
                        help="Reuse a previously frozen cache")
    parser.add_argument("--only", nargs="*", help="Optional scenario names")
    parser.add_argument("--only-groups", nargs="*", help="Optional scenario groups")
    parser.add_argument("--max-gross-exposure", type=float,
                        help="Optional cap on invested value / equity at buy time")
    parser.add_argument("--max-symbol-weight", type=float,
                        help="Optional cap on one symbol's value / equity at buy time")
    args = parser.parse_args()
    START, END = args.start, args.end
    for value in (args.max_gross_exposure, args.max_symbol_weight):
        if value is not None and not 0 < value <= 1:
            parser.error('exposure caps must be in (0, 1]')

    warnings.simplefilter("ignore", PerformanceWarning)
    use_akshare()
    env.g_market_target = EMarketTargetType.E_MARKET_TARGET_CN
    env.g_data_fetch_mode = EMarketDataFetchMode.E_DATA_FETCH_FORCE_LOCAL

    pool = pd.read_csv(Path(env.g_project_cache_dir) / "akshare_cn_stock_info.csv",
                       dtype={"symbol": str})["symbol"].drop_duplicates()
    if args.sample_size > len(pool):
        parser.error("sample size exceeds cached universe")
    sample = pool.sample(args.sample_size, random_state=args.seed).tolist()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = args.snapshot_dir or args.output_dir / "data_snapshot"
    snapshot = prepare_snapshot(args.source_dir, snapshot_dir,
                                pool.tolist() + INDICES)
    env.g_project_kl_df_data_csv = snapshot["directory"]
    (args.output_dir / "sample_symbols.txt").write_text("\n".join(sample) + "\n")
    print("snapshot", snapshot, flush=True)

    rows = []
    for group, name, buy, sell in scenarios():
        if args.only_groups and group not in args.only_groups:
            continue
        if args.only and name not in args.only:
            continue
        tic = time.monotonic()
        try:
            with open(os.devnull, "w") as sink, \
                    contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result, _ = abu.run_loop_back(
                    read_cash=INITIAL_CASH, buy_factors=buy, sell_factors=sell,
                    choice_symbols=sample, start=START, end=END,
                    n_process_kl=1, n_process_pick=1,
                )
            if result is None:
                raise RuntimeError("ABU returned no result")
            row, corrected = summarize(result, group, name, time.monotonic() - tic,
                                       len(sample), snapshot_dir,
                                       args.max_gross_exposure, args.max_symbol_weight)
            corrected.to_csv(args.output_dir / f"curve_{group}_{name}.csv", index_label="date")
            if result.orders_pd is not None:
                result.orders_pd[["buy_date", "buy_price", "buy_cnt", "buy_factor", "symbol",
                                  "sell_date", "sell_price", "sell_type_extra"]].to_csv(
                    args.output_dir / f"orders_{group}_{name}.csv", index=False)
        except Exception as error:
            row = {"group": group, "scenario": name, "sample_size": len(sample),
                   "error": f"{type(error).__name__}: {str(error)[:180]}",
                   "elapsed_sec": round(time.monotonic() - tic, 2)}
        rows.append(row)
        pd.DataFrame(rows).to_csv(args.output_dir / "results.csv", index=False)
        print(f"{group}/{name}: {row.get('portfolio_return_pct')}%, "
              f"buys={row.get('executed_buys')}, time={row['elapsed_sec']}s, "
              f"error={row['error']}", flush=True)

    benchmark_path = Path(env.g_project_kl_df_data_csv) / f"sh000001_{START.replace('-', '')}_{END.replace('-', '')}"
    benchmark = pd.read_csv(benchmark_path)
    benchmark_return = (benchmark["close"].iloc[-1] / benchmark["close"].iloc[0] - 1) * 100
    metadata = {
        "requested_start": START, "requested_end": END,
        "sample_size": len(sample), "seed": args.seed,
        "initial_cash_cny": INITIAL_CASH,
        "benchmark": "sh000001", "benchmark_return_pct": round(float(benchmark_return), 3),
        "fee_model": "ABU default China commission (see ABuCommission.py)",
        "fill_model": "ABU default next-day high-low midpoint, unless factor calls buy_today",
        "accounting": "paired order-ID cash account; same-day sells before buys; native result retained only for diagnosis",
        "universe": "current cached Shanghai and Shenzhen stock list; survivorship bias possible",
        "data_adjustment": "qfq except sh689009 CDR",
        "max_gross_exposure": args.max_gross_exposure,
        "max_symbol_weight": args.max_symbol_weight,
        "snapshot": snapshot,
    }
    (args.output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    print("benchmark", round(float(benchmark_return), 3), "pct", flush=True)


if __name__ == "__main__":
    main()
