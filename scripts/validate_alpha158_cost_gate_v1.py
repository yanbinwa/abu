#!/usr/bin/env python3
"""Post-hoc full-period robustness for the unchanged no-rank-exit ablation.

The primary cost-gate experiment starts after calibration warmup. This explicitly
secondary diagnostic restores 2023 and reports paired block uncertainty. It does
not select new parameters, constitute a holdout, or authorize paper deployment.
"""
from __future__ import annotations
import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from abupy.AlphaBu.ABuArtifactManifest import sha256_file
from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview
from abupy.AlphaBu.ABuAlpha158Lite import load_alpha158_lite_config, load_alpha158_lite_low_turnover_config
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuTrialRegistry import register_trial
from scripts.backtest_alpha158_lite_low_turnover_v3 import run_low_turnover, annual_returns
from scripts.backtest_alpha158_lite_v1 import rank_frame, _save_audit


def paired_block_interval(base_nav, candidate_nav, seed=20261004, replicates=5000, block=20):
    base = np.asarray(base_nav,dtype=float)
    candidate = np.asarray(candidate_nav,dtype=float)
    if len(base) != len(candidate) or min(len(base),len(candidate)) < 3:
        raise ValueError('aligned NAVs required')
    if not np.isfinite(np.r_[base,candidate]).all() or min(base.min(),candidate.min()) <= 0:
        raise ValueError('positive finite NAV required')
    r0,r1 = np.diff(np.log(base)),np.diff(np.log(candidate))
    n=len(r0)
    rng=np.random.default_rng(seed)
    indices=(rng.integers(0,n,(replicates,int(np.ceil(n/block)),1))+np.arange(block))%n
    indices=indices.reshape(replicates,-1)[:,:n]
    delta=(np.exp(r1[indices].sum(axis=1))-np.exp(r0[indices].sum(axis=1)))*100
    return dict(block_sessions=block,replicates=replicates,seed=seed,
        observed_increment_pp=float((candidate[-1]/candidate[0]-base[-1]/base[0])*100),
        ci95_low_pp=float(np.quantile(delta,.025)),ci95_high_pp=float(np.quantile(delta,.975)),
        resampled_nonpositive_fraction=float((delta<=0).mean()),
        interpretation='Paired post-hoc path diagnostic, not a null p-value or independent validation.')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--primary-dir',type=Path,default=Path('/Users/wjy/abu/backtests/alpha158_cost_gate_v1_20261004'))
    args=parser.parse_args()
    primary=args.primary_dir
    manifest=json.loads((primary/'input_manifest.json').read_text())
    report=json.loads((primary/'report.json').read_text())
    if not report['inputs_unchanged'] or not report['golden_passed']:
        raise ValueError('primary run is not valid')
    inputs=manifest['source_files']
    changed=[p for p,h in inputs.items() if sha256_file(p)!=h]
    if changed:
        raise ValueError('primary inputs have changed: '+repr(changed))
    directory=primary/'full_period_robustness'
    directory.mkdir(exist_ok=False)
    validation_hash=sha256_file(Path(__file__))
    register_trial(directory/'trial_registry.jsonl','unchanged-no-rank-full-period',
        'Primary calibration warmup omitted 2023. Re-evaluate the unchanged simpler ablation on the entire saved OOS period at all three costs.',
        dict(parent_manifest_sha256=sha256_file(primary/'input_manifest.json'),
             code_sha256=validation_hash,post_hoc=True,parameter_search=False,
             slippage_scenarios=[25,40,60],block_sessions=20,replicates=5000,seed=20261004))
    source=load_alpha158_lite_config(ROOT/'configs/selection/alpha158_lite_v1.json')
    policy=load_alpha158_lite_low_turnover_config(ROOT/'configs/selection/alpha158_lite_low_turnover_v3.json')
    risk=load_risk_config(ROOT/'configs/selection/risk_v1.json')
    predictions_path=next(Path(p) for p in inputs if p.endswith('oos_predictions.csv.gz'))
    predictions=pd.read_csv(predictions_path,usecols=['signal_asof','symbol','column','alpha_score'])
    scores=rank_frame(predictions,'alpha_score',100)
    print('Loading unchanged PIT panel for full-period diagnostic...',flush=True)
    panel=SelectionPanelV2.from_research_data('/Users/wjy/abu/data/csv',
        '/Users/wjy/abu/data/selection_research',start_date=20200101,end_date=20260930)
    rows,annual,uncertainty=[],[],[]
    for slip in (25,40,60):
        curves={}
        for arm in ('baseline','no_rank_exit'):
            print(f'Full-period {arm} {slip}bp...',flush=True)
            overlay=CostAwareReview(suppress_rank_exits=True) if arm=='no_rank_exit' else None
            result,audit=run_low_turnover(panel,scores,replace(source,label_slippage_bps=slip),
                replace(policy,strategy_version='alpha158_price_only_'+arm+'_v1'),risk,20260930,
                review_overlay=overlay)
            run=f'{arm}_{slip}bp'
            result.update(arm=arm,run=run)
            rows.append(result); curves[arm]=audit['curve'].set_index('date').capital
            _save_audit(directory/run,audit)
            yr=annual_returns(audit['curve']);yr['run']=run;annual.append(yr)
            print(json.dumps({k:result[k] for k in ('run','return_pct','max_drawdown_pct','filled_buys')}),flush=True)
        assert curves['baseline'].index.equals(curves['no_rank_exit'].index)
        uncertainty.append(dict(slippage_bps=slip,**paired_block_interval(
            curves['baseline'].to_numpy(),curves['no_rank_exit'].to_numpy())))
    pd.DataFrame(rows).to_csv(directory/'results.csv',index=False)
    pd.concat(annual).to_csv(directory/'annual_returns.csv',index=False)
    pd.DataFrame(uncertainty).to_csv(directory/'paired_block_uncertainty.csv',index=False)
    changed=[p for p,h in inputs.items() if sha256_file(p)!=h]
    if sha256_file(Path(__file__))!=validation_hash:
        changed.append(str(Path(__file__)))
    result=dict(research_only=True,post_hoc=True,inputs_unchanged=not changed,
                changed_inputs=changed,uncertainty=uncertainty,
                decision='NO_AUTOMATIC_ADMISSION; assess primary gates and full-period diagnostics together')
    (directory/'report.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2),flush=True)
    if changed:
        raise RuntimeError('inputs changed during validation')


if __name__=='__main__':
    main()
