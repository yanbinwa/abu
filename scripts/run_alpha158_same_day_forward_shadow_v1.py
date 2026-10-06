#!/usr/bin/env python3
"""Run a paired prospective same-day-risk shadow; never send real orders."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from dataclasses import replace
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from abupy.AlphaBu import ABuAlphaForwardShadow as shadow
from abupy.AlphaBu.ABuAlpha158Lite import (
    load_alpha158_lite_config, load_alpha158_lite_low_turnover_config,
)
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config
from scripts import run_alpha158_forward_shadow_v1 as base


DEFAULT_ROOT = Path('/Users/wjy/abu/shadow/alpha158_same_day_risk_forward_v1')
PROTOCOL = 'alpha158_same_day_forward_shadow_v1.json'


def initialize(root,signal,research):
    if root.exists():
        raise FileExistsError('experiment already exists; never reset it')
    started = shadow.now_shanghai()
    staging = root.parent/('.'+root.name+'.init-'+uuid.uuid4().hex)
    staging.mkdir(parents=True)
    protocol = json.loads((ROOT/'configs/selection'/PROTOCOL).read_text())
    shadow.validate_protocol(protocol)
    source = load_alpha158_lite_config(
        ROOT/'configs/selection/alpha158_lite_v1.json')
    policy = load_alpha158_lite_low_turnover_config(
        ROOT/'configs/selection/alpha158_lite_low_turnover_v3.json')
    risk = load_risk_config(ROOT/'configs/selection/risk_v1.json')
    if source.sha256 != policy.source_config_sha256:
        raise ValueError('source and policy configuration do not match')
    specs = protocol['accounts']
    if specs['current']['same_day_new_risk_fraction'] != \
            risk.same_day_new_risk_fraction:
        raise ValueError('current account differs from frozen risk baseline')
    if specs['same_day_release']['same_day_new_risk_fraction'] != \
            risk.portfolio_open_risk_fraction:
        raise ValueError('candidate must retain total open-risk ceiling')
    runtime = staging/'runtime';runtime.mkdir()
    for name in ('abupy','scripts','configs/selection'):
        shutil.copytree(ROOT/name,runtime/name,
            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    genesis=staging/'.genesis';genesis.mkdir()
    registration=dict(registered_at=started.isoformat(),protocol=protocol,
        hypothesis='Remove only the redundant same-day ceiling while total, industry and stress risk stay frozen.',
        historical_screen_status='REJECTED_CI_CROSSED_ZERO',
        model_policy='One fit on mature labels; no retraining or parameter search.',
        dependencies=base.dependencies(),runtime_files=shadow.file_manifest(runtime),
        source_config_sha256=source.sha256,policy_config_sha256=policy.sha256,
        risk_config_sha256=risk.sha256,prior_history_is_not_holdout=True,
        account_difference='same_day_new_risk_fraction_only',
        caveats=['Research simulation only; no broker or automatic admission.',
                 'Historical screen failed its primary confidence-interval gate.',
                 'Held stock distributions or data gaps stop the experiment.'])
    shadow.json_write(genesis/'registration.json',registration)
    print('Copying frozen market inputs...',flush=True)
    base.copy_inputs(staging,signal,research)
    shadow.json_write(genesis/'initial_data_hashes.json',
                      shadow.file_manifest(staging/'data'))
    print('Loading registration panel...',flush=True)
    panel=base.load_panel(staging,int(started.strftime('%Y%m%d')))
    if int(panel.dates[-1]) >= int(started.strftime('%Y%m%d')):
        raise ValueError('registration requires completed prior-day history')
    print('Fitting one frozen model...',flush=True)
    model=shadow.train_once(panel,source,protocol)
    shadow.json_write(genesis/'model_manifest.json',model.manifest)
    account_specs={name:dict(ranking_exits=bool(spec['ranking_exits']),
        risk=replace(risk,same_day_new_risk_fraction=float(
            spec['same_day_new_risk_fraction']))) for name,spec in specs.items()}
    state=shadow.new_state(panel,source,policy,risk,protocol,started.isoformat(),
                           account_specs=account_specs)
    shadow.dump_pickle(genesis/'model.pkl.gz',model)
    shadow.dump_pickle(genesis/'panel.pkl.gz',panel)
    shadow.dump_pickle(genesis/'state.pkl.gz',shadow.checkpoint_state(state))
    shadow.export_accounts(genesis,state)
    shadow.commit_directory(genesis,staging/'genesis','GENESIS')
    os.rename(staging,root)
    shadow.json_write(root/'summary.json',shadow.summary(state))
    return shadow.summary(state)


def evaluate(root):
    paths,previous_hash,state=base.latest(root)
    if state['evaluation_status']!='REVIEW_REQUIRED':
        return dict(status='EVALUATION_NOT_DUE',summary=shadow.summary(state))
    if (root/'evaluation').exists():
        shadow.verify_files(root/'evaluation',json.loads(
            (root/'evaluation/commit.json').read_text())['files'])
        return json.loads((root/'evaluation/report.json').read_text())
    shadow.audit_replay(root)
    from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval
    protocol=state['protocol'];outcomes=[];primary=None
    for cost in [protocol['primary_slippage_bps']]+protocol['stress_slippage_bps']:
        replay=shadow.load_pickle(paths[0]/'state.pkl.gz')
        for account in replay['accounts'].values():
            account['executor'].config=replace(
                account['executor'].config,slippage_bps=cost)
        panels=shadow.replay_panels(paths);next(panels)
        for directory in paths[1:]:
            panel=next(panels)
            scores=pd.read_csv(directory/'features_scores.csv.gz',dtype={'symbol':str})
            shadow.step_accounts(replay,panel,scores,check_checkpoint=False)
        metrics=shadow.summary(replay)['accounts']
        current,candidate=metrics['current'],metrics['same_day_release']
        limit=protocol['maximum_drawdown_fraction']*100
        outcomes.append(dict(slippage_bps=cost,accounts=metrics,
            pass_return=candidate['return_pct']>current['return_pct'],
            pass_drawdown=candidate['max_drawdown_pct']>=current['max_drawdown_pct']-1 and candidate['max_drawdown_pct']>=-limit,
            pass_stress=candidate['stress_max_drawdown_pct']>=current['stress_max_drawdown_pct']-1 and candidate['stress_max_drawdown_pct']>=-limit))
        if cost==protocol['primary_slippage_bps']:primary=replay
    initial=protocol['initial_cash_per_account']
    navs=[np.r_[initial,primary['accounts'][name]['executor'].curve_frame().capital.to_numpy()]
          for name in ('current','same_day_release')]
    interval=paired_block_interval(*navs,seed=protocol['bootstrap_seed'],
        replicates=protocol['bootstrap_replicates'],block=protocol['bootstrap_block_sessions'])
    interval['interpretation']='Predeclared paired forward diagnostic; manual review required.'
    passed=all(r['pass_return'] and r['pass_drawdown'] and r['pass_stress'] for r in outcomes) and interval['ci95_low_pp']>0
    report=dict(status='ELIGIBLE_FOR_MANUAL_REVIEW' if passed else 'RESEARCH_ONLY_GATES_FAILED',
        asof=state['evaluation_asof'],scenarios=outcomes,paired_interval=interval,
        paper_admitted=False,live_admitted=False,automatic_admission=False)
    stage=root/('.evaluation-'+uuid.uuid4().hex);stage.mkdir()
    shadow.json_write(stage/'report.json',report)
    shadow.commit_directory(stage,root/'evaluation',previous_hash)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('init','run','status','audit','evaluate'))
    parser.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    parser.add_argument('--signal-dir',type=Path,
        default=Path('/Users/wjy/abu/shadow/alpha158_forward_v1/data/signal'))
    parser.add_argument('--research-dir',type=Path,
        default=Path('/Users/wjy/abu/shadow/alpha158_forward_v1/data/research'))
    args=parser.parse_args();root=args.root.resolve()
    if args.command=='init':result=initialize(root,args.signal_dir,args.research_dir)
    else:
        shadow.verify_chain(root);base.verify_runtime(root)
        frozen=root/'runtime/scripts'/Path(__file__).name
        if Path(__file__).resolve()!=frozen:
            os.execv(sys.executable,[sys.executable,'-B',str(frozen),args.command,'--root',str(root)])
        with (root/'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if args.command=='run':result=base.run_session(root)
            elif args.command=='audit':result=shadow.audit_replay(root)
            elif args.command=='evaluate':result=evaluate(root)
            else:result=shadow.summary(base.latest(root)[2])
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':main()
