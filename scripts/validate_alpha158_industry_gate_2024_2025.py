#!/usr/bin/env python3
"""Frozen historical robustness comparison; no parameter search or admission."""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PARENT = Path('/Users/wjy/abu/backtests/current_strategy_comparison_20261004_v2')
OUTPUT = Path('/Users/wjy/abu/backtests/alpha158_industry_gate_2024_2025_20261004')
START, END = 20240102, 20251231
STRATEGIES = ('alpha_v3', 'alpha_no_rank_exit', 'alpha_industry_gate')
LABELS = dict(alpha_v3='原始低换手策略', alpha_no_rank_exit='当前候选（取消排名退出）',
              alpha_industry_gate='候选＋行业超额过滤', CSI300_price_index_gross='沪深300价格指数（无交易成本）')
NEW_FILES = ('abupy/AlphaBu/ABuAlphaIndustryGate.py',
             'tests/test_alpha_industry_gate.py',
             'scripts/validate_alpha158_industry_gate_2024_2025.py')


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')


def freeze(output):
    output.mkdir(parents=True, exist_ok=False)
    archive = output/'source.tar'
    commit = subprocess.check_output(['git', 'rev-parse', '0b3c997^{commit}'], cwd=ROOT, text=True).strip()
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), commit], cwd=ROOT, check=True)
    runtime = output/'runtime'
    runtime.mkdir()
    with tarfile.open(archive) as stream:
        stream.extractall(runtime, filter='data')
    archive.unlink()
    for name in NEW_FILES:
        shutil.copy2(ROOT/name, runtime/name)
    registration = json.loads((PARENT/'registration.json').read_text())
    registration['runtime_files'] = {str(p.relative_to(runtime)): sha(p) for p in runtime.rglob('*') if p.is_file()}
    write_json(output/'registration.json', registration)
    cases = [dict(scope='continuous', start=START, end=END, strategy=name, cost=cost)
             for cost in (25, 40, 60) for name in STRATEGIES]
    cases += [dict(scope='reset2025', start=20250102, end=END, strategy=name, cost=25)
              for name in STRATEGIES]
    previous = Path('/Users/wjy/abu/backtests/alpha158_case_study_2025_2026_20261004/case_registration.json')
    protected = json.loads(previous.read_text())['protected_account_hashes']
    write_json(output/'experiment.json', dict(
        registered_at=datetime.now().astimezone().isoformat(), committed_source=commit,
        new_code_sha256={name: sha(runtime/name) for name in NEW_FILES},
        cases=cases, initial_cash=1000000, minimum_excess=-0.05,
        hypothesis='Reject new entries whose raw 20-session return minus contemporary industry mean is below -5 percentage points; missing values rejected; keep rank order and persistence; no holding exit from this gate.',
        unchanged='ML walk-forward predictions, risk budgets, 10 holdings, event exits, review frequency, transaction rules; no sizing/add/scale-out/dynamic stop synchronization changes',
        provenance='Hypothesis came from 2025-2026 case study. -5pp coarsens 2025 trade median -5.138pp. Neither 2024 nor 2025 is a new untouched holdout. No threshold search.',
        review_rule='Report every registered arm. Before advancing the filter, require positive incremental return and no larger absolute drawdown in both calendar years at 25bp, positive full-period increment at every cost, and positive reset2025 increment. These are historical screens only, never admission.',
        uncertainty='Paired 20-session circular-block bootstrap, 5000 replicates, seed20261004; descriptive intervals, not a significance/admission test',
        protected_account_hashes={p: sha(p) for p in protected},
        research_only=True, new_holdout=False, automatic_admission=False))
    os.execv(sys.executable, [sys.executable, '-B', str(runtime/NEW_FILES[-1]), '--output', str(output), '--frozen'])


