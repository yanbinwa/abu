#!/usr/bin/env python3
"""One frozen, descriptive replay of all runnable current strategy variants."""
from __future__ import annotations
import argparse
import copy
from dataclasses import asdict,fields,replace,is_dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import numpy as np
import pandas as pd
from abupy.AlphaBu.ABuArtifactManifest import sha256_file,runtime_environment
from abupy.AlphaBu.ABuSelectionPanelV2 import SelectionPanelV2
from abupy.AlphaBu.ABuSelectionStrategiesV2 import LEGACY_STRATEGIES,run_legacy_v2_backtest
from abupy.AlphaBu.ABuVCPStrategy import VCPStrategy,VCP_EXPERIMENTS,run_vcp_backtest,load_vcp_core_config,load_vcp_attention_config,load_vcp_residual_config
from abupy.AlphaBu.ABuAlpha158Lite import load_alpha158_lite_config,load_alpha158_lite_low_turnover_config,load_alpha158_lite_turnover_config
from abupy.AlphaBu.ABuPortfolioRisk import load_risk_config,PortfolioRiskEngine
from abupy.AlphaBu.ABuPortfolioExecutor import ExecutionConfig
from abupy.AlphaBu.ABuCostAwareAlpha import CalibrationConfig,PastScoreCalibration,CostAwareReview
from abupy.AlphaBu.ABuScaleOutPolicy import ScaleOutConfig
from abupy.AlphaBu.ABuPositionAddResearch import replay_residual_add_overlay,replay_isolated_add_sleeve
from scripts.backtest_alpha158_lite_v1 import rank_frame,run_rank_portfolio
from scripts.backtest_alpha158_lite_low_turnover_v3 import run_low_turnover,annual_returns
from scripts.backtest_position_add_v1 import _policy
from scripts.backtest_position_add_isolated_sleeve_v1 import _combine
from scripts.backtest_vcp_context_v1 import EXPERIMENTS,run_experiment,load_frozen_intents
from scripts.backtest_vcp_quality_rank_v1 import quality_intent_frame
from scripts.backtest_vcp_standardized_residual_v1 import standardized_residual_scores
from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval

BACKTESTS=Path('/Users/wjy/abu/backtests')
SIGNAL=Path('/Users/wjy/abu/shadow/alpha158_forward_v1/data/signal')
RESEARCH=Path('/Users/wjy/abu/shadow/alpha158_forward_v1/data/research')
START,END=20230727,20260930
ADD_POLICIES=('protected_winner','rebreakout','turtle_atr','turtle_atr_market_gate','protected_rebreakout_all_of')
SCALES=('scale_out_1r','scale_out_2r','scale_out_2r_50','scale_out_1r_2r')
STRESS=('alpha_v3','alpha_no_rank_exit','alpha_sync','alpha_add_turtle_atr',
        'alpha_scale_out_2r_50','vcp_h_residual_stop_trailing_stagnation',
        'vcp_sync','vcp_add_turtle_atr','vcp_scale_out_2r_50')
INPUTS={
    'alpha':BACKTESTS/'alpha158_lite_v1_hardened_20261003/oos_predictions.csv.gz',
    'alpha_report':BACKTESTS/'alpha158_lite_v1_hardened_20261003/research_report.json',
    'context':BACKTESTS/'vcp_context_shadow_v1/context_shadow_intents.csv',
    'vcp_features':BACKTESTS/'vcp_quality_rank_v1/feature_snapshots.csv.gz',
    **{name:BACKTESTS/name/'oos_predictions.csv' for name in
       ('vcp_quality_rank_v1','vcp_quality_rank_v2','vcp_quality_realized_r_v1')},
}


