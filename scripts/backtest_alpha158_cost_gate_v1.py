#!/usr/bin/env python3
"""Frozen price-only portfolio ablations with PIT calibration and cost stress."""
from __future__ import annotations
import argparse
import json
import subprocess
import sys
from dataclasses import asdict, fields, replace
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from abupy.AlphaBu.ABuArtifactManifest import sha256_file
from abupy.AlphaBu.ABuCostAwareAlpha import CalibrationConfig, PastScoreCalibration, CostAwareReview
from abupy.AlphaBu.ABuAlpha158Lite import load_alpha158_lite_config, load_alpha158_lite_low_turnover_config
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuTrialRegistry import register_trial
from scripts.backtest_alpha158_lite_low_turnover_v3 import run_low_turnover, annual_returns
from scripts.backtest_alpha158_lite_v1 import rank_frame, _save_audit


def fingerprint(paths):
    return {str(path.resolve()): sha256_file(path) for path in sorted(set(paths)) if path.is_file()}


def assert_golden(audit, original):
    saved = pd.read_csv(original/'daily_nav.csv')
    pd.testing.assert_frame_equal(audit['curve'][saved.columns].reset_index(drop=True), saved,
                                  check_dtype=False, rtol=1e-11, atol=1e-7)
    fields = ['date', 'symbol', 'side', 'status', 'quantity', 'reference_price',
              'fill_price_raw', 'commission', 'transfer_fee', 'stamp_tax', 'slippage_cost']
    saved = pd.read_csv(original/'fills.csv')
    pd.testing.assert_frame_equal(audit['fills'][fields].reset_index(drop=True), saved[fields],
                                  check_dtype=False, rtol=1e-11, atol=1e-7)


def universe_diagnostic(panel, predictions, dates):
    """Lagged eligible-pool gross reference; not an investable portfolio."""
    groups = {int(d): g.column.to_numpy(dtype=int)
              for d,g in predictions.groupby('signal_asof')}
    lookup = {int(d):i for i,d in enumerate(panel.dates)}
    # Carry suspended marks, but count missing current quotes explicitly.
    marks = pd.DataFrame(panel.close).ffill().to_numpy()
    rows = []
    for date in dates:
        day = lookup[int(date)]
        columns = groups.get(int(panel.dates[day-1]), np.array([], dtype=int)) if day else []
        if not len(columns):
            rows.append(dict(date=int(date), gross_return=0., constituents=0, missing_quotes=0))
            continue
        valid = np.isfinite(marks[day-1,columns]) & (marks[day-1,columns] > 0)
        values = marks[day,columns[valid]]/marks[day-1,columns[valid]]-1
        values = np.where(np.isfinite(values), values, 0.)
        rows.append(dict(date=int(date), gross_return=float(values.mean()) if len(values) else 0.,
            constituents=len(columns), missing_quotes=int((~np.isfinite(panel.close[day,columns])).sum())))
    result = pd.DataFrame(rows).set_index('date')
    result.loc[result.index[0],'gross_return'] = 0.
    return result


