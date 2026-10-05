#!/usr/bin/env python3
"""Preregistered historical 2x2 gate/allocation experiment plus mechanism controls."""
from __future__ import annotations
import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1]
PARENT=Path('/Users/wjy/abu/backtests/alpha158_industry_gate_2024_2025_20261004')
OUTPUT=Path('/Users/wjy/abu/backtests/alpha158_batch_allocation_20261004')
ARMS=('alpha_no_rank_exit','alpha_industry_gate','alpha_equal_risk','alpha_industry_equal_risk')
FILES=('abupy/AlphaBu/ABuAlphaIndustryGate.py','abupy/AlphaBu/ABuAlphaBatchAllocation.py',
       'scripts/backtest_alpha158_batch_allocation_v1.py','scripts/validate_alpha158_batch_allocation_v1.py',
       'scripts/validate_alpha158_industry_gate_2024_2025.py','tests/test_alpha_industry_gate.py',
       'tests/test_alpha_batch_allocation.py')
LABELS=dict(alpha_no_rank_exit='当前候选',alpha_industry_gate='仅行业过滤',
            alpha_equal_risk='仅等风险分配',alpha_industry_equal_risk='行业过滤＋等风险分配',
            alpha_pool_sequential='固定候选池＋顺序分配',alpha_industry_pool_sequential='过滤＋固定池＋顺序分配')


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')


def freeze(output):
    output.mkdir(parents=True,exist_ok=False)
    commit=subprocess.check_output(['git','rev-parse','0b3c997^{commit}'],cwd=ROOT,text=True).strip()
    archive=output/'source.tar'
    subprocess.run(['git','archive','--format=tar','-o',str(archive),commit],cwd=ROOT,check=True)
    runtime=output/'runtime';runtime.mkdir()
    with tarfile.open(archive) as stream:stream.extractall(runtime,filter='data')
    archive.unlink()
    for name in FILES:shutil.copy2(ROOT/name,runtime/name)
    registration=json.loads((PARENT/'registration.json').read_text())
    registration['runtime_files']={str(p.relative_to(runtime)):sha(p) for p in runtime.rglob('*') if p.is_file()}
    write_json(output/'registration.json',registration)
    cases=[]
    for scope,start,end in [('history2024_2025',20240102,20251231),('reset2026',20260105,20260930)]:
        for cost in (25,40,60):
            for name in ARMS:cases.append(dict(scope=scope,start=start,end=end,cost=cost,strategy=name,order='rank'))
        for name in ('alpha_pool_sequential','alpha_industry_pool_sequential'):
            cases.append(dict(scope=scope,start=start,end=end,cost=25,strategy=name,order='rank'))
    for name in ('alpha_equal_risk','alpha_industry_equal_risk'):
        cases.append(dict(scope='reverse_control',start=20240102,end=20251231,cost=25,strategy=name,order='reverse'))
    protected=json.loads((PARENT/'experiment.json').read_text())['protected_account_hashes']
    write_json(output/'experiment.json',dict(registered_at=datetime.now().astimezone().isoformat(),
        committed_source=commit,new_code_sha256={name:sha(runtime/name) for name in FILES},cases=cases,
        initial_cash=1000000,minimum_excess=-.05,
        allocation_rule='At signal close, rank valid fresh entries and independently check original risk limits without reserving. Choose first N feasible names where N is available slots. Equally divide remaining portfolio/day risk among N, capped by per-trade risk and equal share of remaining industry risk. Floor board lots. Jointly shrink all requests by one common factor for total cash, gross and original stress caps. No redistribution of residual budget and no refill after planning/fill failures. Submit through unchanged risk/execution checks.',
        no_forward_information='Pool and quantities use signal-close information only. Pending sells are not treated as already filled. No next-open fillability screening.',
        controls='Two-by-two gate/allocation. Fixed-pool sequential control distinguishes pool truncation from allocation. At unchanged baseline states, replay same pool in rank/reverse/date-symbol-hash order for both allocation policies using isolated account copies. Full equal-risk reverse submission controls must preserve NAV and fills.',
        frozen_budgets='All risk_v1 ceilings unchanged; no add/scale-out/stop-sync changes.',
        historical_screen='Equal-risk without gate must improve total return at 25/40/60bp in both periods, improve each 2024/2025/2026 calendar return at 25bp without larger drawdown, and reduce <0.5%-equity entry share. Order invariance and golden checks must pass. Publish all results; do not select another parameter or seed. Never automatic admission.',
        limitations='All periods have previously been observed. 2026 reset is historical robustness, not an untouched holdout. Batch execution order invariance does not remove rank dependence of pool selection or whole-account path dependence.',
        protected_account_hashes={p:sha(p) for p in protected},new_holdout=False,automatic_admission=False))
    os.execv(sys.executable,[sys.executable,'-B',str(runtime/'scripts/validate_alpha158_batch_allocation_v1.py'),'--output',str(output),'--frozen'])


