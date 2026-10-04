#!/usr/bin/env python3
"""Report the frozen comparison without selecting or changing any strategy."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

LABELS={
 'alpha_no_rank_exit':'当前候选：取消排名退出',
 'alpha_v3':'Alpha v3 原低换手', 'alpha_sync':'Alpha v3 动态止损同步',
 'alpha_v1':'Alpha v1 每日排名', 'alpha_v2':'Alpha v2 换手约束',
 'alpha_baseline_score':'Alpha 固定因子评分基线',
 'alpha_cost_gate':'Alpha 成本门槛换仓', 'alpha_diversified20':'Alpha 20只分散持仓',
 'alpha_add_turtle_atr':'Alpha 动态止损＋ATR加仓',
 'alpha_scale_out_2r_50':'Alpha ATR加仓＋2R止盈50%',
 'alpha_add_protected_winner':'Alpha 保本后加仓',
 'alpha_protected_scale_out_2r_50':'Alpha 保本后加仓＋2R止盈50%（原组合）',
 'vcp_h_residual_stop_trailing_stagnation':'VCP 残差动量事件退出',
 'vcp_sync':'VCP 残差＋动态止损同步',
 'vcp_add_turtle_atr':'VCP 动态止损＋ATR加仓',
 'vcp_scale_out_2r_50':'VCP ATR加仓＋2R止盈50%',
 'vcp_add_protected_winner':'VCP 保本后加仓',
 'vcp_protected_scale_out_2r_50':'VCP 保本后加仓＋2R止盈50%（原组合）',
 'alpha_residual_add':'Alpha 剩余风险额度加仓',
 'alpha_sleeve':'Alpha 90%基础＋10%独立加仓账户',
 'alpha_sleeve_market_gate':'Alpha 独立加仓账户＋市场过滤',
 'combined_base_orders':'Alpha＋VCP 共享资金订单重放',
 'context_market_overlay':'VCP 市场状态过滤',
 'context_industry_rank':'VCP 行业排序',
 'context_industry_leader_rank':'VCP 行业龙头排序',
 'context_baseline_replay':'VCP 原始信号重放对照',
 'vcp_standardized':'VCP 标准化残差评分',
 'vcp_followthrough':'VCP 次日收盘确认',
 'CSI300_price_index_gross':'沪深300价格指数（毛收益）',
}
KEYS=('alpha_no_rank_exit','alpha_v3','alpha_sync','alpha_add_turtle_atr','alpha_scale_out_2r_50',
      'alpha_add_protected_winner','alpha_protected_scale_out_2r_50',
      'vcp_h_residual_stop_trailing_stagnation','vcp_sync','vcp_add_turtle_atr','vcp_scale_out_2r_50',
      'vcp_add_protected_winner','vcp_protected_scale_out_2r_50',
      'alpha_residual_add','alpha_v1','alpha_v2','combined_base_orders')


def table(frame,columns,labels=None):
    labels=labels or columns
    lines=['| '+' | '.join(labels)+' |','| '+' | '.join(['---']*len(columns))+' |']
    for row in frame.to_dict('records'):
        values=[]
        for name in columns:
            value=row[name]
            if name=='strategy':value=LABELS.get(value,value)
            elif isinstance(value,(float,np.floating)):value='—' if not np.isfinite(value) else f'{value:.2f}'
            values.append(str(value))
        lines.append('| '+' | '.join(values)+' |')
    return '\n'.join(lines)


def main():
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);args=p.parse_args();root=args.root
    completion=json.loads((root/'completion.json').read_text())
    if completion['status']!='COMPLETE':raise ValueError('comparison not complete')
    registration=json.loads((root/'registration.json').read_text())
    frame=pd.read_csv(root/'results.csv');primary=pd.read_csv(root/'primary_ranking.csv')
    years=pd.read_csv(root/'annual_returns.csv');uncertainty=pd.read_csv(root/'paired_uncertainty.csv')
    benchmark=pd.read_csv(root/'benchmark/CSI300_price_index_gross_0bp/daily_nav.csv')
    br=benchmark.capital.pct_change().fillna(0).to_numpy()
    def nav(name,cost=25,scope='main'):
        return pd.read_csv(root/scope/f'{name}_{cost:g}bp/daily_nav.csv')
    diagnostics=[]
    for row in primary.itertuples():
        curve=nav(row.strategy)
        returns=curve.capital.pct_change().fillna(0).to_numpy()
        weight=float(np.std(returns[1:],ddof=1)/np.std(br[1:],ddof=1))
        vol_index=np.cumprod(1+br*weight)
        exp_index=np.cumprod(1+br*curve.exposure.shift(1).fillna(0).to_numpy())
        diagnostics.append(dict(strategy=row.strategy,ex_post_vol_match_weight=weight,
            ending_exposure_pct=float(curve.exposure.iloc[-1]*100),
            vol_matched_index_return_pct=float((vol_index[-1]-1)*100),
            vol_matched_index_dd_pct=float((vol_index/np.maximum.accumulate(vol_index)-1).min()*100),
            lag_exposure_index_return_pct=float((exp_index[-1]-1)*100),
            excess_vs_lag_exposure_index_pp=float(row.return_pct-(exp_index[-1]-1)*100)))
    diagnostic=pd.DataFrame(diagnostics);diagnostic.to_csv(root/'benchmark_diagnostics.csv',index=False)
    primary=primary.merge(diagnostic,on='strategy',validate='one_to_one')
    selected=primary.set_index('strategy').reindex(KEYS).reset_index()
    delta=[]
    for cost in (25,40,60):
        lookup=frame[(frame.scope=='main')&(frame.slippage_bps==cost)].set_index('strategy')
        for baseline,candidate in (('alpha_v3','alpha_no_rank_exit'),('alpha_add_turtle_atr','alpha_scale_out_2r_50'),
                                   ('vcp_add_turtle_atr','vcp_scale_out_2r_50'),
                                   ('alpha_add_protected_winner','alpha_protected_scale_out_2r_50'),
                                   ('vcp_add_protected_winner','vcp_protected_scale_out_2r_50')):
            b,c=lookup.loc[baseline],lookup.loc[candidate]
            u=uncertainty[(uncertainty.baseline==baseline)&(uncertainty.candidate==candidate)&(uncertainty.slippage_bps==cost)].iloc[0]
            delta.append(dict(baseline=baseline,strategy=candidate,slippage_bps=cost,
                return_delta_pp=c.return_pct-b.return_pct,drawdown_improvement_pp=c.max_drawdown_pct-b.max_drawdown_pct,
                friction_saving_pp=b.total_friction_pct_initial-c.total_friction_pct_initial,
                ci95_low_pp=u.ci95_low_pp,ci95_high_pp=u.ci95_high_pp))
    delta=pd.DataFrame(delta);delta.to_csv(root/'key_pair_deltas.csv',index=False)
    # Descriptive fixed subperiods; never train/select or reset capital here.
    periods=[]
    for name in KEYS:
        curve=nav(name)
        for begin,end,label in ((20230727,20241231,'2023-2024'),(20250101,20260930,'2025-2026')):
            sub=curve[curve.date.between(begin,end)]
            previous=curve[curve.date<begin]
            initial=float(previous.capital.iloc[-1]) if len(previous) else 1e6
            values=np.r_[initial,sub.capital.to_numpy()]
            periods.append(dict(strategy=name,period=label,return_pct=(values[-1]/initial-1)*100,
                max_drawdown_pct=(values/np.maximum.accumulate(values)-1).min()*100))
    pd.DataFrame(periods).to_csv(root/'fixed_subperiods.csv',index=False)
    drawdowns=[];concentration=[]
    for name in KEYS:
        curve=nav(name);values=curve.capital.to_numpy()
        dd=values/np.maximum.accumulate(values)-1
        trough=int(np.argmin(dd));peak=int(np.argmax(values[:trough+1]))
        recovery=np.flatnonzero(values[trough+1:]>=values[peak])
        drawdowns.append(dict(strategy=name,peak_date=int(curve.date.iloc[peak]),
            trough_date=int(curve.date.iloc[trough]),max_drawdown_pct=float(dd[trough]*100),
            recovery_date=int(curve.date.iloc[trough+1+recovery[0]]) if len(recovery) else '期末未恢复'))
        directory=root/'main'/f'{name}_25bp'
        if not (directory/'lot_dispositions.csv').exists() or not (directory/'logical_trades.csv').exists():
            continue
        dispositions=pd.read_csv(directory/'lot_dispositions.csv')
        trades=pd.read_csv(directory/'logical_trades.csv')
        closed=trades[trades.status.eq('CLOSED')].trade_id
        pnl=dispositions[dispositions.trade_id.isin(closed)].groupby('trade_id').realized_pnl_cash.sum()
        winners=pnl[pnl>0].sort_values(ascending=False)
        concentration.append(dict(strategy=name,closed_trades=len(pnl),closed_trade_pnl=float(pnl.sum()),
            top5_winning_pnl=float(winners.head(5).sum()),
            top5_share_of_gross_wins_pct=float(winners.head(5).sum()/winners.sum()*100) if winners.sum()>0 else np.nan,
            net_closed_pnl_ex_top5=float(pnl.sum()-winners.head(5).sum())))
    drawdowns=pd.DataFrame(drawdowns);drawdowns.to_csv(root/'drawdown_episodes.csv',index=False)
    concentration=pd.DataFrame(concentration);concentration.to_csv(root/'closed_trade_concentration.csv',index=False)
    selected_years=years[(years.scope=='main')&(years.slippage_bps==25)&years.strategy.isin(KEYS)]
    year_col=next(c for c in selected_years if c not in ('year','strategy','scope','slippage_bps') and 'return' in c)
    annual=selected_years.pivot(index='strategy',columns='year',values=year_col).reindex(KEYS).reset_index()
    # Standalone figures, comparable paths and all primary arms.
    fig,axes=plt.subplots(2,2,figsize=(15,10),layout='constrained')
    named={'alpha_no_rank_exit':'Alpha event exits only','alpha_v3':'Alpha v3 original',
        'alpha_protected_scale_out_2r_50':'Alpha protected add + 2R 50%','vcp_h_residual_stop_trailing_stagnation':'VCP residual',
        'vcp_protected_scale_out_2r_50':'VCP protected add + 2R 50%'}
    date=pd.to_datetime(benchmark.date.astype(str))
    axes[0,0].plot(date,benchmark.capital/1e6,label='CSI300 price index (gross)',color='black',alpha=.7,ls='--')
    for name,label in named.items():
        curve=nav(name);x=pd.to_datetime(curve.date.astype(str));capital=curve.capital/1e6
        axes[0,0].plot(x,capital,label=label,lw=1.4)
        axes[0,1].plot(x,(capital/capital.cummax()-1)*100,label=label,lw=1.2)
    axes[0,0].set(title='Continuous NAV, CNY 1m each, 25bp per side',ylabel='NAV');axes[0,0].legend(fontsize=8)
    axes[0,1].set(title='Selected strategy drawdowns',ylabel='Drawdown (%)')
    for prefix,color,label in (('legacy_','#999999','Legacy PIT rules'),('vcp_','#3875b6','VCP'),('alpha_','#db8340','Alpha'),('context_','#58a070','VCP context'),('combined_','#9267a8','Shared account')):
        subset=primary[primary.strategy.str.startswith(prefix)]
        axes[1,0].scatter(-subset.max_drawdown_pct,subset.return_pct,s=27,color=color,label=label,alpha=.75)
    r=primary.set_index('strategy').loc['alpha_no_rank_exit']
    axes[1,0].scatter([-r.max_drawdown_pct],[r.return_pct],marker='*',s=160,color='#b82222',label='Current candidate')
    axes[1,0].annotate('Current candidate',(-r.max_drawdown_pct,r.return_pct),fontsize=8,xytext=(45,0),
        textcoords='offset points',va='center',arrowprops=dict(arrowstyle='-',lw=.6,color='#666666'))
    axes[1,0].set(title=f'All {len(primary)} primary variants: less drawdown is left',xlabel='Maximum drawdown magnitude (%)',ylabel='Total return (%)');axes[1,0].legend(fontsize=8)
    for name,label in named.items():
        subset=frame[(frame.scope=='main')&(frame.strategy==name)].sort_values('slippage_bps')
        axes[1,1].plot(subset.slippage_bps,subset.return_pct,marker='o',label=label)
    axes[1,1].set(title='Executable cost stress (orders may change)',xlabel='Slippage per side (bp)',ylabel='Total return (%)',xticks=[25,40,60])
    for ax in axes.flat:ax.grid(alpha=.2)
    fig.savefig(root/'strategy_comparison.png',dpi=170);fig.savefig(root/'strategy_comparison.pdf');plt.close(fig)
    columns=['strategy','return_pct','cagr_pct','max_drawdown_pct','calmar','average_exposure_pct']
    headers=['策略','累计收益%','年化收益%','最大回撤%','Calmar','平均仓位%']
    current=primary.set_index('strategy').loc['alpha_no_rank_exit'];base=primary.set_index('strategy').loc['alpha_v3']
    protected=primary.set_index('strategy').loc['alpha_add_protected_winner']
    protected_scale=primary.set_index('strategy').loc['alpha_protected_scale_out_2r_50']
    frontier=primary[primary.pareto_efficient]
    content=f'''# 当前策略统一回测比较（2026-10-04）

本轮实际运行 {completion['runs']} 个情景：{completion['primary_variants']} 个策略/规则组合的主回测、13 个关键版本各两档追加成本压力、三个较短机器学习样本的18个同期对照，以及沪深300价格指数。结果是重复使用既有历史的诊断，不能升级为新的样本外验证或自动准入。

初始清单冻结了59个组合。核对旧止盈实验的原始加仓触发码后，确认旧实验使用 ProtectedWinner（保本后加仓），而非 TurtleATR（ATR加仓）。因此另行登记并补齐8个原组合及其压力情景；初始ATR交叉组合仍完整保留，不冒充旧实验。原清单、补充清单、合并前结果全部保留，没有在看到收益后挑选新的阈值或修改规则。

## 当前候选与原版

取消排名退出候选累计收益 {current.return_pct:.4f}%，原版 {base.return_pct:.4f}%，增量 {current.return_pct-base.return_pct:.4f} 个百分点；最大回撤分别为 {current.max_drawdown_pct:.4f}% 和 {base.max_drawdown_pct:.4f}%。年化收益为 {current.cagr_pct:.4f}%。请把收益和回撤同时判断，累计收益不能当成年化收益。

此前的保本后加仓＋2R止盈50%组合也已复现：累计收益 {protected_scale.return_pct:.4f}%，最大回撤 {protected_scale.max_drawdown_pct:.4f}%；它所对应的保本后加仓统一退出基线为 {protected.return_pct:.4f}%、{protected.max_drawdown_pct:.4f}%。原组合与ATR交叉组合分别记录，不能互换。该原止盈组合在60bp成本下转为亏损，不能仅凭25bp结果判为稳健升级。

{table(selected,columns,headers)}

沪深300价格指数同期累计收益 {frame[frame.strategy=='CSI300_price_index_gross'].iloc[0].return_pct:.4f}%，最大回撤 {frame[frame.strategy=='CSI300_price_index_gross'].iloc[0].max_drawdown_pct:.4f}%。这是满仓价格指数毛收益，不含股息、交易费与基金跟踪误差，与平均仓位不同的策略不能直接视为等风险对照。

## 收益提升与成本压力

回撤改善值为“候选最大回撤－基线最大回撤”，正数表示改善。区间采用20交易日块、5000次、固定种子的配对重采样，未作多重比较校正，不是严格显著性检验。取消排名退出与Alpha原v3比较；分批止盈分别与同一种加仓政策下的统一退出版本比较。不同成本情景重新撮合并审批订单，成交路径会变化，收益不一定随滑点严格单调。

{table(delta,['strategy','slippage_bps','return_delta_pp','drawdown_improvement_pp','friction_saving_pp','ci95_low_pp','ci95_high_pp'],['候选','单边滑点bp','收益增量pp','回撤改善pp','成本节省pp','区间下界pp','区间上界pp'])}

三次连续跌停估值是对当日持仓打折的机械压力场景，未假定可以在跌停时顺利卖出，也不是三天路径仿真的概率预测：

{table(selected,['strategy','ending_exposure_pct','stress_return_pct','stress_drawdown_pct'],['策略','期末仓位%','三连跌停估值终值收益%','压力净值最大回撤%'])}

## 连续资金路径的年度贡献

2023和2026为不完整年度；年度切片不重置持仓或现金。

{table(annual,list(annual.columns),['策略']+[str(c)+'收益%' for c in annual.columns[1:]])}

## 最大回撤发生时段

{table(drawdowns,['strategy','peak_date','trough_date','max_drawdown_pct','recovery_date'],['策略','前峰值日','最大回撤谷底日','回撤%','恢复前峰值日'])}

## 已平仓交易的利润集中度

先把加仓与分批退出合并到同一逻辑交易，避免把一笔交易拆成多个胜样本。“扣除前五赢家的净额”仅为已实现交易损益诊断，不是删掉交易后重新回测，也不包含未平仓估值和全部公司行为现金。

{table(concentration,['strategy','closed_trades','closed_trade_pnl','top5_share_of_gross_wins_pct','net_closed_pnl_ex_top5'],['策略','已平仓逻辑交易','已平仓净损益元','前五占总盈利%','扣除前五赢家净额元'])}

## 仓位及指数诊断

同波动指数权重是事后由全样本波动估计，不能作为可实时实施的基线；前一日仓位匹配指数是无交易成本的诊断路径。两者均不能单独证明Alpha。独立加仓账户和共享订单重放另有资源分配语义，不能仅按终值断言选股更优。

{table(selected.merge(diagnostic[['strategy','vol_matched_index_return_pct','lag_exposure_index_return_pct']],on='strategy') if 'vol_matched_index_return_pct' not in selected else selected,['strategy','vol_matched_index_return_pct','lag_exposure_index_return_pct','excess_vs_lag_exposure_index_pp'],['策略','同波动指数收益%','滞后仓位指数收益%','相对仓位指数差pp'])}

## 全部主回测结果

按累计收益排列只是描述性展示，不是从{len(primary)}个变体中选择部署赢家。历史收益更高且回撤更小的非劣集合共有 {len(frontier)} 个版本；这仍不构成前瞻有效性证明。

{table(primary,columns+['pareto_efficient'],headers+['历史非劣集合'])}

## 较短机器学习预测区间

质量排序模型只在其原有OOS预测起止日期间比较。对照仅使用相同意图ID，避免把缺失预测期间当成完整部署。三个窗口分别报告，不能混入三年主表排名。所有模型继续使用当时已有的滚动OOS预测，没有把2026-10-04拟合的冻结前瞻模型拿去预测过去。

{table(frame[frame.scope.str.startswith('vcp_quality')],['scope','strategy','start','end','return_pct','max_drawdown_pct'],['预测窗口','策略','开始','结束','收益%','回撤%'])}

## 统一口径与限制

- 主区间2023-07-27至2026-09-30，各账户100万元，首日现金锚点，首笔成交最早下一交易日，全年资金连续；闲置现金不计利息。
- 单边滑点25bp；佣金、最低收费、过户费、卖出税及下单约束沿用相同执行器。策略自己的仓位、加仓和风险限制保留，不把不同策略改造成同一个策略。
- 原模型与意图沿用冻结股票池及历史ST口径，不因新收集的ST数据而扩大股票池。读取独立注册数据副本，输入哈希结束时重新核验。
- 当前前瞻模型仅用注册时已知历史拟合，不适用于回测过去。本轮候选历史表现对应既有滚动OOS模型上的退出规则消融，两者不能拼接。
- 基本面多因子v4、需要历史涨停池/龙虎榜等快照的短线情绪、缺分钟数据的M1/M2仍无有效可比样本；短线研究中已有历史资料的市场/行业上下文H1/H2/H3已通过context系列纳入。旧A1错误兼容口径与小股票池原型由PIT A2/B1/B2替代。未给缺少数据的版本编造收益。
- 日线撮合、历史证券状态、退市估值、分红税及公司行为时点仍有数据/模型限制；实际冲击、排队和分红纳税不完全可复现。压力估值是情景诊断，不能保证未来回撤上限。
- 原有研究性残余资金/独立加仓重放缺少同格式的完整费用与压力估值输出，表中相应指标留空；原始叠加账本已保存，未把缺失当成0成本。
- 检查了{len(primary)}个历史变体，许多共享同一信号，不能当成{len(primary)}个独立样本；存在多重比较和策略选择偏差。本轮不作为调参循环，不改变已经注册的双影子账户或现有模拟盘。

![统一回测图](strategy_comparison.png)

可复现证据：`registration.json`、`runtime/`、`inputs/`、各情景逐日净值/订单/成交/风险审计、`results.csv`、`annual_returns.csv`、`paired_uncertainty.csv`、`benchmark_diagnostics.csv`、`completion.json`。
'''
    (root/'REPORT.md').write_text(content)
    print(selected[columns].to_string(index=False));print('\n'+delta.to_string(index=False))


if __name__=='__main__':main()
