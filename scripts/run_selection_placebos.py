#!/usr/bin/env python3
"""Generate matched placebo distributions for completed selection backtests."""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuSelectionStrategies import (  # noqa: E402
    STRATEGIES, SelectionPanel, run_matched_placebos,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--signal-dir', type=Path,
                        default=Path('/Users/wjy/abu/data/csv'))
    parser.add_argument('--research-data', type=Path,
                        default=Path('/Users/wjy/abu/data/selection_research'))
    parser.add_argument('--backtest-dir', type=Path,
                        default=Path('/Users/wjy/abu/backtests/selection_pit_2022_2026'))
    parser.add_argument('--years', type=int, nargs='+',
                        default=[2022, 2023, 2024, 2025, 2026])
    parser.add_argument('--replicates', type=int, default=500)
    parser.add_argument('--workers', type=int, default=3)
    parser.add_argument('--slippage-bps', type=int, default=25)
    args = parser.parse_args()

    panel = SelectionPanel.from_research_data(
        args.signal_dir, args.research_data, 20200101, 20261002)
    results = pd.read_csv(args.backtest_dir / 'results.csv')
    jobs = []
    for year in args.years:
        for strategy in STRATEGIES:
            result = results[(results.year == year) &
                             (results.strategy == strategy) &
                             (results.slippage_bps_per_side == args.slippage_bps)]
            trade_path = args.backtest_dir / 'trades_{}_{}_{}bps.csv'.format(
                strategy, year, args.slippage_bps)
            if result.empty or not trade_path.exists():
                continue
            trades = pd.read_csv(trade_path)
            if not (trades.side == 'buy').any():
                continue
            jobs.append((strategy, year, float(result.iloc[0].return_pct), trades))

    summaries = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(
                run_matched_placebos, panel, trades, year, actual_return,
                args.replicates,
                20261002 + year * 10 + STRATEGIES.index(strategy)):
            (strategy, year)
            for strategy, year, actual_return, trades in jobs
        }
        for future in as_completed(futures):
            strategy, year = futures[future]
            summary, distribution = future.result()
            summary.update({'strategy': strategy, 'year': year})
            summaries.append(summary)
            distribution.to_csv(
                args.backtest_dir / 'placebo_{}_{}_{}bps.csv'.format(
                    strategy, year, args.slippage_bps), index=False)
            print(strategy, year, 'percentile',
                  round(summary['actual_return_percentile'], 2), flush=True)
    pd.DataFrame(summaries).sort_values(['year', 'strategy']).to_csv(
        args.backtest_dir / 'placebo_summary.csv', index=False)


if __name__ == '__main__':
    main()