def frame(value):
    import pandas as pd
    return value if isinstance(value,pd.DataFrame) else pd.DataFrame([vars(x) if hasattr(x,'__dict__') else x for x in value])


def run(output):
    sys.path.insert(0,str(output/'runtime'))
    import numpy as np
    import pandas as pd
    from scripts import compare_current_strategies_v1 as c
    from scripts.backtest_alpha158_batch_allocation_v1 import run_low_turnover
    from scripts.validate_alpha158_industry_gate_2024_2025 import annual_metrics
    from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval
    from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview
    from abupy.AlphaBu.ABuAlphaIndustryGate import IndustryExcessHistory,IndustryExcessEntryGate
    from abupy.AlphaBu.ABuAlphaBatchAllocation import BatchRiskAllocator
    registration=json.loads((output/'registration.json').read_text())
    experiment=json.loads((output/'experiment.json').read_text())
    def verify():
        for p,h in registration['data_files'].items():
            if sha(p)!=h:raise ValueError('data changed: '+p)
        for p,h in registration['runtime_files'].items():
            if sha(output/'runtime'/p)!=h:raise ValueError('runtime changed: '+p)
        for p,h in experiment['protected_account_hashes'].items():
            if sha(p)!=h:raise ValueError('protected account changed: '+p)
    verify()
    comparison=c.Comparison(output)
    history=IndustryExcessHistory(comparison.panel,comparison.source)
    annual,execution,diagnostics,goldens,uncertainty=[],[],[],[],[]
    for case in experiment['cases']:
        name,scope,cost,begin,end=(case[k] for k in ('strategy','scope','cost','start','end'))
        print('RUN',scope,name,cost,case['order'],flush=True)
        scores=comparison.scores[comparison.scores.signal_asof.between(begin,end)]
        options={}
        gated='industry' in name
        overlay=IndustryExcessEntryGate(history,-.05) if gated else CostAwareReview(suppress_rank_exits=True)
        if 'equal_risk' in name:
            options['batch_allocator']=BatchRiskAllocator(submission_order=case['order'])
        elif 'pool_sequential' in name:
            options['batch_allocator']=BatchRiskAllocator(mode='sequential_pool')
        elif cost==25:
            options['batch_observer']=BatchRiskAllocator(diagnose=True)
        audit=run_low_turnover(comparison.panel,scores,replace(comparison.source,label_slippage_bps=cost),
            comparison.low,comparison.risk,end,review_overlay=overlay,**options)[1]
        if gated:audit['industry_entry_gate']=overlay.entry_decisions
        comparison.save(name,cost,audit,scope=scope,begin=begin,end=end)
        curve=comparison.navs[(scope,name,cost)]
        annual+=annual_metrics(curve,name,cost,scope)
        fills=audit['fills'];buys=fills[fills.status.eq('filled')&fills.side.eq('buy')]
        orders=frame(audit['orders'])
        funded=buys.merge(orders[['intent_id','portfolio_equity_asof']],on='intent_id',validate='one_to_one')
        weights=(funded.quantity*funded.fill_price_raw+funded.commission+funded.transfer_fee+funded.stamp_tax)/funded.portfolio_equity_asof
        execution.append(dict(scope=scope,strategy=name,slippage_bps=cost,buys=len(buys),
            tiny_buys=int((weights<.005).sum()),tiny_share_pct=float((weights<.005).mean()*100),
            median_entry_weight_pct=float(weights.median()*100),mean_entry_weight_pct=float(weights.mean()*100)))
        if 'batch_allocator' in options and options['batch_allocator'].mode=='equal_risk':
            plans=pd.DataFrame(audit['batch_plans'])
            expected=plans.set_index(['signal_asof','symbol']).planned_quantity
            actual=orders[orders.side.eq('buy')].set_index(['created_asof','symbol']).quantity
            for key,quantity in expected.items():
                assert int(actual.get(key,0))==quantity,'final order differs from plan'
        if 'batch_observer' in options:
            diag=pd.DataFrame(audit['order_diagnostics'])
            seq=diag[['sequential_rank','sequential_reverse','sequential_fixed_hash']]
            eq=diag[['equal_risk_rank','equal_risk_reverse','equal_risk_fixed_hash']]
            assert eq.max(axis=1).eq(eq.min(axis=1)).all()
            diag['sequential_order_sensitive']=seq.max(axis=1).ne(seq.min(axis=1))
            reviews=diag.groupby('signal_asof').sequential_order_sensitive.any()
            diagnostics.append(dict(scope=scope,strategy=name,reviews=len(reviews),
                order_sensitive_reviews=int(reviews.sum()),order_sensitive_review_pct=float(reviews.mean()*100),
                candidate_evaluations=len(diag),order_sensitive_candidates=int(diag.sequential_order_sensitive.sum()),
                equal_risk_order_sensitive_candidates=0))
        if scope=='history2024_2025' and name in ARMS[:2]:
            directory=PARENT/'continuous'/f'{name}_{cost}bp'
            columns=['date','cash','stocks','capital','exposure']
            pd.testing.assert_frame_equal(curve[columns],pd.read_csv(directory/'daily_nav.csv')[columns],check_dtype=False,rtol=1e-10,atol=1e-7)
            columns=['date','symbol','side','status','quantity','fill_price_raw','commission','transfer_fee','stamp_tax','slippage_cost']
            pd.testing.assert_frame_equal(fills[columns],pd.read_csv(directory/'fills.csv')[columns],check_dtype=False,rtol=1e-10,atol=1e-7)
            goldens.append(dict(strategy=name,slippage_bps=cost,passed=True))
        if scope=='reverse_control':
            reference=comparison.navs[('history2024_2025',name,cost)]
            columns=['date','cash','stocks','capital','exposure']
            pd.testing.assert_frame_equal(curve[columns],reference[columns],check_dtype=False,rtol=1e-10,atol=1e-7)
            ref=pd.read_csv(output/'history2024_2025'/f'{name}_{cost}bp'/'fills.csv')
            columns=['date','symbol','side','status','quantity','fill_price_raw','commission','transfer_fee','stamp_tax','slippage_cost']
            pd.testing.assert_frame_equal(fills[columns].sort_values(['date','symbol','side','status']).reset_index(drop=True),
                ref[columns].sort_values(['date','symbol','side','status']).reset_index(drop=True),check_dtype=False,rtol=1e-10,atol=1e-7)
            goldens.append(dict(strategy=name,control='reverse_submission',passed=True))
    for scope in ('history2024_2025','reset2026'):
        for left,right in [(ARMS[0],ARMS[1]),(ARMS[0],ARMS[2]),(ARMS[2],ARMS[3]),(ARMS[1],ARMS[3])]:
            a=comparison.navs[(scope,left,25)].capital.to_numpy()
            b=comparison.navs[(scope,right,25)].capital.to_numpy()
            uncertainty.append(dict(scope=scope,baseline=left,candidate=right,**paired_block_interval(a,b)))
    pd.DataFrame(annual).to_csv(output/'annual_metrics.csv',index=False)
    pd.DataFrame(execution).to_csv(output/'entry_statistics.csv',index=False)
    pd.DataFrame(diagnostics).to_csv(output/'order_sensitivity.csv',index=False)
    pd.DataFrame(uncertainty).to_csv(output/'paired_uncertainty.csv',index=False)
    write_json(output/'golden_checks.json',goldens)
    summarize(output,comparison,pd.DataFrame(annual),pd.DataFrame(execution),pd.DataFrame(diagnostics))
    verify()
    write_json(output/'completion.json',dict(status='COMPLETE',strategy_runs=len(comparison.rows),
        inputs_unchanged=True,code_unchanged=True,protected_accounts_unchanged=True,
        golden_checks=len(goldens),new_holdout=False,automatic_admission=False))
    print('COMPLETE',flush=True)


