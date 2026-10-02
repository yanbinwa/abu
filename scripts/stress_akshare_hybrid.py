#!/usr/bin/env python3
"""Replay the saved hybrid orders with stricter fill assumptions."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import backtest_akshare_cn_strategies as backtest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, type=Path)
    args = parser.parse_args()
    backtest.START, backtest.END = '2021-10-02', '2026-10-02'
    data = args.root / 'data_snapshot'
    benchmark = pd.read_csv(next(data.glob('sh000001_*')), usecols=['date'])
    rows = []
    for seed in (20261002, 20261003, 20261004):
        folder = args.root / f'blend_causal_seed_{seed}'
        orders = pd.read_csv(folder / 'orders_hybrid_volatility_blend.csv')
        orders['buy_type_str'] = 'call'
        result = SimpleNamespace(orders_pd=orders,
                                 benchmark=SimpleNamespace(kl_pd=benchmark))
        cases = (('baseline', False, 0, None, None),
                 ('no_locked_25bps', True, 25, None, None),
                 ('capped_80_12', False, 0, .8, .12),
                 ('capped_80_12_no_locked_25bps', True, 25, .8, .12))
        for case, locked, bps, gross, symbol in cases:
            curve, buys, sells, fees, _ = backtest.simulate_orders(
                result, data, skip_locked_buys=locked, slippage_bps=bps,
                max_gross_exposure=gross, max_symbol_weight=symbol)
            row = {
                'seed': seed, 'scenario': 'volatility_blend', 'case': case,
                'return_pct': round((curve.capital.iloc[-1] / backtest.INITIAL_CASH - 1) * 100, 3),
                'max_drawdown_pct': round((curve.capital / curve.capital.cummax() - 1).min() * 100, 3),
                'average_exposure_pct': round((curve.stocks / curve.capital).mean() * 100, 3),
                'executed_buys': buys, 'executed_sells': sells,
                'fees_cny': round(fees, 2),
            }
            rows.append(row)
            print(row, flush=True)
    pd.DataFrame(rows).to_csv(args.root / 'hybrid_execution_sensitivity.csv', index=False)


if __name__ == '__main__':
    main()
