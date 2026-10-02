#!/usr/bin/env python3
"""Backtest three explicit A-share selection strategies on frozen local bars."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuSelectionStrategies import (  # noqa: E402
    STRATEGIES, SelectionPanel, run_matched_placebos, run_selection_backtest,
)


def audit_ledger(curve, trades, panel):
    cash = 1_000_000.0
    active = {}
    max_error = 0.0
    by_date = {date: frame for date, frame in trades.groupby('date')}
    symbol_index = {symbol: position for position, symbol in enumerate(panel.symbols)}
    date_index = {int(date): position for position, date in enumerate(panel.dates)}
    first_day = date_index[int(curve.date.iloc[0])]
    last_price = pd.DataFrame(panel.exec_close[:first_day]).ffill().iloc[-1].to_numpy()
    for row in curve.itertuples(index=False):
        for trade in by_date.get(row.date, pd.DataFrame()).itertuples(index=False):
            value = trade.quantity * trade.price
            if trade.side == 'buy':
                if trade.symbol in active:
                    raise AssertionError('duplicate buy: ' + trade.symbol)
                active[trade.symbol] = (trade.quantity, trade.date)
                cash -= value + trade.fee
            elif trade.side == 'sell':
                if trade.symbol not in active:
                    raise AssertionError('sale without position: ' + trade.symbol)
                quantity, buy_date = active.pop(trade.symbol)
                if quantity != trade.quantity or buy_date >= trade.date:
                    raise AssertionError('quantity or T+1 violation: ' + trade.symbol)
                cash += value - trade.fee
            elif trade.side == 'cash_dividend':
                cash += trade.cash_flow
            elif trade.side == 'stock_dividend':
                if trade.symbol in active:
                    quantity, buy_date = active[trade.symbol]
                    active[trade.symbol] = (quantity + trade.quantity, buy_date)
                else:
                    active[trade.symbol] = (trade.quantity, trade.date)
        day = date_index[int(row.date)]
        observed = panel.exec_close[day]
        fresh = pd.notna(observed) & (observed > 0)
        last_price[fresh] = observed[fresh]
        independently_valued = sum(
            quantity * last_price[symbol_index[symbol]]
            for symbol, (quantity, _) in active.items())
        max_error = max(max_error, abs(cash - row.cash))
        if cash < -1e-6 or len(active) != row.holdings or \
                abs(row.capital - row.cash - row.stocks) > 1e-5 or \
                abs(independently_valued - row.stocks) > 0.05:
            raise AssertionError('account invariant failed on {}'.format(row.date))
    if max_error > 1e-5:
        raise AssertionError('cash reconciliation failed: {}'.format(max_error))
    return max_error


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot-dir', type=Path, default=Path(
        '/Users/wjy/abu/data/csv'))
    parser.add_argument('--stock-info', type=Path, default=Path(
        '/Users/wjy/abu/data/cache/akshare_cn_stock_info.csv'))
    parser.add_argument('--research-data', type=Path, default=Path(
        '/Users/wjy/abu/data/selection_research'))
    parser.add_argument('--output-dir', type=Path, default=Path(
        '/Users/wjy/abu/backtests/selection_pit_2022_2026'))
    parser.add_argument('--years', type=int, nargs='+',
                        default=[2022, 2023, 2024, 2025, 2026])
    parser.add_argument('--strategies', nargs='+', choices=STRATEGIES,
                        default=list(STRATEGIES))
    parser.add_argument('--slippage-bps', type=float, nargs='+', default=[0, 25])
    parser.add_argument('--placebo-replicates', type=int, default=500)
    parser.add_argument('--placebo-slippage-bps', type=float, default=25)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.research_data.exists():
        panel = SelectionPanel.from_research_data(
            args.snapshot_dir, args.research_data,
            start_date=20200101, end_date=20261002)
    else:
        panel = SelectionPanel.from_snapshot(args.snapshot_dir, args.stock_info)
    print('loaded', len(panel.symbols), 'stocks,', len(panel.dates), 'market dates', flush=True)
    rows = []
    for year in args.years:
        for strategy in args.strategies:
            for slippage in args.slippage_bps:
                result, curve, trades = run_selection_backtest(
                    panel, strategy, year, slippage)
                result['audit_max_cash_error_cny'] = audit_ledger(curve, trades, panel)
                row = {key: round(value, 3) if isinstance(value, float) else value
                       for key, value in result.items()}
                rows.append(row)
                label = '{}_{}_{}bps'.format(strategy, year, int(slippage))
                curve.to_csv(args.output_dir / ('curve_' + label + '.csv'), index=False)
                trades.to_csv(args.output_dir / ('trades_' + label + '.csv'), index=False)
                pd.DataFrame(rows).to_csv(args.output_dir / 'results.csv', index=False)
                print(label, 'return', row['return_pct'], 'drawdown',
                      row['max_drawdown_pct'], 'buys', row['buys'], flush=True)
                if (args.placebo_replicates > 0 and
                        slippage == args.placebo_slippage_bps):
                    summary, distribution = run_matched_placebos(
                        panel, trades, year, result['return_pct'],
                        replicates=args.placebo_replicates,
                        seed=20261002 + int(year) * 10 + STRATEGIES.index(strategy))
                    if summary:
                        summary.update({'strategy': strategy, 'year': int(year)})
                        placebo_path = args.output_dir / 'placebo_summary.csv'
                        existing = (pd.read_csv(placebo_path).to_dict('records')
                                    if placebo_path.exists() else [])
                        existing = [item for item in existing
                                    if not (item['strategy'] == strategy and
                                            int(item['year']) == int(year))]
                        pd.DataFrame(existing + [summary]).to_csv(
                            placebo_path, index=False)
                        distribution.to_csv(
                            args.output_dir / ('placebo_' + label + '.csv'), index=False)
    metadata = {
        'snapshot_dir': str(args.snapshot_dir),
        'stock_info': str(args.stock_info),
        'stock_count': len(panel.symbols),
        'market_dates': len(panel.dates),
        'first_market_date': int(panel.dates[0]),
        'last_market_date': int(panel.dates[-1]),
        'years': args.years,
        'strategies': args.strategies,
        'slippage_bps_per_side': args.slippage_bps,
        'initial_cash_cny': 1_000_000,
        'gross_cap': 0.8,
        'symbol_cap': 0.08,
        'signal_timing': 'adjusted close t; raw open t+1 execution',
        'fee_model': 'brokerage 0.025% min CNY 5 each side; transfer 0.001% each side; stamp 0.05% sell',
        'locked_bar': 'raw high <= low or no volume: cancel buy, defer sell',
        'accounting': 'single independent cash ledger per strategy and year',
        'universe': 'current plus exchange delisting lists active in the research window',
        'prices': 'qfq adjusted signals; unadjusted fills and valuation',
        'corporate_actions': 'CNINFO record date, gross cash and stock credits',
        'placebo': ('fixed dates, holding periods, notionals and costs; matched on '
                    'industry when available, price, liquidity, market cap, beta and volatility'),
        'known_limitation': 'Shanghai historical ST intervals remain incomplete',
    }
    (args.output_dir / 'metadata.json').write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    main()