def summarize(output,comparison,annual,execution,diagnostics):
    import pandas as pd
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    results=pd.DataFrame(comparison.rows)
    criteria={}
    for scope in ('history2024_2025','reset2026'):
        for cost in (25,40,60):
            selected=results[(results.scope==scope)&(results.slippage_bps==cost)].set_index('strategy')
            criteria[f'{scope}_{cost}bp_return']=bool(selected.loc[ARMS[2],'return_pct']>selected.loc[ARMS[0],'return_pct'])
        stats=execution[(execution.scope==scope)&(execution.slippage_bps==25)].set_index('strategy')
        criteria[f'{scope}_tiny_entries']=bool(stats.loc[ARMS[2],'tiny_share_pct']<stats.loc[ARMS[0],'tiny_share_pct'])
        years=annual[(annual.scope==scope)&(annual.slippage_bps==25)]
        for year,group in years.groupby('year'):
            group=group.set_index('strategy')
            criteria[f'{year}_return']=bool(group.loc[ARMS[2],'return_pct']>group.loc[ARMS[0],'return_pct'])
            criteria[f'{year}_drawdown']=bool(group.loc[ARMS[2],'max_drawdown_pct']>=group.loc[ARMS[0],'max_drawdown_pct'])
    write_json(output/'historical_screen.json',dict(passed=all(criteria.values()),criteria=criteria,automatic_admission=False))
    lines=['# 候选顺序与风险预算分配：固定规则验证','',
        '主实验为行业过滤开/关 × 原顺序分配/等风险批量分配。所有风险上限、信号、退出规则保持一致。2024—2025 连续账户与 2026-01-05 至 2026-09-30 空仓重启各独立投入 100 万。全部为已观察历史，不是新的样本外验证。','',
        '等风险规则先按原排名选出与空余持仓名额相同数量、在当前账户下单独可通过风控的候选；在候选间等分剩余组合及当日风险预算，同一行业再受行业剩余额度均分约束。按整手向下取整；现金、总仓位和压力约束不足时统一比例缩小。余量不重新分给排前股票，失败不在当轮补位，挂出的卖单不提前释放风险。','',
        '## 完整主结果（25 bp）','','| 范围 | 版本 | 收益 | 最大回撤幅度 | 平均仓位 | 买入笔数 |','|---|---|---:|---:|---:|---:|']
    for r in results[(results.slippage_bps==25)&results.scope.ne('reverse_control')].itertuples():
        lines.append(f'| {r.scope} | {LABELS[r.strategy]} | {r.return_pct:.2f}% | {abs(r.max_drawdown_pct):.2f}% | {r.average_exposure_pct:.2f}% | {r.filled_buys} |')
    lines+=['','## 逐年结果（25 bp）','','| 范围 | 年份 | 版本 | 收益 | 最大回撤幅度 |','|---|---|---|---:|---:|']
    for r in annual[(annual.slippage_bps==25)&annual.scope.ne('reverse_control')&annual.strategy.isin(ARMS)].itertuples():
        lines.append(f'| {r.scope} | {r.year} | {LABELS[r.strategy]} | {r.return_pct:.2f}% | {abs(r.max_drawdown_pct):.2f}% |')
    lines+=['','## 成本压力：仅分配规则变化','','| 范围 | 滑点 bp | 当前收益 | 等分收益 | 当前回撤幅度 | 等分回撤幅度 |','|---|---:|---:|---:|---:|---:|']
    for scope in ('history2024_2025','reset2026'):
        for cost in (25,40,60):
            group=results[(results.scope==scope)&(results.slippage_bps==cost)].set_index('strategy')
            a,b=group.loc[ARMS[0]],group.loc[ARMS[2]]
            lines.append(f'| {scope} | {cost} | {a.return_pct:.2f}% | {b.return_pct:.2f}% | {abs(a.max_drawdown_pct):.2f}% | {abs(b.max_drawdown_pct):.2f}% |')
    lines+=['','## 顺序敏感性（同一个真实账户状态、同一候选池）','','| 范围 | 版本 | 可评审次数 | 顺序改变会影响数量 | 比例 | 等分规则受影响候选数 |','|---|---|---:|---:|---:|---:|']
    for r in diagnostics.itertuples():
        lines.append(f'| {r.scope} | {LABELS[r.strategy]} | {r.reviews} | {r.order_sensitive_reviews} | {r.order_sensitive_review_pct:.1f}% | {r.equal_risk_order_sensitive_candidates} |')
    lines+=['','三种固定顺序为排名、倒序、日期与股票代码哈希；不挑选最优种子。隔离账户复制后只模拟审批，不使用未来成交或收益。该结果仅检验固定池内的提交顺序；候选池的选取仍依赖模型排名，真实后续账户路径也可能不同。','',
        '## 零碎仓位（入场投入不足权益 0.5%）','','| 范围 | 版本 | 小仓位笔数 / 全部买入 | 占比 | 入场权重中位数 |','|---|---|---:|---:|---:|']
    for r in execution[(execution.slippage_bps==25)&execution.scope.ne('reverse_control')&execution.strategy.isin(ARMS)].itertuples():
        lines.append(f'| {r.scope} | {LABELS[r.strategy]} | {r.tiny_buys}/{r.buys} | {r.tiny_share_pct:.1f}% | {r.median_entry_weight_pct:.2f}% |')
    lines+=['','## 预登记历史筛选','',f'总体：{"通过" if all(criteria.values()) else "未通过"}。']
    lines += [f'- {k}: {"通过" if v else "未通过"}' for k,v in criteria.items()]
    lines+=['','固定候选池＋顺序分配是机制对照，不是另一个待挑选的最优策略。全路径对照不能保持跨日持仓完全一致，只能控制规则；同状态审批诊断才隔离了即时顺序效应。详细成本、逐日净值、成交、计划、风控日志、配对区块重采样区间和全部控制组均保留。本轮没有前瞻准入或默认策略变更。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n')
    fig,axes=plt.subplots(2,1,figsize=(12,8),constrained_layout=True)
    english=['Current','Industry gate','Equal risk','Gate + equal risk']
    for ax,scope in zip(axes,('history2024_2025','reset2026')):
        for name,label in zip(ARMS,english):
            curve=comparison.navs[(scope,name,25)]
            ax.plot(pd.to_datetime(curve.date.astype(str)),(curve.capital/1e6-1)*100,label=label)
        ax.set(title=scope+' | fresh initial cash 1m | 25 bp',ylabel='Cumulative return (%)')
        ax.grid(alpha=.2);ax.legend(fontsize=9);ax.axhline(0,color='black',linewidth=.5)
    fig.savefig(output/'comparison.png',dpi=150);plt.close(fig)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=OUTPUT)
    parser.add_argument('--frozen',action='store_true')
    args=parser.parse_args()
    if args.frozen:run(args.output.resolve())
    else:freeze(args.output.resolve())