def annual_metrics(curve, name, cost, scope):
    import numpy as np
    rows = []
    previous = 1e6
    for year, frame in curve.groupby(curve.date.astype(int)//10000, sort=True):
        values = np.r_[previous, frame.capital.to_numpy(float)]
        rows.append(dict(strategy=name, scope=scope, slippage_bps=cost, year=int(year),
            return_pct=float((values[-1]/values[0]-1)*100),
            max_drawdown_pct=float((values/np.maximum.accumulate(values)-1).min()*100),
            average_exposure_pct=float(frame.exposure.mean()*100)))
        previous = values[-1]
    return rows


def report(output, comparison, experiment, annual, intervals, gate_stats):
    import numpy as np
    import pandas as pd
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    results = pd.DataFrame(comparison.rows)
    annual = pd.DataFrame(annual)
    annual.to_csv(output/'annual_metrics.csv', index=False)
    intervals = pd.DataFrame(intervals)
    intervals.to_csv(output/'paired_uncertainty.csv', index=False)
    pd.DataFrame(gate_stats).to_csv(output/'gate_summary.csv', index=False)
    main = results[(results.scope=='continuous') & (results.slippage_bps==25)].set_index('strategy')
    base, filtered = main.loc['alpha_no_rank_exit'], main.loc['alpha_industry_gate']
    year = annual[(annual.scope=='continuous') & (annual.slippage_bps==25)].set_index(['strategy', 'year'])
    criteria = {}
    for y in (2024, 2025):
        a, b = year.loc[('alpha_no_rank_exit', y)], year.loc[('alpha_industry_gate', y)]
        criteria[f'{y}_positive_increment'] = bool(b.return_pct > a.return_pct)
        criteria[f'{y}_no_larger_drawdown'] = bool(b.max_drawdown_pct >= a.max_drawdown_pct)
    for cost in (25, 40, 60):
        selected = results[(results.scope=='continuous') & (results.slippage_bps==cost)].set_index('strategy')
        criteria[f'{cost}bp_positive_increment'] = bool(selected.loc['alpha_industry_gate', 'return_pct'] > selected.loc['alpha_no_rank_exit', 'return_pct'])
    reset = results[(results.scope=='reset2025') & (results.slippage_bps==25)].set_index('strategy')
    criteria['reset2025_positive_increment'] = bool(reset.loc['alpha_industry_gate','return_pct'] > reset.loc['alpha_no_rank_exit','return_pct'])
    passed = all(criteria.values())
    write_json(output/'historical_screen.json', dict(criteria=criteria, passed=passed, automatic_admission=False))
    lines = ['# 2024—2025 年策略历史稳定性验证', '',
        '执行器和当前候选冻结于提交 `0b3c997`；仅叠加独立行业入场过滤。每组初始资金 100 万，主实验自 2024-01-02 连续运行至 2025-12-31，跨年持仓不清空；另注册 2025 年空仓重启对照。', '',
        '过滤规则：信号收盘时，个股近 20 日收益减去当时所属行业均值 ≥ -5 个百分点。使用原始特征而非截面排名；缺失值拒绝；保持候选排序、连续入围要求和所有原有风控。只限制新买入，不据此卖出现有持仓。行业口径沿用特征引擎中当日符合历史长度和状态条件的股票等权均值，并非行业指数。', '',
        '**此轮不是全新的样本外验证。** 行业过滤假设来自已分析的 2025—2026 年案例，-5pp 是对 2025 年样本中位数 -5.138pp 的粗化；2024—2025 年也曾出现在此前的策略比较中。本轮只检验历史稳定性，未搜索参数，未新增前瞻样本。模型沿用逐期历史训练的预测，逐行检查 train_end < signal_asof。', '',
        '## 连续两年账户', '',
        '| 策略 | 滑点 bp | 累计收益 | 年化收益 | 最大回撤幅度 | 平均仓位 | 买入笔数 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for row in results[results.scope.isin(['continuous','benchmark'])].itertuples():
        lines.append(f'| {LABELS[row.strategy]} | {row.slippage_bps} | {row.return_pct:.2f}% | {row.cagr_pct:.2f}% | {abs(row.max_drawdown_pct):.2f}% | {row.average_exposure_pct:.2f}% | {row.filled_buys} |')
    lines += ['', '沪深300为价格指数，未计交易成本和股息再投资；策略含执行成本和已到账现金分红，风险敞口差异很大，不能将收益差直接等同于选股能力差。', '', '## 分年结果（连续账户，25 bp）', '',
        '| 年份 | 策略 | 当年收益 | 当年最大回撤幅度 | 平均仓位 |', '|---|---|---:|---:|---:|']
    for row in annual[(annual.scope=='continuous') & (annual.slippage_bps==25)].sort_values(['year','strategy']).itertuples():
        lines.append(f'| {row.year} | {LABELS[row.strategy]} | {row.return_pct:.2f}% | {abs(row.max_drawdown_pct):.2f}% | {row.average_exposure_pct:.2f}% |')
    lines += ['', '分年收益使用上一年末权益作为起点，回撤也包含该起点。2025 年结果包含从 2024 年带入的持仓。', '', '## 2025 年空仓重启（25 bp）', '',
        '| 策略 | 收益 | 最大回撤幅度 | 平均仓位 |', '|---|---:|---:|---:|']
    for row in results[results.scope=='reset2025'].itertuples():
        lines.append(f'| {LABELS[row.strategy]} | {row.return_pct:.2f}% | {abs(row.max_drawdown_pct):.2f}% | {row.average_exposure_pct:.2f}% |')
    lines += ['', '## 增量与不确定性', '',
        '| 比较（25 bp） | 范围 | 收益增量 pp | 配对区块重采样 95% 区间 pp |', '|---|---|---:|---:|']
    for row in intervals.itertuples():
        lines.append(f'| {LABELS[row.candidate]} vs {LABELS[row.baseline]} | {row.period} | {row.observed_increment_pp:.2f} | [{row.ci95_low_pp:.2f}, {row.ci95_high_pp:.2f}] |')
    lines += ['', '区间来自每日账户收益的配对 20 日区块重采样（5000 次），仅描述已观察路径的不确定性，不修正先前多次试验及规则选择偏差，也不是重新执行每条重采样交易路径。', '',
        '## 预先登记的历史检查', '',
        f'- 两年累计收益增量：{filtered.return_pct-base.return_pct:+.2f} 个百分点。',
        f'- 两年最大回撤幅度变化：{abs(filtered.max_drawdown_pct)-abs(base.max_drawdown_pct):+.2f} 个百分点（负值为改善）。',
        f'- 平均仓位变化：{filtered.average_exposure_pct-base.average_exposure_pct:+.2f} 个百分点。',
        f'- 全部历史检查：{"通过" if passed else "未通过"}。']
    lines += [f'- {name}: {"通过" if value else "未通过"}' for name,value in criteria.items()]
    lines += ['', '预登记标准：2024、2025 各年都提高收益且不扩大回撤；全部成本档累计增量为正；2025 空仓重启增量为正。即使全部满足，也仍需独立前瞻验证；任何不满足都不通过本轮筛选。', '',
        '完整候选过滤日志、逐日净值、委托、成交、拒绝原因和持仓账本保存在各实验目录。模型、行情、执行源码与受保护账户的哈希在前后核对。没有提高风险预算，没有修改实盘或冻结前瞻账户。']
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n')
    colors = dict(alpha_v3='#79808c', alpha_no_rank_exit='#2563eb', alpha_industry_gate='#d97706')
    english = dict(alpha_v3='Original v3', alpha_no_rank_exit='Current candidate', alpha_industry_gate='Candidate + industry filter')
    fig, axes = plt.subplots(2,1,figsize=(12,8),sharex=True,constrained_layout=True)
    for name in STRATEGIES:
        frame = comparison.navs[('continuous',name,25)]
        dates = pd.to_datetime(frame.date.astype(str))
        nav = frame.capital.to_numpy()/1e6
        axes[0].plot(dates,(nav-1)*100,label=english[name],color=colors[name])
        axes[1].plot(dates,(nav/np.maximum.accumulate(np.r_[1.,nav])[1:]-1)*100,color=colors[name])
    bench = comparison.navs[('benchmark','CSI300_price_index_gross',0)]
    axes[0].plot(pd.to_datetime(bench.date.astype(str)),(bench.capital/1e6-1)*100,label='CSI300 price index (gross)',color='#b9bec6',linestyle='--')
    axes[0].set(title='2024-2025 historical robustness | fixed rules | 25 bp slippage',ylabel='Cumulative return (%)')
    axes[1].set(ylabel='Strategy drawdown (%)',xlabel='Date')
    axes[0].legend(loc='upper left',fontsize=9)
    for ax in axes:
        ax.grid(alpha=.2)
        ax.axhline(0,color='black',linewidth=.5)
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    fig.savefig(output/'comparison.png',dpi=160)
    plt.close(fig)


def run(output):
    sys.path.insert(0,str(output/'runtime'))
    import numpy as np
    import pandas as pd
    from scripts import compare_current_strategies_v1 as c
    from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval
    from abupy.AlphaBu.ABuAlphaIndustryGate import IndustryExcessHistory, IndustryExcessEntryGate
    registration = json.loads((output/'registration.json').read_text())
    experiment = json.loads((output/'experiment.json').read_text())
    def verify():
        for p,h in registration['data_files'].items():
            if sha(p)!=h: raise ValueError('input changed: '+p)
        for p,h in registration['runtime_files'].items():
            if sha(output/'runtime'/p)!=h: raise ValueError('runtime changed: '+p)
        for p,h in experiment['protected_account_hashes'].items():
            if sha(p)!=h: raise ValueError('protected account changed: '+p)
    verify()
    # Retain the original panel length/column mapping so frozen predictions align.
    # Feature methods select <= signal day; later prices never enter gate decisions.
    comparison = c.Comparison(output)
    history = IndustryExcessHistory(comparison.panel, comparison.source)
    annual, gate_stats = [], []
    for case in experiment['cases']:
        name,cost,scope,begin,end = (case[k] for k in ('strategy','cost','scope','start','end'))
        print(f'RUN {scope} {name} {cost}bp',flush=True)
        options = {}
        if name=='alpha_industry_gate':
            gate = IndustryExcessEntryGate(history, experiment['minimum_excess'])
            options['review_overlay'] = gate
        audit = comparison.alpha(name,cost,begin=begin,end=end,**options)
        if name=='alpha_industry_gate':
            decisions = pd.DataFrame(gate.entry_decisions)
            audit['industry_entry_gate'] = decisions
            allowed = set(zip(decisions.loc[decisions.allowed,'signal_asof'], decisions.loc[decisions.allowed,'symbol']))
            orders = pd.DataFrame([vars(x) for x in audit['orders']])
            buys = orders[orders.side.eq('buy')]
            assert all((int(r.created_asof),r.symbol) in allowed for r in buys.itertuples()), 'buy escaped entry gate'
            gate_stats.append(dict(scope=scope, slippage_bps=cost, evaluations=len(decisions),
                rejected=int((~decisions.allowed).sum()), allowed=int(decisions.allowed.sum()),
                missing=int(decisions.stock_excess_industry_20d.isna().sum()),
                actual_buy_orders=len(buys), buy_gate_verified=True))
        comparison.save(name,cost,audit,scope=scope,begin=begin,end=end)
        curve = comparison.navs[(scope,name,cost)]
        annual += annual_metrics(curve,name,cost,scope)
        if scope=='reset2025' and name=='alpha_no_rank_exit':
            path=Path('/Users/wjy/abu/backtests/alpha158_case_study_2025_2026_20261004/case2025/alpha_no_rank_exit_25bp/daily_nav.csv')
            reference=pd.read_csv(path)
            columns=['date','cash','stocks','capital','exposure']
            pd.testing.assert_frame_equal(curve[columns], reference.loc[reference.date<=END,columns].reset_index(drop=True),check_dtype=False,rtol=1e-10,atol=1e-7)
            write_json(output/'baseline_golden.json',dict(passed=True,reference=str(path)))
    dates=comparison.panel.dates
    mask=(dates>=START)&(dates<=END)
    values=comparison.panel.benchmark_close[mask]
    bench=pd.DataFrame(dict(date=dates[mask],capital=values/values[0]*1e6,exposure=1.))
    comparison.save('CSI300_price_index_gross',0,dict(curve=bench,fills=pd.DataFrame()),scope='benchmark',begin=START,end=END)
    annual += annual_metrics(bench,'CSI300_price_index_gross',0,'benchmark')
    intervals=[]
    for baseline,candidate in [('alpha_v3','alpha_no_rank_exit'),('alpha_no_rank_exit','alpha_industry_gate')]:
        for period in ('2024-2025','2024','2025','reset2025'):
            scope='reset2025' if period=='reset2025' else 'continuous'
            left=comparison.navs[(scope,baseline,25)]
            right=comparison.navs[(scope,candidate,25)]
            assert np.array_equal(left.date,right.date)
            if period in ('2024','2025'):
                indices=np.flatnonzero(left.date.to_numpy()//10000==int(period))
                first,last=indices[0],indices[-1]
                a=np.r_[float(left.capital.iloc[first-1]) if first else 1e6,left.capital.iloc[first:last+1]]
                b=np.r_[float(right.capital.iloc[first-1]) if first else 1e6,right.capital.iloc[first:last+1]]
            else:
                a,b=left.capital.to_numpy(),right.capital.to_numpy()
            intervals.append(dict(baseline=baseline,candidate=candidate,period=period,**paired_block_interval(a,b)))
    report(output,comparison,experiment,annual,intervals,gate_stats)
    verify()
    write_json(output/'completion.json',dict(status='COMPLETE',strategy_runs=12,benchmark_runs=1,
        inputs_unchanged=True,runtime_unchanged=True,protected_accounts_unchanged=True,
        buy_gate_verified=True,new_holdout=False,automatic_admission=False))
    print('COMPLETE',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=OUTPUT)
    parser.add_argument('--frozen',action='store_true')
    args=parser.parse_args()
    if args.frozen:
        run(args.output.resolve())
    else:
        freeze(args.output.resolve())
