#!/usr/bin/env python3
"""Registered exposure ablation on the previous comparison's frozen runtime.

No production policy edits, model refits, or forward-account writes.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

PARENT = Path('/Users/wjy/abu/backtests/current_strategy_comparison_20261004_v2')
OUTPUT = Path('/Users/wjy/abu/backtests/alpha158_exposure_ablation_20261004')
ARMS = (
    ('alpha_no_rank_exit', False, 1.0),
    ('sync_risk_1x', True, 1.0),
    ('sync_risk_1_5x', True, 1.5),
    ('sync_risk_2x', True, 2.0),
)
BUDGET_FIELDS = ('single_trade_risk_fraction', 'portfolio_open_risk_fraction',
                 'industry_open_risk_fraction', 'same_day_new_risk_fraction')
COSTS = (25, 40, 60)
NAMES = {'alpha_no_rank_exit': '当前候选（不同步）', 'sync_risk_1x': '仅同步止损',
         'sync_risk_1_5x': '同步＋风险预算1.5倍', 'sync_risk_2x': '同步＋风险预算2倍'}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                    default=str) + '\n')


def scaled_risk(config, multiplier):
    if multiplier not in (1.0, 1.5, 2.0):
        raise ValueError('risk multiplier outside registered experiment')
    return replace(config, **{key: getattr(config, key) * multiplier
                             for key in BUDGET_FIELDS})


def account_files():
    base = Path('/Users/wjy/abu/shadow/alpha158_forward_v1/genesis')
    commit = json.loads((base / 'commit.json').read_text())
    paths = [base / name for name in commit['files']]
    paths += [base / 'commit.json', base.parent / 'summary.json',
              Path('/Users/wjy/abu/paper/vcp_residual_v2/state.json')]
    return {str(path): digest(path) for path in paths}


def register(parent, output):
    output.mkdir(parents=True, exist_ok=False)
    runner = output / 'registered_runner.py'
    shutil.copy2(__file__, runner)
    shutil.copy2(parent / 'registration.json', output / 'registration.json')
    write_json(output / 'experiment_registration.json', {
        'registered_at': datetime.now().astimezone().isoformat(),
        'parent': str(parent), 'parent_registration_sha256': digest(parent / 'registration.json'),
        'runner_sha256': digest(runner), 'arms': ARMS, 'costs': COSTS,
        'budget_fields_scaled_together': BUDGET_FIELDS,
        'fixed_constraints': '10 positions, 8% single name, 80% gross, 6% stress; same signals/exits/entry schedule; no adds or scale-outs',
        'primary_question': 'Does synchronizing executable trailing stops alone release exposure and improve the existing no-rank-exit candidate?',
        'secondary_question': 'What is the return/drawdown tradeoff of proportional 1.5x and 2x risk budgets after synchronization?',
        'interpretation': 'One fixed sensitivity experiment, not an optimizer. All 12 cases reported, no selection or further tuning based on results.',
        'robustness': 'Costs 25/40/60bp, continuous annual slices, paired 20-session block bootstrap 5000 replicates seed20261004, three-limit markdown',
        'strict_historical_screen': 'Higher return and no worse maximum drawdown than current candidate in every cost scenario; positive 25bp incremental CI lower bound. Descriptive only, no automatic admission.',
        'reused_history': True, 'new_holdout': False, 'model_refit': False,
        'automatic_admission': False, 'protected_account_hashes': account_files(),
    })
    os.execv(sys.executable, [sys.executable, '-B', str(runner), '--parent',
                            str(parent), '--output', str(output), '--registered'])


def diagnostics(audit, curve):
    import pandas as pd
    decisions = pd.DataFrame([asdict(x) if hasattr(x, '__dataclass_fields__') else x
                              for x in audit['decisions']])
    counts = Counter()
    for value in decisions.loc[decisions.decision.eq('rejected'), 'reason_codes']:
        counts.update(ast.literal_eval(value) if isinstance(value, str) else value)
    states = pd.DataFrame(audit['risk_states_daily'])
    return dict(average_holdings=float(curve.holdings.mean()),
                maximum_exposure_pct=float(curve.exposure.max()*100),
                approval_attempts=len(decisions), rejected=int(decisions.decision.eq('rejected').sum()),
                reduced=int(decisions.decision.eq('reduced').sum()),
                rejected_portfolio_risk=counts['PORTFOLIO_OPEN_RISK'],
                rejected_same_day_risk=counts['SAME_DAY_NEW_RISK'],
                rejected_industry_risk=counts['INDUSTRY_OPEN_RISK'],
                rejected_stress=counts['STRESS_LOSS'],
                average_open_risk_pct=float((states.open_risk_cash/states.equity).mean()*100))


def report(output, rows, pairs, annual):
    import pandas as pd
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    frame = pd.DataFrame(rows)
    frame.to_csv(output/'results.csv', index=False)
    pd.DataFrame(pairs).to_csv(output/'paired_uncertainty.csv', index=False)
    annual.to_csv(output/'annual_returns.csv', index=False)
    text = ['# 低仓位机制与风险预算实验（2026-10-04）', '',
            '沿用上轮冻结的代码、数据和滚动OOS预测。2023-07-27至2026-09-30，各100万元、跨年连续资金，现金不计息。所有版本取消排名退出，保留相同事件退出；最多10只，不加仓、不分批止盈。', '',
            '首先只改变动态止损向风险账本同步；其次用一个比例参数，同时放大单笔、组合、行业、同日新增风险预算至1.5倍或2倍。单股8%、总仓位80%、压力损失6%等其他上限固定。12个情景事先登记，全部披露。', '',
            '这轮仍复用已研究历史，不是新样本外检验；不得凭排名自动选参数或更改前瞻账户。', '',
            '| 版本 | 单边滑点bp | 累计收益% | 年化收益% | 最大回撤% | 平均仓位% | 平均持股数 | 买入笔数 | 摩擦/初始资金% |',
            '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for r in frame.itertuples():
        text.append(f'| {NAMES[r.strategy]} | {r.slippage_bps} | {r.return_pct:.2f} | {r.cagr_pct:.2f} | {r.max_drawdown_pct:.2f} | {r.average_exposure_pct:.2f} | {r.average_holdings:.2f} | {r.filled_buys} | {r.total_friction_pct_initial:.2f} |')
    text += ['', '## 风险拒绝与压力估值（25bp）', '',
             '拒绝原因可重叠；审批尝试不是独立信号。组合风险额度是新买单准入限制，并非每日强制减仓线。三次连续跌停估值按各持仓板块跌幅计算，不是假定可以卖出，也不是事件概率预测。', '',
             '| 版本 | 审批尝试 | 拒绝 | 组合额度拒绝 | 同日额度拒绝 | 行业额度拒绝 | 压力额度拒绝 | 三连跌停终值收益% | 压力最大回撤% |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for r in frame[frame.slippage_bps.eq(25)].itertuples():
        text.append(f'| {NAMES[r.strategy]} | {r.approval_attempts} | {r.rejected} | {r.rejected_portfolio_risk} | {r.rejected_same_day_risk} | {r.rejected_industry_risk} | {r.rejected_stress} | {r.stress_return_pct:.2f} | {r.stress_drawdown_pct:.2f} |')
    text += ['', '## 配对增量', '', '20交易日块、5000次固定种子配对重采样，未作多重比较校正。不是独立显著性证据。回撤改善为正表示改善。成本情景重新撮合，成交路径改变，收益不必随成本单调。', '',
             '| 基线 | 候选 | 滑点bp | 收益增量pp | 回撤改善pp | 收益增量区间下界pp | 上界pp |',
             '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for p in pairs:
        text.append(f"| {NAMES[p['baseline']]} | {NAMES[p['candidate']]} | {p['slippage_bps']} | {p['observed_increment_pp']:.2f} | {p['drawdown_improvement_pp']:.2f} | {p['ci95_low_pp']:.2f} | {p['ci95_high_pp']:.2f} |")
    text += ['', '## 连续年度切片（25bp）', '', '2023和2026不是完整年度；各年不重置资金。', '',
             '| 版本 | 年份 | 年度收益% |', '| --- | ---: | ---: |']
    for r in annual[annual.slippage_bps.eq(25)].itertuples():
        text.append(f'| {NAMES[r.strategy]} | {r.year} | {r.return_pct:.2f} |')
    screens = []
    for name, _, _ in ARMS[1:]:
        candidates = [p for p in pairs if p['candidate']==name and p['baseline']==ARMS[0][0]]
        passed = all(p['observed_increment_pp']>0 and p['drawdown_improvement_pp']>=0 for p in candidates)
        passed &= next(p['ci95_low_pp'] for p in candidates if p['slippage_bps']==25)>0
        screens.append(dict(strategy=name, strict_historical_screen=bool(passed), automatic_admission=False))
    write_json(output/'historical_screen.json', screens)
    text += ['', '严格历史筛查要求：每个成本情景均提高收益且不扩大最大回撤，并且25bp收益增量区间下界为正。即使通过，也不等于通过未来验证。', '',
             *[f"- {NAMES[s['strategy']]}：{'通过' if s['strict_historical_screen'] else '未通过'}。" for s in screens], '',
             '审计文件：experiment_registration.json、registration.json、baseline_golden.json、completion.json，以及main下完整逐日净值、成交、订单、仓位和风控记录。', '',
             '限制：日线撮合、历史证券状态和公司行为仍沿用原研究数据局限；抬高止损不保证跳空时按止损价成交。报告不改变任何影子或模拟账户。']
    (output/'REPORT.md').write_text('\n'.join(text)+'\n')
    plt.style.use('default')
    fig, axes = plt.subplots(2,2,figsize=(13,9))
    for name, _, _ in ARMS:
        curve = pd.read_csv(output/'main'/f'{name}_25bp/daily_nav.csv')
        dates = pd.to_datetime(curve.date.astype(str))
        label = name.replace('alpha_no_rank_exit','Baseline: no rank exits')
        axes[0,0].plot(dates,curve.capital/1e6,label=label)
        axes[0,1].plot(dates,(curve.capital/curve.capital.cummax()-1)*100,label=label)
        axes[1,0].plot(dates,curve.exposure*100,label=label)
        subset = frame[frame.strategy.eq(name)]
        axes[1,1].plot(subset.slippage_bps,subset.return_pct,marker='o',label=label)
    for ax,title in zip(axes.flat,['Continuous NAV (25bp)','Drawdown % (25bp)','Exposure % (25bp)','Return % by slippage per side']):
        ax.set_title(title);ax.grid(alpha=.2)
    axes[0,0].legend(fontsize=8)
    for ax in (axes[0,0],axes[0,1],axes[1,0]):
        ax.xaxis.set_major_locator(mdates.MonthLocator(bymonth=(1,7)))
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        ax.tick_params(axis='x',labelsize=9)
    axes[1,1].set_xticks(COSTS)
    fig.tight_layout();fig.savefig(output/'exposure_ablation.png',dpi=170);plt.close(fig)


def run(parent, output):
    sys.path.insert(0,str(parent/'runtime'))
    import pandas as pd
    from scripts import compare_current_strategies_v1 as c
    registration = json.loads((output/'experiment_registration.json').read_text())
    if digest(__file__)!=registration['runner_sha256']:
        raise ValueError('registered runner changed')
    inherited = json.loads((output/'registration.json').read_text())
    def verify_data():
        changed = [p for p,h in inherited['data_files'].items() if digest(p)!=h]
        if changed: raise ValueError('registered inputs changed: '+str(changed[:5]))
    verify_data()
    comparison = c.Comparison(output)
    base_risk = comparison.risk
    rows, golden = [], []
    for cost in COSTS:
        for name, sync, multiplier in ARMS:
            print(f'RUN {name} cost={cost} risk={multiplier} sync={sync}',flush=True)
            comparison.risk = scaled_risk(base_risk,multiplier)
            audit = comparison.alpha('alpha_no_rank_exit',cost,sync_dynamic_stops=sync)
            comparison.save(name,cost,audit)
            curve = comparison.navs[('main',name,cost)]
            if name=='alpha_no_rank_exit':
                previous = parent/'main'/f'{name}_{cost}bp'
                pd.testing.assert_frame_equal(curve,pd.read_csv(previous/'daily_nav.csv'),check_dtype=False,rtol=1e-10,atol=1e-7)
                columns = ['date','symbol','side','status','quantity','fill_price_raw','commission','transfer_fee','stamp_tax','slippage_cost']
                pd.testing.assert_frame_equal(audit['fills'][columns],pd.read_csv(previous/'fills.csv')[columns],check_dtype=False,rtol=1e-10,atol=1e-7)
                golden.append(dict(slippage_bps=cost,passed=True))
                write_json(output/'baseline_golden.json',golden)
            row = dict(comparison.rows[-1], **diagnostics(audit,curve))
            rows.append(row)
            pd.DataFrame(rows).to_csv(output/'diagnostics_partial.csv',index=False)
            write_json(output/'main'/f'{name}_{cost}bp/risk_config.json',asdict(comparison.risk))
            print(json.dumps(row,ensure_ascii=False),flush=True)
    pairs = []
    for cost in COSTS:
        for baseline,candidate in [(ARMS[0][0],a[0]) for a in ARMS[1:]]+[(ARMS[1][0],a[0]) for a in ARMS[2:]]:
            b = comparison.navs[('main',baseline,cost)];a = comparison.navs[('main',candidate,cost)]
            br = next(r for r in rows if r['strategy']==baseline and r['slippage_bps']==cost)
            ar = next(r for r in rows if r['strategy']==candidate and r['slippage_bps']==cost)
            pairs.append(dict(baseline=baseline,candidate=candidate,slippage_bps=cost,
                              drawdown_improvement_pp=ar['max_drawdown_pct']-br['max_drawdown_pct'],
                              **c.paired_block_interval(b.capital,a.capital)))
    report(output,rows,pairs,pd.concat(comparison.annual,ignore_index=True))
    verify_data()
    accounts_unchanged = all(digest(p)==h for p,h in registration['protected_account_hashes'].items())
    write_json(output/'completion.json',dict(status='COMPLETE',runs=len(rows),baseline_golden=golden,
        inputs_unchanged=True,protected_accounts_unchanged=accounts_unchanged,new_holdout=False,automatic_admission=False))
    print('COMPLETE',flush=True)


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent',type=Path,default=PARENT)
    parser.add_argument('--output',type=Path,default=OUTPUT)
    parser.add_argument('--registered',action='store_true')
    args = parser.parse_args()
    if args.registered: run(args.parent.resolve(),args.output.resolve())
    else: register(args.parent.resolve(),args.output.resolve())
