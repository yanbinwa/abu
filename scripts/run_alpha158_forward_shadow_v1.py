#!/usr/bin/env python3
"""Register and run a frozen prospective paired experiment; never send orders."""
from __future__ import annotations
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import sys
import uuid
from dataclasses import replace
from importlib.metadata import version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from abupy.AlphaBu import ABuAlphaForwardShadow as shadow
from abupy.AlphaBu.ABuAlpha158Lite import load_alpha158_lite_config, load_alpha158_lite_low_turnover_config
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuArtifactManifest import sha256_file

DEFAULT_ROOT = Path('/Users/wjy/abu/shadow/alpha158_forward_v1')
CONFIGS = ('alpha158_forward_shadow_v1.json','alpha158_lite_v1.json',
           'alpha158_lite_low_turnover_v3.json','risk_v1.json')


def dependencies():
    return dict(python=sys.version,packages={name:version(name) for name in
                ('numpy','pandas','scikit-learn','scipy','akshare')})


def verify_runtime(root):
    registration = json.loads((root/'genesis/registration.json').read_text())
    shadow.verify_files(root/'runtime',registration['runtime_files'])
    if dependencies() != registration['dependencies']:
        raise ValueError('runtime dependencies changed; restore the registered environment')


def copy_inputs(target, signal, research):
    data = target/'data'; data.mkdir()
    shutil.copytree(signal,data/'signal')
    (data/'research').mkdir()
    for name in ('raw','signal_extra'):
        shutil.copytree(research/name,data/'research'/name)
    for name in ('security_master.csv','corporate_actions.csv','industry_changes.csv','sz_name_changes.csv'):
        shutil.copy2(research/name,data/'research'/name)


def apply_spot_status(panel,collector):
    # Daily names are prospective ST evidence. Preserve the original Shenzhen
    # research universe; do not silently expand it to Shanghai mid-experiment.
    for path in sorted(collector.glob('market_snapshots/*/stock_spot.csv')):
        date = int(path.parent.name)
        matches = np.flatnonzero(panel.dates == date)
        if not len(matches):
            continue
        day = int(matches[0])
        frame = pd.read_csv(path,dtype={'symbol':str}).set_index('symbol')
        for col,symbol in enumerate(panel.symbols):
            if not symbol.startswith('sz'):
                continue
            if symbol in frame.index:
                name = str(frame.loc[symbol,'名称']).upper()
                panel.base.st_status[day,col] = 'ST' in name or '退' in name
                panel.st_status_known[day,col] = bool(name and name != 'NAN')
            else:
                panel.st_status_known[day,col] = False


def load_panel(root,end):
    panel = SelectionPanelV2.from_research_data(root/'data/signal',root/'data/research',
                                               start_date=20200101,end_date=end)
    apply_spot_status(panel,root/'collector')
    genesis_summary = root/'genesis/summary.json'
    cutoff = (json.loads(genesis_summary.read_text())['historical_cutoff']
              if genesis_summary.exists() else int(panel.dates[-1]))
    shadow.forward_actions(panel,pd.read_csv(root/'data/research/corporate_actions.csv',dtype={'symbol':str}),after_date=cutoff)
    return panel