def comparison_metrics(curve, benchmark, universe):
    capital = curve.set_index('date').capital
    nav = capital/capital.iloc[0]
    r = nav.pct_change().fillna(0.)
    market = benchmark.reindex(nav.index).pct_change().fillna(0.)
    vol = r.iloc[1:].std()*np.sqrt(252)
    scale = r.iloc[1:].std()/market.iloc[1:].std()
    matched = (1+market*scale).cumprod()
    lag_exposure = curve.set_index('date').exposure.shift(1).fillna(0.)
    pool = (1+universe.reindex(nav.index).gross_return*lag_exposure).cumprod()
    years = (pd.Timestamp(str(nav.index[-1]))-pd.Timestamp(str(nav.index[0]))).days/365.25
    return dict(cagr_pct=(nav.iloc[-1]**(1/years)-1)*100,
        annual_vol_pct=vol*100, sharpe_rf0=r.iloc[1:].mean()*252/vol if vol else None,
        ex_post_vol_matched_index_weight=scale,
        ex_post_vol_matched_index_return_pct=(matched.iloc[-1]-1)*100,
        ex_post_vol_matched_index_drawdown_pct=(matched/matched.cummax()-1).min()*100,
        lag_exposure_pool_gross_return_pct=(pool.iloc[-1]-1)*100,
        lag_exposure_pool_gross_drawdown_pct=(pool/pool.cummax()-1).min()*100)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path('/Users/wjy/abu/backtests/alpha158_cost_gate_v1_20261004'))
    parser.add_argument('--predictions', type=Path, default=Path('/Users/wjy/abu/backtests/alpha158_lite_v1_hardened_20261003/oos_predictions.csv.gz'))
    parser.add_argument('--signal-dir', type=Path, default=Path('/Users/wjy/abu/data/csv'))
    parser.add_argument('--research-dir', type=Path, default=Path('/Users/wjy/abu/data/selection_research'))
    parser.add_argument('--end-date', type=int, default=20260930)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    config_paths = [ROOT/'configs/selection'/name for name in (
        'alpha158_cost_gate_v1.json', 'alpha158_lite_v1.json',
        'alpha158_lite_low_turnover_v3.json', 'risk_v1.json')]
    config = json.loads(config_paths[0].read_text())
    source = load_alpha158_lite_config(config_paths[1])
    policy = load_alpha158_lite_low_turnover_config(config_paths[2])
    risk = load_risk_config(config_paths[3])
    research_report = args.predictions.parent/'research_report.json'
    if source.sha256 != policy.source_config_sha256 or json.loads(research_report.read_text())['config_sha256'] != source.sha256:
        raise ValueError('source/prediction configuration mismatch')
    calibration_config = CalibrationConfig(**{f.name:config[f.name] for f in fields(CalibrationConfig)})
    print('Freezing input data and code fingerprints...', flush=True)
    data_paths = list(args.signal_dir.glob('sh*'))+list(args.signal_dir.glob('sz*'))
    for directory in ('raw', 'signal_extra'):
        data_paths += list((args.research_dir/directory).glob('*'))
    data_paths += [args.research_dir/name for name in (
        'security_master.csv','corporate_actions.csv','industry_changes.csv','sz_name_changes.csv')]
    code_paths = [Path(module.__file__) for module in list(sys.modules.values())
                  if getattr(module,'__file__',None) and str(ROOT) in str(module.__file__)
                  and '/.venv/' not in str(module.__file__) and str(module.__file__).endswith('.py')]
    inputs = fingerprint(data_paths+config_paths+code_paths+[args.predictions,research_report,
        args.predictions.parent/'fold_manifests.json', Path(__file__)])
    configuration = dict(config=config, source=asdict(source), policy=asdict(policy), risk=asdict(risk),
        end_date=args.end_date, source_files=inputs,
        git_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        observed_history=True, new_holdout=False, dynamic_stop_sync=False,
        calibration_start_rule='first signal date with 126 strictly matured OOS dates',
        deployment='research_only_no_paper_order_effect')
    (args.output_dir/'input_manifest.json').write_text(json.dumps(configuration, indent=2))
    registry = args.output_dir/'trial_registry.jsonl'
    trial = 'alpha158-price-only-cost-portfolio-v1'
    register_trial(registry, trial,
        'Cost-aware voluntary replacement and separately diversified holdings can improve net return without relaxing risk; compare all frozen arms and costs.',
        configuration)
    print('Loading frozen OOS predictions and PIT execution panel...', flush=True)
    predictions = pd.read_csv(args.predictions, usecols=['signal_asof','symbol','column','alpha_score','excess_return_20d','train_end'],dtype={'symbol':str})
    predictions = predictions[predictions.signal_asof <= args.end_date].copy()
    panel = SelectionPanelV2.from_research_data(args.signal_dir, args.research_dir,
                                              start_date=20200101,end_date=args.end_date)
    symbol_mapping = predictions[['symbol','column']].drop_duplicates()
    if any(panel.symbols[int(row.column)] != row.symbol for row in symbol_mapping.itertuples()):
        raise ValueError('prediction symbol-column mapping mismatch')
    calibration = PastScoreCalibration(predictions, panel.dates, calibration_config)
    start = calibration.first_ready_date()
    scores = rank_frame(predictions, policy.score_column, policy.retention_rank_limit)
    print('Replaying full original baseline for golden check...', flush=True)
    golden_result, golden_audit = run_low_turnover(panel, scores, source, policy,risk,args.end_date)
    original = Path('/Users/wjy/abu/backtests/alpha158_lite_low_turnover_v3/alpha158_lite_low_turnover_v3')
    if args.end_date != 20260930:
        raise ValueError('golden comparison is frozen to 20260930')
    assert_golden(golden_audit, original)
    (args.output_dir/'golden_check.json').write_text(json.dumps(dict(passed=True,result=golden_result),indent=2))
    print(f'Golden NAV/fills passed. Common cash start: {start}', flush=True)
    scores = scores[scores.signal_asof >= start].copy()
    dates = panel.dates[(panel.dates >= start)&(panel.dates <= args.end_date)]
    universe = universe_diagnostic(panel,predictions,dates)
    universe.to_csv(args.output_dir/'eligible_pool_diagnostic.csv')
    benchmark = pd.Series(panel.benchmark_close,index=panel.dates)
    rows, years = [], []
    for slip in config['slippage_scenarios_bps']:
        for arm in config['experiments']:
            name = f'{arm}_{slip:g}bp'
            print(f'Running {name}...',flush=True)
            run_policy = replace(policy, strategy_version='alpha158_price_only_'+arm+'_v1',
                                 target_positions=20 if arm == 'diversified_20' else 10)
            overlay = (CostAwareReview(calibration) if arm == 'cost_gate' else
                       CostAwareReview(suppress_rank_exits=True) if arm == 'no_rank_exit' else None)
            result,audit = run_low_turnover(panel,scores,replace(source,label_slippage_bps=slip),
                run_policy,risk,args.end_date,review_overlay=overlay)
            curve = audit['curve']
            assert (curve.capital-curve.cash-curve.stocks).abs().max() < 1e-6
            assert curve.cash.min() >= -1e-7
            directory = args.output_dir/name
            _save_audit(directory,audit)
            if overlay is not None:
                pd.DataFrame(overlay.decisions).to_csv(directory/'cost_decisions.csv',index=False)
            result.update(comparison_metrics(curve,benchmark,universe))
            result.update(arm=arm,run=name)
            rows.append(result)
            yr = annual_returns(curve); yr['run']=name; years.append(yr)
            pd.DataFrame(rows).to_csv(args.output_dir/'results.csv',index=False)
            print(json.dumps({k:result[k] for k in ('run','return_pct','max_drawdown_pct','filled_buys','average_exposure_pct')}),flush=True)
    results = pd.DataFrame(rows)
    gates = []
    for arm in config['experiments'][1:]:
        candidate = results[results.arm == arm].set_index('slippage_bps')
        baseline = results[results.arm == 'baseline'].set_index('slippage_bps')
        gates.append(dict(arm=arm,
            all_costs_return_improved=bool((candidate.return_pct > baseline.return_pct).all()),
            all_costs_drawdown_not_worse=bool((candidate.max_drawdown_pct >= baseline.max_drawdown_pct).all()),
            all_costs_stress_return_positive=bool((candidate.liquidation_3_limits_return_pct > 0).all()),
            all_costs_positive_return=bool((candidate.return_pct > 0).all())))
    pd.concat(years).to_csv(args.output_dir/'annual_returns.csv',index=False)
    print('Verifying frozen inputs after replay...',flush=True)
    changed = [path for path,digest in inputs.items() if not Path(path).is_file() or sha256_file(path) != digest]
    report = dict(research_only=True,post_hoc_diagnostic=True,common_start=start,end=args.end_date,
        golden_passed=True,inputs_unchanged=not changed,changed_inputs=changed,gates=gates,
        status='HISTORICAL_DIAGNOSTIC_COMPLETE' if not changed else 'INVALIDATED_INPUT_CHANGED',
        benchmark_limitations='Price index excludes dividends. Pool is lagged eligible daily equal weight, gross and non-executable; missing prices carried, no delisting recovery. Matched volatility is ex post. Cash rf=0. No statistical admission or forward deployment.')
    (args.output_dir/'report.json').write_text(json.dumps(report,indent=2))
    register_trial(registry,trial+'-observed','All frozen arms completed; no parameter selection.',
                   configuration,status='OBSERVED' if not changed else 'INVALIDATED',observed_metrics=report,parent_trial_id=trial)
    print(json.dumps(report,indent=2),flush=True)
    if changed:
        raise RuntimeError('inputs changed during experiment; result invalidated')


if __name__ == '__main__':
    main()
