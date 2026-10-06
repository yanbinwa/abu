#!/usr/bin/env python3
"""Frozen stock-only risk-budget challenge; never claims fresh holdout evidence."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

BUDGET_FIELDS = (
    "single_trade_risk_fraction", "portfolio_open_risk_fraction",
    "industry_open_risk_fraction", "same_day_new_risk_fraction")
BACKTESTS = Path('/Users/wjy/abu/backtests')
STRICT = BACKTESTS / 'alpha158_canonical_price_volume_persistence_v2_20261006'
OLD = BACKTESTS / 'current_strategy_comparison_20261004_v2'


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    allow_nan=False, default=str) + '\n')


def scaled_risk(risk, multiplier):
    if multiplier not in (1.0, 2.0):
        raise ValueError('Only the two registered risk budgets are allowed')
    return replace(risk, **{name: getattr(risk, name) * multiplier
                           for name in BUDGET_FIELDS})


def curve_metrics(curve):
    capital = curve.capital.to_numpy(float)
    if not np.isfinite(capital).all() or np.any(capital <= 0):
        raise ValueError('Invalid NAV')
    dates = pd.to_datetime(curve.date.astype(str), format='%Y%m%d')
    if not dates.is_monotonic_increasing or dates.duplicated().any():
        raise ValueError('Curve dates must be unique and ordered')
    if not np.isclose(capital[0], 1e6, rtol=0, atol=1e-6):
        raise ValueError('Missing initial cash anchor')
    if not np.allclose(curve.cash + curve.stocks, capital, rtol=0, atol=1e-6):
        raise ValueError('Cash and holdings do not reconcile')
    years = (dates.iloc[-1] - dates.iloc[0]).days / 365.25
    if years <= 0:
        raise ValueError('Positive elapsed time required')
    return dict(return_pct=(capital[-1] / capital[0] - 1) * 100,
                cagr_pct=((capital[-1] / capital[0]) ** (1 / years) - 1) * 100,
                max_drawdown_pct=(1 - capital / np.maximum.accumulate(capital)).max() * 100,
                average_exposure_pct=float(curve.exposure.mean() * 100), years=years)


def paired_cagr_interval(base, candidate, years, block=20, replicates=5000, seed=20261006):
    base, candidate = np.asarray(base, float), np.asarray(candidate, float)
    if base.shape != candidate.shape or base.ndim != 1 or len(base) < 3 or years <= 0:
        raise ValueError('Aligned positive NAVs and elapsed years required')
    if not np.isfinite(np.r_[base, candidate]).all() or min(base.min(), candidate.min()) <= 0:
        raise ValueError('Positive finite NAVs required')
    if block < 1 or replicates < 1:
        raise ValueError('Positive bootstrap sizes required')
    r0, r1 = np.diff(np.log(base)), np.diff(np.log(candidate))
    n = len(r0)
    rng = np.random.default_rng(seed)
    indices = (rng.integers(0, n, (replicates, int(np.ceil(n / block)), 1)) + np.arange(block)) % n
    indices = indices.reshape(replicates, -1)[:, :n]
    delta = (np.exp(r1[indices].sum(axis=1) / years) -
             np.exp(r0[indices].sum(axis=1) / years)) * 100
    return dict(ci95_low_pp=float(np.quantile(delta, .025)),
                ci95_high_pp=float(np.quantile(delta, .975)),
                interpretation='Paired observed-history CAGR diagnostic; does not correct prior experiment selection.')


def validate_folds(manifests, predictions):
    if predictions.duplicated(['signal_asof', 'symbol']).any():
        raise ValueError('Duplicate OOS predictions')
    if set(predictions.fold.unique()) != {f['fold'] for f in manifests}:
        raise ValueError('Prediction and manifest folds differ')
    for fold in manifests:
        if not (fold['train_label_end'] < fold['validation_start'] and
                fold['validation_label_end'] < fold['test_start']):
            raise ValueError('Label boundary overlap')
        rows = predictions[predictions.fold.eq(fold['fold'])]
        if not rows.signal_asof.between(fold['test_start'], fold['test_end']).all():
            raise ValueError('Prediction outside its OOS fold')
        if not rows.train_end.eq(fold['train_end']).all():
            raise ValueError('Prediction training boundary mismatch')


def freeze(args):
    from scripts.backtest_alpha158_event_exit_only_2025_2026 import data_paths
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    runtime = output / 'runtime'
    for name in ('abupy', 'scripts', 'configs/selection'):
        shutil.copytree(ROOT / name, runtime / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    inputs = output / 'inputs'
    inputs.mkdir()
    sources = {
        'strict.csv.gz': STRICT / 'oos_predictions.csv.gz',
        'strict_folds.json': STRICT / 'fold_manifests.json',
        'strict_report.json': STRICT / 'report.json',
        'older.csv.gz': OLD / 'inputs/alpha.csv.gz',
    }
    for name, source in sources.items():
        shutil.copy2(source, inputs / name)
    files = {str(p): digest(p) for p in data_paths(args.signal_dir, args.research_dir)}
    config = json.loads(args.config.read_text())
    if config['scaled_fields'] != list(BUDGET_FIELDS) or config['risk_multipliers'] != [1., 2.]:
        raise ValueError('Research specification changed')
    write_json(output / 'registration.json', dict(
        registered_at=datetime.now().astimezone().isoformat(), experiment=config,
        input_sources={name: str(p) for name, p in sources.items()},
        input_hashes={str(p): digest(p) for p in inputs.iterdir()},
        market_hashes=files, signal_dir=str(args.signal_dir), research_dir=str(args.research_dir),
        runtime_hashes={str(p): digest(p) for p in runtime.rglob('*') if p.is_file()},
        candidate_count=1, parameter_search=False, observed_history=True, new_holdout=False,
        primary_prediction_column='ridge_score',
        excluded_prediction_columns=['candidate_score', 'target_rank', 'excess_return_20d'],
        stock_signal_rule='unchanged; close signal then next-session execution',
        admission=False))
    os.execv(sys.executable, [sys.executable, '-B', str(runtime / 'scripts' / Path(__file__).name),
                            '--output', str(output), '--frozen'])


def run(output):
    from abupy.AlphaBu.ABuAlpha158Lite import load_alpha158_lite_config, load_alpha158_lite_low_turnover_config
    from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config
    from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
    from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview
    from scripts.backtest_alpha158_lite_v1 import rank_frame
    from scripts.backtest_alpha158_lite_low_turnover_v3 import run_low_turnover, annual_returns
    from scripts.backtest_alpha158_event_exit_only_2025_2026 import save_audit

    registration = json.loads((output / 'registration.json').read_text())
    hashes = {**registration['input_hashes'], **registration['runtime_hashes'], **registration['market_hashes']}
    if any(digest(p) != expected for p, expected in hashes.items()):
        raise ValueError('Registered inputs changed')
    config = registration['experiment']
    source = load_alpha158_lite_config(ROOT / 'configs/selection/alpha158_lite_v1.json')
    policy = load_alpha158_lite_low_turnover_config(ROOT / 'configs/selection/alpha158_lite_low_turnover_v3.json')
    risk = load_risk_config(ROOT / 'configs/selection/risk_v1.json')
    if source.sha256 != policy.source_config_sha256:
        raise ValueError('Policy/source mismatch')
    print('Loading registered PIT market panel', flush=True)
    panel = SelectionPanelV2.from_research_data(registration['signal_dir'], registration['research_dir'],
                                               start_date=20200101, end_date=20260930)
    rows, curves, annual = [], {}, []
    for boundary, column, costs in (
            ('strict', 'ridge_score', config['primary_slippage_bps']),
            ('older', 'alpha_score', config['boundary_sensitivity_slippage_bps'])):
        columns = ['signal_asof', 'symbol', 'column', column]
        if boundary == 'strict':
            columns += ['fold', 'train_end']
        predictions = pd.read_csv(output / 'inputs' / (boundary + '.csv.gz'), usecols=columns)
        if boundary == 'strict':
            validate_folds(json.loads((output / 'inputs/strict_folds.json').read_text()), predictions)
        mapped = predictions.symbol.map(panel.symbol_index)
        if mapped.isna().any() or not np.array_equal(mapped.to_numpy(), predictions.column.to_numpy()):
            raise ValueError('Prediction security index differs from market panel')
        scores = rank_frame(predictions.rename(columns={column: 'alpha_score'}), 'alpha_score',
                            max(policy.entry_rank_limit, policy.retention_rank_limit))
        for cost in costs:
            for multiplier in config['risk_multipliers']:
                key = f'{boundary}_{int(cost)}bp_{int(multiplier)}x'
                result, audit = run_low_turnover(
                    panel, scores, replace(source, label_slippage_bps=cost), policy,
                    scaled_risk(risk, multiplier), 20260930, sync_dynamic_stops=False,
                    review_overlay=CostAwareReview(suppress_rank_exits=True))
                curve = audit['curve'].copy()
                first = int(scores.signal_asof.min())
                if int(curve.date.iloc[0]) != first:
                    anchor = {name: 0. for name in curve.columns}
                    anchor.update(date=first, cash=1e6, capital=1e6)
                    for name in curve.columns:
                        if name.startswith('liquidation_nav_'):
                            anchor[name] = 1e6
                    curve = pd.concat([pd.DataFrame([anchor]), curve], ignore_index=True)
                curve['date'] = curve.date.astype(int)
                audit['curve'] = curve
                expected_dates = panel.dates[(panel.dates >= first) & (panel.dates <= 20260930)]
                if not np.array_equal(curve.date.to_numpy(), expected_dates):
                    raise ValueError('Incomplete curve calendar')
                stats = curve_metrics(curve)
                if boundary == 'strict' and cost == 25 and multiplier == 1:
                    previous = json.loads((output / 'inputs/strict_report.json').read_text())['portfolio']['ridge']
                    if not np.isclose(stats['return_pct'], previous['return_pct'], atol=1e-7, rtol=0):
                        raise ValueError('Strict baseline reproduction failed')
                save_audit(output / key, audit)
                write_json(output / key / 'executor_metrics.json', result)
                curves[key] = curve
                rows.append(dict(key=key, boundary=boundary, slippage_bps=cost, multiplier=multiplier,
                                 filled_buys=result['filled_buys'], **stats))
                year = annual_returns(curve)
                year.insert(0, 'key', key)
                annual.append(year)
                pd.DataFrame(rows).to_csv(output / 'results.csv', index=False)
                print(json.dumps(rows[-1]), flush=True)

    annual = pd.concat(annual, ignore_index=True)
    annual.to_csv(output / 'annual_returns.csv', index=False)
    pairs = []
    for boundary, costs in (('strict', config['primary_slippage_bps']),
                             ('older', config['boundary_sensitivity_slippage_bps'])):
        for cost in costs:
            bkey, ckey = (f'{boundary}_{int(cost)}bp_{m}x' for m in (1, 2))
            b, c = (next(r for r in rows if r['key'] == key) for key in (bkey, ckey))
            bc, cc = curves[bkey], curves[ckey]
            if not np.array_equal(bc.date, cc.date):
                raise ValueError('Paired calendars differ')
            by, cy = (annual[annual.key.eq(key)].set_index('year').return_pct for key in (bkey, ckey))
            pair = dict(boundary=boundary, slippage_bps=cost,
                        cagr_delta_pp=c['cagr_pct'] - b['cagr_pct'],
                        drawdown_worsening_pp=c['max_drawdown_pct'] - b['max_drawdown_pct'],
                        positive_years=int((cy - by > 0).sum()),
                        **paired_cagr_interval(bc.capital, cc.capital, b['years'],
                            block=config['bootstrap_block_sessions'], replicates=config['bootstrap_replicates'],
                            seed=config['bootstrap_seed']))
            pairs.append(pair)
    pd.DataFrame(pairs).to_csv(output / 'paired_results.csv', index=False)
    primary = pairs[0]
    passed = (all(p['cagr_delta_pp'] >= config['target_net_cagr_delta_pp'] and
                  p['drawdown_worsening_pp'] <= config['max_drawdown_worsening_pp'] for p in pairs)
              and primary['ci95_low_pp'] > 0 and primary['positive_years'] >= config['minimum_positive_years'])
    if any(digest(p) != expected for p, expected in hashes.items()):
        raise ValueError('Registered inputs changed during replay')
    write_json(output / 'report.json', dict(
        decision='HISTORICAL_SCREEN_PASS_ONLY' if passed else 'REJECT_HISTORICAL_SCREEN',
        target='Stock-only net CAGR +1 percentage point', results=rows, pairs=pairs,
        strict_baseline_reproduced=True, hashes_unchanged=True,
        observed_history=True, new_holdout=False, live_admitted=False,
        limitations=['Risk-budget increase is not stock-selection alpha.',
                     'Historical experiments already viewed this period; no claim of absence of overfitting.',
                     'Only original Ridge scores are used; failed canonical features and future labels are excluded.',
                     'Daily execution and existing lifecycle/corporate-action source limitations remain.',
                     'Older training boundary is a sensitivity diagnostic, not primary clean evidence.']))
    print('COMPLETE ' + ('HISTORICAL_SCREEN_PASS_ONLY' if passed else 'REJECT_HISTORICAL_SCREEN'), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/annual_uplift_challenge_20261006')
    parser.add_argument('--config', type=Path, default=ROOT / 'configs/selection/alpha158_annual_uplift_challenge_v1.json')
    parser.add_argument('--signal-dir', type=Path, default=Path('/Users/wjy/abu/data/csv'))
    parser.add_argument('--research-dir', type=Path, default=Path('/Users/wjy/abu/data/selection_research'))
    parser.add_argument('--frozen', action='store_true')
    args = parser.parse_args()
    run(args.output.resolve()) if args.frozen else freeze(args)