def initialize(root,signal,research):
    if root.exists():
        raise FileExistsError('experiment already exists; never reset an existing trial')
    started = shadow.now_shanghai()
    staging = root.parent/('.'+root.name+'.init-'+uuid.uuid4().hex)
    staging.mkdir(parents=True)
    protocol = json.loads((ROOT/'configs/selection'/CONFIGS[0]).read_text())
    shadow.validate_protocol(protocol)
    source = load_alpha158_lite_config(ROOT/'configs/selection'/CONFIGS[1])
    policy = load_alpha158_lite_low_turnover_config(ROOT/'configs/selection'/CONFIGS[2])
    risk = load_risk_config(ROOT/'configs/selection'/CONFIGS[3])
    if source.sha256 != policy.source_config_sha256:
        raise ValueError('source and policy configuration do not match')
    runtime = staging/'runtime'; runtime.mkdir()
    for name in ('abupy','scripts','configs/selection'):
        shutil.copytree(ROOT/name,runtime/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    # Registration is written before fitting; no return-based model selection.
    genesis = staging/'.genesis'; genesis.mkdir()
    registration = dict(registered_at=started.isoformat(),protocol=protocol,
        hypothesis='Suppressing only persistent-rank sells improves net return without worsening drawdown.',
        model_policy='One fit on strictly mature past labels; no retraining or parameter search.',
        dependencies=dependencies(),runtime_files=shadow.file_manifest(runtime),
        source_config_sha256=source.sha256,policy_config_sha256=policy.sha256,
        risk_config_sha256=risk.sha256,prior_history_is_not_holdout=True,
        caveats=['Research simulation, no live fills or automatic admission.',
                 'Daily entry-date clusters are not independent observations.',
                 'Frozen registered universe; conservative cash-dividend tax reserve of 20%.',
                 'Held stock distributions or data gaps stop the experiment for data/ledger review.'])
    shadow.json_write(genesis/'registration.json',registration)
    print('Copying private market data...',flush=True)
    copy_inputs(staging,signal,research)
    shadow.json_write(genesis/'initial_data_hashes.json',shadow.file_manifest(staging/'data'))
    print('Loading registration panel...',flush=True)
    panel = load_panel(staging,int(started.strftime('%Y%m%d')))
    if int(panel.dates[-1]) >= int(started.strftime('%Y%m%d')):
        raise ValueError('register before the next session using completed prior-day history')
    print('Fitting the one frozen model...',flush=True)
    model = shadow.train_once(panel,source,protocol)
    shadow.json_write(genesis/'model_manifest.json',model.manifest)
    state = shadow.new_state(panel,source,policy,risk,protocol,started.isoformat())
    shadow.dump_pickle(genesis/'model.pkl.gz',model)
    shadow.dump_pickle(genesis/'panel.pkl.gz',panel)
    shadow.dump_pickle(genesis/'state.pkl.gz',shadow.checkpoint_state(state))
    shadow.export_accounts(genesis,state)
    shadow.commit_directory(genesis,staging/'genesis','GENESIS')
    os.rename(staging,root)
    shadow.json_write(root/'summary.json',shadow.summary(state))
    return shadow.summary(state)


def latest(root):
    paths,digest = shadow.verify_chain(root)
    return paths, digest, shadow.load_pickle(paths[-1]/'state.pkl.gz')


def validate_capture(snapshot,date,registered_at,minimum):
    frame = pd.read_csv(snapshot/'stock_spot.csv',dtype={'symbol':str})
    if len(frame) < minimum or set(frame.date.astype(int)) != {date} or frame.symbol.duplicated().any():
        raise ValueError('incomplete or inconsistent same-day snapshot')
    batches = list(snapshot.glob('raw_batches/*/metadata.json'))
    if not batches:
        raise ValueError('missing raw provider capture')
    for path in batches:
        metadata = json.loads(path.read_text())
        captured = pd.Timestamp(metadata['collected_at']).tz_convert('Asia/Shanghai')
        if (int(metadata['trade_date']) != date or int(captured.strftime('%Y%m%d')) != date
                or captured <= pd.Timestamp(registered_at) or (captured.hour,captured.minute) < (15,15)):
            raise ValueError('capture was not made after registration at the same-day close')
    return frame


def run_session(root):
    paths,previous_hash,state = latest(root)
    now = shadow.now_shanghai(); today = int(now.strftime('%Y%m%d'))
    if state['evaluation_asof'] is not None:
        return dict(status='STOPPED_AT_CHECKPOINT',summary=shadow.summary(state))
    if today <= state['last_processed_date']:
        return dict(status='ALREADY_COMMITTED',summary=shadow.summary(state))
    if now.weekday() >= 5 or (now.hour,now.minute) < (15,15):
        return dict(status='WAITING_FOR_SESSION_CLOSE',summary=shadow.summary(state))
    from scripts.update_paper_market_data import update_market_data, refresh_held_actions
    collector = root/'collector'; collector.mkdir(exist_ok=True)
    update = update_market_data(root/'data/signal',root/'data/research/raw',collector,
                               today=now.date(),required_trade_date=today)
    snapshot = collector/'market_snapshots'/str(today)
    if not snapshot.exists():
        return dict(status='NO_SAME_DAY_SNAPSHOT',market_update=update,summary=shadow.summary(state))
    validate_capture(snapshot,today,state['registered_at'],state['protocol']['minimum_market_rows'])
    # Only the private collector gets this compatibility view; the existing
    # VCP paper state and its notification pipeline are never touched.
    positions = {s:{} for a in state['accounts'].values() for s in a['executor'].positions}
    orders = [dict(symbol=o.symbol,side=o.side) for a in state['accounts'].values() for o in a['executor'].orders]
    shadow.json_write(collector/'state.json',dict(active=dict(positions=positions,orders=orders)))
    actions = refresh_held_actions(collector,root/'data/research')
    panel = load_panel(root,today)
    if int(panel.dates[-1]) != today:
        raise ValueError('latest panel session differs from current calendar date')
    old_panel = shadow.last_panel(paths)
    delta = shadow.panel_delta(old_panel,panel)
    del old_panel
    model = shadow.load_pickle(root/'genesis/model.pkl.gz')
    scores = shadow.feature_scores(panel,state['source'],model,state['protocol']['minimum_candidate_rows'])
    shadow.step_accounts(state,panel,scores)
    stage = root/('.session-'+uuid.uuid4().hex); stage.mkdir()
    shutil.copytree(snapshot,stage/'source_snapshot')
    shutil.copy2(root/'data/research/corporate_actions.csv',stage/'corporate_actions.csv')
    shadow.json_write(stage/'collection.json',dict(processed_at=now.isoformat(),market=update,actions=actions))
    scores.to_csv(stage/'features_scores.csv.gz',index=False,float_format='%.17g')
    shadow.dump_pickle(stage/'panel_delta.pkl.gz',delta)
    shadow.dump_pickle(stage/'state.pkl.gz',shadow.checkpoint_state(state))
    shadow.export_accounts(stage,state)
    shadow.commit_directory(stage,root/'sessions'/str(today),previous_hash)
    shadow.json_write(root/'summary.json',shadow.summary(state))
    return shadow.summary(state)


def evaluate(root):
    paths,previous_hash,primary = latest(root)
    if primary['evaluation_status'] != 'REVIEW_REQUIRED':
        return dict(status='EVALUATION_NOT_DUE',summary=shadow.summary(primary))
    if (root/'evaluation').exists():
        shadow.verify_files(root/'evaluation',json.loads((root/'evaluation/commit.json').read_text())['files'])
        return json.loads((root/'evaluation/report.json').read_text())
    shadow.audit_replay(root)
    from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval
    protocol = primary['protocol']
    outcomes = []
    for cost in [protocol['primary_slippage_bps']]+protocol['stress_slippage_bps']:
        state = shadow.load_pickle(paths[0]/'state.pkl.gz')
        for account in state['accounts'].values():
            executor = account['executor']
            executor.config = replace(executor.config,slippage_bps=cost)
        panels = shadow.replay_panels(paths)
        next(panels)
        for directory in paths[1:]:
            panel = next(panels)
            scores = pd.read_csv(directory/'features_scores.csv.gz',dtype={'symbol':str})
            shadow.step_accounts(state,panel,scores,check_checkpoint=False)
        metrics = shadow.summary(state)['accounts']
        base,candidate = metrics['baseline'],metrics['event_exit_only']
        limit = protocol['maximum_drawdown_fraction']*100
        outcomes.append(dict(slippage_bps=cost,accounts=metrics,
            pass_return=candidate['return_pct']>max(0.,base['return_pct']),
            pass_drawdown=candidate['max_drawdown_pct']>=base['max_drawdown_pct'] and candidate['max_drawdown_pct']>=-limit,
            pass_stress=candidate['stress_max_drawdown_pct']>=-limit and candidate['stress_return_pct']>0))
    initial=protocol['initial_cash_per_account']
    navs=[np.r_[initial,primary['accounts'][arm]['executor'].curve_frame().capital.to_numpy()] for arm in shadow.ARMS]
    interval=paired_block_interval(*navs,seed=protocol['bootstrap_seed'],
        replicates=protocol['bootstrap_replicates'],block=protocol['bootstrap_block_sessions'])
    interval['interpretation']='Predeclared paired block diagnostic; no independence or future-profit guarantee.'
    passed=all(r['pass_return'] and r['pass_drawdown'] and r['pass_stress'] for r in outcomes) and interval['ci95_low_pp']>0
    report=dict(status='ELIGIBLE_FOR_MANUAL_REVIEW' if passed else 'RESEARCH_ONLY_GATES_FAILED',
        asof=primary['evaluation_asof'],scenarios=outcomes,paired_interval=interval,
        paper_admitted=False,live_admitted=False,automatic_admission=False)
    stage=root/('.evaluation-'+uuid.uuid4().hex); stage.mkdir()
    shadow.json_write(stage/'report.json',report)
    shadow.commit_directory(stage,root/'evaluation',previous_hash)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('init','run','status','audit','evaluate'))
    parser.add_argument('--root',type=Path,default=DEFAULT_ROOT)
    parser.add_argument('--signal-dir',type=Path,default=Path('/Users/wjy/abu/data/csv'))
    parser.add_argument('--research-dir',type=Path,default=Path('/Users/wjy/abu/data/selection_research'))
    args=parser.parse_args(); root=args.root.resolve()
    if args.command == 'init':
        result=initialize(root,args.signal_dir,args.research_dir)
    else:
        # Check hashes before loading private checkpoints or executing the
        # archived implementation. Workspace edits cannot change the trial.
        shadow.verify_chain(root)
        verify_runtime(root)
        frozen=root/'runtime/scripts'/Path(__file__).name
        if Path(__file__).resolve()!=frozen:
            os.execv(sys.executable,[sys.executable,'-B',str(frozen),args.command,'--root',str(root)])
        with (root/'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if args.command=='run':
                result=run_session(root)
            elif args.command=='audit':
                result=shadow.audit_replay(root)
            elif args.command=='evaluate':
                result=evaluate(root)
            else:
                result=shadow.summary(latest(root)[2])
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':
    main()