def write_json(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')


def cache_frozen_signals(panel):
    """Memoize only pure inputs on this immutable panel, never account state."""
    eligibility=panel.signal_eligible
    generate=VCPStrategy.generate_intents
    eligible_cache,intent_cache={},{}
    def eligible(min_history=120,required_fields=(),unknown_st_policy='exclude'):
        key=(min_history,tuple(required_fields),unknown_st_policy)
        if key not in eligible_cache:
            value=eligibility(min_history,required_fields,unknown_st_policy)
            value.setflags(write=False)
            eligible_cache[key]=value
        return eligible_cache[key]
    def intents(strategy,day,variant='core',allow_terminal=False):
        if strategy.panel is not panel:
            return generate(strategy,day,variant,allow_terminal)
        key=(day,variant,allow_terminal,strategy.core.sha256,strategy.attention.sha256,strategy.residual.sha256)
        if key not in intent_cache:
            intent_cache[key]=generate(strategy,day,variant,allow_terminal)
        return copy.deepcopy(intent_cache[key])
    panel.signal_eligible=eligible
    VCPStrategy.generate_intents=intents
    def restore():
        del panel.signal_eligible
        VCPStrategy.generate_intents=generate
    return restore


def arm_list():
    return ([f'legacy_{s}_{m}' for s in LEGACY_STRATEGIES for m in ('a2_pit_corrected','b1','b2')]
        +['vcp_'+s for s in VCP_EXPERIMENTS]+['vcp_sync']
        +['vcp_add_'+p for p in ADD_POLICIES]+['vcp_'+s for s in SCALES]
        +['alpha_baseline_score','alpha_v1','alpha_v2','alpha_v3','alpha_sync',
          'alpha_no_rank_exit','alpha_cost_gate','alpha_diversified20']
        +['alpha_add_'+p for p in ADD_POLICIES]+['alpha_'+s for s in SCALES]
        +['context_'+s for s in EXPERIMENTS]+['vcp_standardized','vcp_followthrough',
          'alpha_residual_add','alpha_sleeve','alpha_sleeve_market_gate','combined_base_orders'])


def freeze(output):
    output.mkdir(parents=True,exist_ok=False)
    runtime=output/'runtime';runtime.mkdir()
    for name in ('abupy','scripts','configs/selection'):
        shutil.copytree(ROOT/name,runtime/name,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    frozen_files={str(p.relative_to(runtime)):sha256_file(p) for p in runtime.rglob('*') if p.is_file()}
    (output/'inputs').mkdir()
    snapshots={}
    for key,path in INPUTS.items():
        dest=output/'inputs'/(key+''.join(path.suffixes))
        shutil.copy2(path,dest)
        snapshots[key]=dict(path=str(dest),source=str(path),sha256=sha256_file(dest))
    data_files=[p for p in SIGNAL.rglob('*') if p.is_file()]
    for name in ('raw','signal_extra'):
        data_files.extend(p for p in (RESEARCH/name).rglob('*') if p.is_file())
    data_files.extend(RESEARCH/name for name in ('security_master.csv','corporate_actions.csv','industry_changes.csv','sz_name_changes.csv'))
    write_json(output/'registration.json',dict(created_at=datetime.now().astimezone().isoformat(),
        main_start=START,end=END,initial_cash=1000000,primary_slippage_bps=25,
        arms=arm_list(),stress_arms=STRESS,stress_slippage_bps=[40,60],
        runtime_files=frozen_files,input_snapshots=snapshots,
        data_files={str(p):sha256_file(p) for p in sorted(data_files)},
        environment=runtime_environment(),parameter_search=False,post_hoc=True,new_holdout=False,
        primary_candidate='alpha_no_rank_exit',no_forward_state_mutation=True,
        quality_scope='Each saved model OOS window separately; cash start, no terminal extrapolation.',
        excluded={'alpha158_multifactor_v4':'PIT fundamental data gate remains blocked',
            'shortline_emotion':'No admissible historical signal sample',
            'intraday_M1_M2':'Minute data/admission unavailable; daily D0 is covered',
            'legacy_a1_and_akshare_prototypes':'Known legacy accounting or selected-universe prototypes; superseded by PIT A2/B1/B2',
            'frozen_forward_model':'Fitted at registration; cannot predict the past without look-ahead',
            'shadow_ADD_variants':'Signal-only controls duplicated by their unchanged executable NoAdd base; not extra funded accounts'}))
    os.execv(sys.executable,[sys.executable,'-B',str(runtime/'scripts'/Path(__file__).name),'--output',str(output),'--frozen'])


def normalize_curve(curve,dates):
    curve=curve.copy().set_index('date')
    if curve.index.duplicated().any() or not curve.index.isin(dates).all():
        raise ValueError('invalid curve dates')
    missing=pd.Index(dates).difference(curve.index)
    if len(missing) and list(missing)!=[int(dates[0])]:
        raise ValueError('only the initial cash anchor can be absent')
    if len(missing):
        anchor={key:0. for key in curve.columns}
        for key in curve.columns:
            if key in ('capital','cash') or key.startswith('liquidation_nav_'):
                anchor[key]=1e6
        curve.loc[int(dates[0])]=anchor
    return curve.sort_index().reset_index()


def metrics(curve,fills):
    capital=np.r_[1e6,curve.capital.to_numpy(float)]
    ret=capital[1:]/capital[:-1]-1
    dd=(capital/np.maximum.accumulate(capital)-1).min()
    years=(pd.Timestamp(str(int(curve.date.iloc[-1])))-pd.Timestamp(str(int(curve.date.iloc[0])))).days/365.25
    cagr=(capital[-1]/1e6)**(1/years)-1
    filled=fills[fills.status.eq('filled')] if len(fills) and 'status' in fills else pd.DataFrame()
    cost_cols=[c for c in ('commission','transfer_fee','stamp_tax','slippage_cost') if c in filled]
    cost=float(filled[cost_cols].sum().sum()) if len(filled) else np.nan
    stress=np.r_[1e6,curve.liquidation_nav_3_limits.to_numpy()] if 'liquidation_nav_3_limits' in curve else None
    return dict(return_pct=float((capital[-1]/1e6-1)*100),cagr_pct=float(cagr*100),
        max_drawdown_pct=float(dd*100),calmar=float(cagr/abs(dd)) if dd else np.nan,
        annual_vol_pct=float(ret.std(ddof=1)*np.sqrt(252)*100),
        sharpe_rf0=float(ret.mean()/ret.std(ddof=1)*np.sqrt(252)) if ret.std() else np.nan,
        daily_es95_pct=float(ret[ret<=np.quantile(ret,.05)].mean()*100),
        average_exposure_pct=float(curve.exposure.mean()*100),
        filled_buys=int((filled.side=='buy').sum()) if len(filled) else 0,
        total_friction_pct_initial=cost/1e4,ending_capital=float(capital[-1]),
        stress_drawdown_pct=float((stress/np.maximum.accumulate(stress)-1).min()*100) if stress is not None else np.nan,
        stress_return_pct=float((stress[-1]/1e6-1)*100) if stress is not None else np.nan)


class Comparison:
    def __init__(self,output):
        self.output=output;self.registration=json.loads((output/'registration.json').read_text())
        for name,digest in self.registration['runtime_files'].items():
            if sha256_file(ROOT/name)!=digest:
                raise ValueError('frozen code changed: '+name)
        for value in self.registration['input_snapshots'].values():
            if sha256_file(value['path'])!=value['sha256']:
                raise ValueError('prediction snapshot changed')
        self.c=ROOT/'configs/selection'
        self.source=load_alpha158_lite_config(self.c/'alpha158_lite_v1.json')
        self.low=load_alpha158_lite_low_turnover_config(self.c/'alpha158_lite_low_turnover_v3.json')
        self.risk=load_risk_config(self.c/'risk_v1.json')
        self.raw=self.read('alpha')
        if not (self.raw.train_end<self.raw.signal_asof).all():
            raise ValueError('in-sample predictions forbidden')
        report=json.loads(Path(self.registration['input_snapshots']['alpha_report']['path']).read_text())
        if report['config_sha256']!=self.source.sha256 or self.low.source_config_sha256!=self.source.sha256:
            raise ValueError('prediction config mismatch')
        print('Loading common PIT panel...',flush=True)
        self.panel=SelectionPanelV2.from_research_data(SIGNAL,RESEARCH,start_date=20200101,end_date=END)
        self.restore_cache=cache_frozen_signals(self.panel)
        for row in self.raw[['symbol','column']].drop_duplicates().itertuples():
            if self.panel.symbols[int(row.column)]!=row.symbol:
                raise ValueError('prediction column mapping mismatch')
        self.scores=rank_frame(self.raw,'alpha_score',100)
        self.shadow=self.read('context')
        self.dates=self.panel.dates[(self.panel.dates>=START)&(self.panel.dates<=END)]
        cfg=json.loads((self.c/'alpha158_cost_gate_v1.json').read_text())
        self.calibration=PastScoreCalibration(self.raw,self.panel.dates,
            CalibrationConfig(**{f.name:cfg[f.name] for f in fields(CalibrationConfig)}))
        self.rows=[];self.annual=[];self.navs={};self.extra={}

    def read(self,key):
        return pd.read_csv(self.registration['input_snapshots'][key]['path'],dtype={'symbol':str})

    def alpha(self,name,cost,begin=START,end=END,**override):
        raw=self.raw[(self.raw.signal_asof>=begin)&(self.raw.signal_asof<=end)]
        scores=self.scores[(self.scores.signal_asof>=begin)&(self.scores.signal_asof<=end)]
        source=replace(self.source,label_slippage_bps=cost)
        if name in ('alpha_v1','alpha_v2','alpha_baseline_score'):
            column='baseline_score' if name=='alpha_baseline_score' else 'alpha_score'
            config={}
            if name=='alpha_v2':
                turnover=load_alpha158_lite_turnover_config(self.c/'alpha158_lite_turnover_v2.json')
                source=replace(source,entry_top_k=turnover.target_positions,portfolio_score_depth=turnover.entry_candidate_depth)
                config=dict(rank_exit_mode='dropout',max_rank_replacements_per_day=turnover.max_rank_replacements_per_day,
                            entry_candidate_depth=turnover.entry_candidate_depth)
            return run_rank_portfolio(self.panel,rank_frame(raw,column,source.portfolio_score_depth),column,source,self.risk,end,**config)[1]
        low=replace(self.low,target_positions=20) if name=='alpha_diversified20' else self.low
        options={}
        if name=='alpha_no_rank_exit': options['review_overlay']=CostAwareReview(suppress_rank_exits=True)
        if name=='alpha_cost_gate': options['review_overlay']=CostAwareReview(self.calibration)
        if name=='alpha_sync' or name.startswith(('alpha_add_','alpha_scale_')):
            options['sync_dynamic_stops']=True
        if name.startswith('alpha_add_'):options['position_add_policy']=_policy(name.removeprefix('alpha_add_'))
        if name.startswith('alpha_scale_'):
            options.update(position_add_policy=_policy('turtle_atr'),scale_out_config=self.scale(name.removeprefix('alpha_')))
        options.update(override)
        return run_low_turnover(self.panel,scores,source,low,self.risk,end,**options)[1]

    def scale(self,name):
        payload=json.loads((self.c/(name+'_v1.json')).read_text())
        for key in ('trigger_r_multiples','cumulative_exit_fractions'):
            payload[key]=tuple(payload[key])
        return ScaleOutConfig(**{f.name:payload[f.name] for f in fields(ScaleOutConfig)})

    def vcp(self,name,cost,begin=START,end=END):
        options={};experiment='h_residual_stop_trailing_stagnation'
        suffix=name.removeprefix('vcp_')
        if suffix in VCP_EXPERIMENTS:experiment=suffix
        else:options['sync_dynamic_stops']=True
        if suffix.startswith('add_'):options['position_add_policy']=_policy(suffix.removeprefix('add_'))
        if suffix.startswith('scale_out_'):
            options.update(position_add_policy=_policy('turtle_atr'),scale_out_config=self.scale(suffix))
        audit={}
        dates=self.panel.dates[(self.panel.dates>=begin)&(self.panel.dates<=end)]
        _,curve,fills,_,_=run_vcp_backtest(self.panel,None,experiment,cost,
            load_vcp_core_config(self.c/'vcp_core_v1.json'),load_vcp_attention_config(self.c/'vcp_attention_v1.json'),
            self.risk,load_vcp_residual_config(self.c/'vcp_residual_v2.json'),
            start_date=int(dates[1]),end_date=end,audit=audit,**options)
        audit.update(curve=curve,fills=fills)
        return audit

    def special(self,name,cost):
        if name.startswith('legacy_'):
            strategy,mode=next((s,m) for s in LEGACY_STRATEGIES for m in ('a2_pit_corrected','b1','b2') if name==f'legacy_{s}_{m}')
            _,curve,fills,decisions=run_legacy_v2_backtest(self.panel,strategy,None,mode,cost,self.risk,
                start_date=int(self.dates[1]),end_date=END)
            return dict(curve=curve,fills=fills,decisions=decisions)
        if name.startswith('context_'):
            return run_experiment(self.panel,self.shadow,name.removeprefix('context_'),self.risk,int(self.dates[1]),END,slippage_bps=cost)[1]
        if name=='vcp_followthrough':
            from abupy.AlphaBu.ABuVCPFollowThrough import confirm_followthrough_intents,load_vcp_followthrough_config
            intents=confirm_followthrough_intents(self.panel,load_frozen_intents(self.shadow),load_vcp_followthrough_config(self.c/'vcp_followthrough_v1.json'))
            frame=pd.DataFrame([asdict(i) for i in intents])
            return run_experiment(self.panel,frame,name,self.risk,int(self.dates[1]),END,ranking_column='score',slippage_bps=cost)[1]
        if name=='vcp_standardized':
            from abupy.AlphaBu.ABuScoreToIntent import rerank_intents
            frame=self.shadow[['intent_id','signal_asof','symbol','ma120_slope','contraction_tightness','breakout_strength']].merge(
                self.read('vcp_features')[['intent_id','residual_momentum_standardized']],on='intent_id',validate='one_to_one')
            scored=standardized_residual_scores(frame,json.loads((self.c/'vcp_residual_standardized_v1.json').read_text()))
            source=self.shadow[self.shadow.intent_id.isin(scored.intent_id)]
            intents=rerank_intents(load_frozen_intents(source),scored.set_index('intent_id').standardized_residual_score.to_dict(),name)
            frame=pd.DataFrame([asdict(i) for i in intents])
            return run_experiment(self.panel,frame,name,self.risk,int(self.dates[1]),END,ranking_column='score',slippage_bps=cost)[1]
        if name in ('alpha_residual_add','alpha_sleeve','alpha_sleeve_market_gate'):
            gated=name!='alpha_sleeve';initial=1e6 if name=='alpha_residual_add' else 9e5
            audit=self.alpha('alpha_sync',cost,position_add_policy=_policy('turtle_atr_market_gate' if gated else 'turtle_atr'),
                position_add_execution_mode='shadow',initial_cash=initial)
            if name=='alpha_residual_add':
                replay=replay_residual_add_overlay(self.panel,audit['add_proposals'],audit['lot_dispositions'],
                    audit['curve'],audit['orders'],audit['risk_positions_daily'],ExecutionConfig(slippage_bps=cost),
                    PortfolioRiskEngine(self.panel,self.risk),start_date=START,end_date=END)
                curve=replay['curve']
            else:
                replay=replay_isolated_add_sleeve(self.panel,audit['add_proposals'],audit['lot_dispositions'],
                    ExecutionConfig(initial_cash=1e5,slippage_bps=cost),1e5,start_date=START,end_date=END,max_gross_exposure=.8)
                curve=_combine(audit['curve'],replay['curve'],1e5)
            # Auxiliary fills have a different schema; do not misreport base-only costs.
            return dict(curve=curve,fills=pd.DataFrame(),base_fills=audit['fills'],overlay_entries=replay['entries'],
                        overlay_dispositions=replay['dispositions'],overlay_rejections=replay['rejections'])
        if name=='combined_base_orders':
            from scripts.backtest_position_add_combined_v1 import run
            frames=[]
            for arm in ('vcp_sync','alpha_sync'):
                frame=pd.read_csv(self.output/'main'/f'{arm}_25bp/orders.csv')
                frame['source_strategy']=arm;frames.append(frame)
            executor,_,curve,decisions=run(self.panel,frames,self.risk,start_date=int(self.dates[1]),end_date=END)
            return dict(curve=curve,fills=executor.fills_frame(),decisions=decisions,orders=executor.order_history)
        raise ValueError(name)

    def save(self,name,cost,audit,scope='main',begin=START,end=END):
        dates=self.panel.dates[(self.panel.dates>=begin)&(self.panel.dates<=end)]
        curve=normalize_curve(audit['curve'],dates);fills=audit.get('fills',pd.DataFrame())
        # First interrupted attempt is retained as an uncached golden reference.
        reference=BACKTESTS/'current_strategy_comparison_20261004'/scope/f'{name}_{cost:g}bp'
        if self.output.name!='current_strategy_comparison_20261004' and (reference/'metrics.json').exists():
            pd.testing.assert_frame_equal(curve,pd.read_csv(reference/'daily_nav.csv'),check_dtype=False,rtol=1e-10,atol=1e-7)
            if len(fills):
                business=['date','symbol','side','status','quantity','fill_price_raw','commission','transfer_fee','stamp_tax','slippage_cost']
                pd.testing.assert_frame_equal(fills[business],pd.read_csv(reference/'fills.csv')[business],check_dtype=False,rtol=1e-10,atol=1e-7)
        if {'cash','stocks'}.issubset(curve):
            if (curve.capital-curve.cash-curve.stocks).abs().max()>1e-5 or curve.cash.min() < -1e-6:
                raise AssertionError('accounting invariant failed')
        directory=self.output/scope/f'{name}_{cost:g}bp';directory.mkdir(parents=True,exist_ok=True)
        curve.to_csv(directory/'daily_nav.csv',index=False)
        for key,value in audit.items():
            if key=='curve':continue
            if isinstance(value,pd.DataFrame):value.to_csv(directory/(key+'.csv'),index=False)
            elif isinstance(value,(list,tuple)):
                pd.DataFrame([asdict(v) if is_dataclass(v) else v for v in value]).to_csv(directory/(key+'.csv'),index=False)
        row=dict(strategy=name,scope=scope,slippage_bps=cost,start=begin,end=end,**metrics(curve,fills))
        self.rows.append(row);self.navs[(scope,name,cost)]=curve
        year=annual_returns(curve);year['strategy']=name;year['scope']=scope;year['slippage_bps']=cost
        self.annual.append(year)
        pd.DataFrame(self.rows).to_csv(self.output/'results.csv',index=False)
        pd.concat(self.annual,ignore_index=True).to_csv(self.output/'annual_returns.csv',index=False)
        write_json(directory/'metrics.json',row)
        print(json.dumps({k:row[k] for k in ('strategy','scope','slippage_bps','return_pct','max_drawdown_pct','average_exposure_pct')}),flush=True)

    def run_one(self,name,cost,scope='main',begin=START,end=END):
        print(f'RUN {scope} {name} {cost}bp',flush=True)
        if name.startswith('alpha_') and name not in ('alpha_residual_add','alpha_sleeve','alpha_sleeve_market_gate'):
            audit=self.alpha(name,cost,begin,end)
        elif name.startswith('vcp_') and name not in ('vcp_followthrough','vcp_standardized'):
            audit=self.vcp(name,cost,begin,end)
        else:audit=self.special(name,cost)
        self.save(name,cost,audit,scope,begin,end)

    def finish(self):
        for key in ('vcp_quality_rank_v1','vcp_quality_rank_v2','vcp_quality_realized_r_v1'):
            pred=self.read(key)
            if not (pred.train_end<pred.signal_asof).all():raise ValueError('non-OOS VCP scores')
            begin,end=int(pred.signal_asof.min()),int(pred.signal_asof.max())
            source=self.shadow[self.shadow.intent_id.isin(pred.intent_id)].copy()
            ranked=quality_intent_frame(source,pred,key)
            dates=self.panel.dates[(self.panel.dates>=begin)&(self.panel.dates<=end)]
            for name,frame in ((key,ranked),('matched_original_intents',source)):
                audit=run_experiment(self.panel,frame,name,self.risk,int(dates[1]),end,ranking_column='score')[1]
                self.save(name,25,audit,key,begin,end)
            for name in ('alpha_no_rank_exit','alpha_v3','alpha_scale_out_2r_50','vcp_h_residual_stop_trailing_stagnation'):
                self.run_one(name,25,key,begin,end)
        pairs=[]
        for cost in (25,40,60):
            for base,candidate in (('alpha_v3','alpha_no_rank_exit'),('alpha_add_turtle_atr','alpha_scale_out_2r_50'),
                                   ('vcp_add_turtle_atr','vcp_scale_out_2r_50')):
                left=self.navs[('main',base,cost)].capital.to_numpy()
                right=self.navs[('main',candidate,cost)].capital.to_numpy()
                pairs.append(dict(baseline=base,candidate=candidate,slippage_bps=cost,**paired_block_interval(left,right)))
        pd.DataFrame(pairs).to_csv(self.output/'paired_uncertainty.csv',index=False)
        benchmark=pd.Series(self.panel.benchmark_close,index=self.panel.dates).reindex(self.dates)
        curve=pd.DataFrame(dict(date=self.dates,capital=1e6*benchmark.to_numpy()/benchmark.iloc[0],exposure=1.))
        self.save('CSI300_price_index_gross',0,dict(curve=curve,fills=pd.DataFrame()),scope='benchmark')
        main=pd.DataFrame(self.rows).query("scope == 'main' and slippage_bps == 25").copy()
        main['pareto_efficient']=[not ((main.return_pct>=r.return_pct)&(main.max_drawdown_pct>=r.max_drawdown_pct)&
            ((main.return_pct>r.return_pct)|(main.max_drawdown_pct>r.max_drawdown_pct))).any() for r in main.itertuples()]
        main.sort_values('return_pct',ascending=False).to_csv(self.output/'primary_ranking.csv',index=False)
        changed=[path for path,digest in self.registration['data_files'].items() if sha256_file(path)!=digest]
        write_json(self.output/'completion.json',dict(status='COMPLETE' if not changed else 'INVALID_DATA_CHANGED',
            runs=len(self.rows),primary_variants=len(main),inputs_unchanged=not changed,changed_inputs=changed,
            research_only=True,new_holdout=False,forward_sessions_added=0,automatic_admission=False))
        if changed:raise ValueError('input data changed')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=BACKTESTS/'current_strategy_comparison_20261004')
    parser.add_argument('--frozen',action='store_true')
    args=parser.parse_args();output=args.output.resolve()
    if not args.frozen:freeze(output)
    comparison=Comparison(output)
    for name in arm_list():comparison.run_one(name,25)
    for cost in (40,60):
        for name in STRESS:comparison.run_one(name,cost)
    comparison.finish()


if __name__=='__main__':main()
