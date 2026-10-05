#!/usr/bin/env python3
"""Replay the committed candidate and diagnose cases without fitting a policy."""
from __future__ import annotations
import argparse
from dataclasses import asdict, is_dataclass
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
OUTPUT = Path('/Users/wjy/abu/backtests/alpha158_case_study_2025_2026_20261004')
START, END = 20250102, 20260930
FEATURES = ('daily_rank', 'return_20d', 'return_60d', 'atr_fraction',
            'amount_ratio_20d', 'high_252_nearness', 'stock_excess_industry_20d',
            'industry_breadth_ma60', 'ma60_bias', 'market_return_20d')
FEATURE_NAMES = dict(zip(FEATURES, ('模型排名', '20日涨幅', '60日涨幅', 'ATR/股价',
    '成交额/20日中位数', '距252日最高价比例', '20日行业超额', '行业站上60日线比例',
    '60日均线偏离', '沪深300近20日涨幅')))


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(8*1024*1024), b''): h.update(b)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n')


def freeze(output):
    output.mkdir(parents=True, exist_ok=False)
    archive = output/'source.tar'
    commit = subprocess.check_output(['git', 'rev-parse', '0b3c997^{commit}'], cwd=ROOT, text=True).strip()
    subprocess.run(['git', 'archive', '--format=tar', '-o', str(archive), commit], cwd=ROOT, check=True)
    runtime = output/'runtime'; runtime.mkdir()
    with tarfile.open(archive) as stream: stream.extractall(runtime, filter='data')
    archive.unlink()
    runner = runtime/'scripts'/Path(__file__).name
    shutil.copy2(__file__, runner)
    registration = json.loads((PARENT/'registration.json').read_text())
    registration['runtime_files'] = {str(p.relative_to(runtime)): sha(p) for p in runtime.rglob('*') if p.is_file()}
    write_json(output/'registration.json', registration)
    protected = [Path('/Users/wjy/abu/paper/vcp_residual_v2/state.json')]
    genesis = Path('/Users/wjy/abu/shadow/alpha158_forward_v1/genesis')
    protected += [genesis/'commit.json', genesis.parent/'summary.json']
    protected += [genesis/name for name in json.loads((genesis/'commit.json').read_text())['files']]
    write_json(output/'case_registration.json', dict(
        registered_at=datetime.now().astimezone().isoformat(), committed_source=commit,
        runner_sha256=sha(runner), start=START, end=END, fresh_cash=1000000,
        cases=[['alpha_no_rank_exit', x] for x in (25,40,60)]+[['alpha_v3',25]],
        features=FEATURES, threshold_rule='2025 closed-by-20251231 trade feature median; direction from 2025 high/low mean net return only; unchanged 2026 entry cohorts',
        case_rule='Top/bottom five closed trades by cash contribution and net return separately; all closed trades split by ex-post return quartiles; nonnegative bottom quartile is low-positive group',
        uncertainty='2026 entry-date cluster bootstrap, 3000 replicates, seed20261004; exploratory unadjusted intervals across ten features',
        right_censoring='Open trades excluded from closed-trade rankings and shown separately; fixed 20-session adjusted-close outcomes also recorded for all mature entries',
        entry_features='Signal-close information only; next-open gaps, holding time, MFE/MAE and post-exit returns explicitly outcome diagnostics',
        historical_reuse=True, new_holdout=False, strategy_optimization=False,
        auto_admission=False, protected_account_hashes={str(p):sha(p) for p in protected} ))
    os.execv(sys.executable, [sys.executable,'-B',str(runner),'--output',str(output),'--frozen'])


def records(value):
    import pandas as pd
    return value if isinstance(value,pd.DataFrame) else pd.DataFrame([asdict(x) if is_dataclass(x) else x for x in value])


def summarize(group):
    return dict(n=len(group), mean_return_pct=float(group.net_return_pct.mean()),
        median_return_pct=float(group.net_return_pct.median()), win_rate_pct=float(group.net_pnl_cash.gt(0).mean()*100),
        pnl_cash=float(group.net_pnl_cash.sum()), median_holding=float(group.holding_sessions.median()))


def risk_multiple(pnl, initial_r_cash):
    """A gap through the initial stop has undefined R, not infinite quality."""
    return float(pnl)/float(initial_r_cash) if float(initial_r_cash)>0 else float('nan')


def terminal_market_value(panel, day, column, quantity):
    import numpy as np
    from abupy.AlphaBu.ABuSecurityLifecycle import accounting_mark
    history=panel.exec_close[:day+1,column]
    valid=history[np.isfinite(history)&(history>0)]
    previous=float(valid[-1]) if len(valid) else float('nan')
    mark=accounting_mark(float(history[-1]),previous,bool(panel.terminated_mask[day,column]))
    return float(quantity)*mark


def cluster_interval(frame, column, threshold, direction, replicates=3000):
    import numpy as np
    groups = [g for _,g in frame.groupby('opened_at',sort=True)]
    if not groups: return (float('nan'),float('nan'))
    rows=[]
    for g in groups:
        mask=(g[column]>=threshold) if direction==1 else (g[column]<=threshold)
        rows.append([g.loc[mask,'net_return_pct'].sum(),mask.sum(),g.loc[~mask,'net_return_pct'].sum(),(~mask).sum()])
    values=np.asarray(rows,float); rng=np.random.default_rng(20261004)
    samples=values[rng.integers(0,len(values),(replicates,len(values)))].sum(axis=1)
    ok=(samples[:,1]>0)&(samples[:,3]>0)
    delta=samples[ok,0]/samples[ok,1]-samples[ok,2]/samples[ok,3]
    return tuple(np.quantile(delta,[.025,.975])) if len(delta) else (float('nan'),float('nan'))


def feature_check(trades):
    import numpy as np
    import pandas as pd
    closed=trades[trades.status.eq('CLOSED')]
    dev=closed[(closed.opened_at<20260101)&(closed.closed_at<=20251231)]
    later=closed[closed.opened_at>=20260101]
    rows=[]
    for column in FEATURES:
        d=dev.dropna(subset=[column]);v=later.dropna(subset=[column])
        threshold=float(d[column].median())
        high=d[column]>=threshold
        direction=1 if d.loc[high,'net_return_pct'].mean()>=d.loc[~high,'net_return_pct'].mean() else -1
        dm=(d[column]>=threshold) if direction==1 else (d[column]<=threshold)
        vm=(v[column]>=threshold) if direction==1 else (v[column]<=threshold)
        low,upper=cluster_interval(v,column,threshold,direction)
        row=dict(feature=column,threshold=threshold,direction='>=' if direction==1 else '<=',
            dev_selected_n=int(dm.sum()),dev_other_n=int((~dm).sum()),
            dev_delta_pp=float(d.loc[dm,'net_return_pct'].mean()-d.loc[~dm,'net_return_pct'].mean()),
            later_selected_n=int(vm.sum()),later_other_n=int((~vm).sum()),later_entry_clusters=int(v.opened_at.nunique()),
            later_selected_return_pct=float(v.loc[vm,'net_return_pct'].mean()),
            later_other_return_pct=float(v.loc[~vm,'net_return_pct'].mean()),
            later_delta_pp=float(v.loc[vm,'net_return_pct'].mean()-v.loc[~vm,'net_return_pct'].mean()),
            later_ci95_low_pp=float(low),later_ci95_high_pp=float(upper))
        # Outcomes of a common holding horizon reduce the closed-trades-only selection effect.
        mature=trades[(trades.opened_at>=20260101)&trades.forward_20d_pct.notna()].dropna(subset=[column])
        mm=(mature[column]>=threshold) if direction==1 else (mature[column]<=threshold)
        row.update(mature20_selected_n=int(mm.sum()),mature20_other_n=int((~mm).sum()),
            mature20_delta_pp=float(mature.loc[mm,'forward_20d_pct'].mean()-mature.loc[~mm,'forward_20d_pct'].mean()))
        row['descriptive_screen']=bool(min(row['dev_selected_n'],row['dev_other_n'],row['later_selected_n'],row['later_other_n'])>=15 and row['later_ci95_low_pp']>0 and row['mature20_delta_pp']>0)
        rows.append(row)
    return pd.DataFrame(rows)


def trade_cases(comparison, audit, output):
    import numpy as np
    import pandas as pd
    from abupy.AlphaBu.ABuAlpha158Lite import Alpha158LiteFeatureEngine, ALPHA158_LITE_FEATURES
    panel=comparison.panel; dates={int(x):i for i,x in enumerate(panel.dates)}; last=dates[END]
    fills=audit['fills']; filled=fills[fills.status.eq('filled')]
    buys=filled[filled.side.eq('buy')].set_index('intent_id')
    orders=records(audit['orders']).set_index('intent_id')
    lots=records(audit['position_lots']); dispositions=records(audit['lot_dispositions'])
    logical=records(audit['logical_trades']); positions=records(audit['risk_positions_daily'])
    events=records(audit['position_events'])
    funded=logical[logical.entry_intent_id.isin(buys.index)].copy()
    # Dividend ownership belongs to the record-date holder, even if paid after exit.
    dividend_by_trade={};dividend_rows=[]
    for day,actions in panel.corporate_actions.items():
        if day>last:continue
        day_positions=positions[(positions.date==int(panel.dates[day]))&~positions.pending.astype(bool)]
        for action in actions:
            payment=action['cash_day']
            if not action['cash_per_share'] or payment is None or not day<payment<=last:continue
            symbol=panel.symbols[action['symbol']]
            for row in day_positions[day_positions.symbol.eq(symbol)].itertuples():
                value=float(row.quantity*action['cash_per_share'])
                dividend_by_trade[row.trade_id]=dividend_by_trade.get(row.trade_id,0.)+value
                dividend_rows.append(dict(trade_id=row.trade_id,symbol=symbol,record_date=int(panel.dates[day]),payment_date=int(panel.dates[payment]),cash=value))
    paid=float(events.loc[events.event_type.eq('CASH_DIVIDEND'),'cash_delta'].sum()) if len(events) else 0.
    assert abs(sum(dividend_by_trade.values())-paid)<1e-5, 'dividend attribution mismatch'
    pd.DataFrame(dividend_rows).to_csv(output/'dividend_attribution.csv',index=False)
    rows=[];path_rows=[]
    names=pd.read_csv(comparison.RESEARCH/'security_master.csv') if hasattr(comparison,'RESEARCH') else pd.read_csv(PARENT.parent.parent/'shadow/alpha158_forward_v1/data/research/security_master.csv')
    names=names.drop_duplicates('symbol').set_index('symbol')['name'].to_dict()
    selected=records(audit['selection']).set_index(['signal_asof','symbol'])
    for trade in funded.itertuples():
        buy=buys.loc[trade.entry_intent_id]; order=orders.loc[trade.entry_intent_id]
        entry=int(buy.date);start=dates[entry];signal=int(order.created_asof);col=panel.symbol_index[trade.symbol]
        closed=trade.status=='CLOSED';exit_date=int(trade.closed_at) if closed else END
        stop=dates[exit_date];end=stop if closed else last+1
        ds=dispositions[dispositions.trade_id.eq(trade.trade_id)];ls=lots[lots.trade_id.eq(trade.trade_id)]
        book=float(buy.quantity*buy.fill_price_raw+buy.commission+buy.transfer_fee+buy.stamp_tax)
        realized=float(ds.realized_pnl_cash.sum());remaining=float(ls.remaining_book_cost_cash.sum())
        # The runner does not emit a risk-decision snapshot on its terminal day.
        # Mark final ledger quantities using the executor's accounting rule.
        market_value=terminal_market_value(panel,last,col,ls.quantity_remaining.sum())
        unrealized=market_value-remaining
        dividend=dividend_by_trade.get(trade.trade_id,0.)
        net=realized+unrealized+dividend
        adjusted_entry=float(buy.fill_price_raw/(panel.exec_open[start,col]/panel.open[start,col]))
        path=panel.close[start:end,col].astype(float)/adjusted_entry-1
        if not len(path):path=np.array([0.])
        for i,value in enumerate(path):path_rows.append(dict(trade_id=trade.trade_id,date=int(panel.dates[start+i]),held_session=i+1,adjusted_close_return_pct=float(value*100)))
        forward={}
        for horizon in (10,20,60):
            target=start+horizon-1
            forward[f'forward_{horizon}d_pct']=float((panel.close[target,col]/adjusted_entry-1)*100) if target<=last and np.isfinite(panel.close[target,col]) else np.nan
        post={}
        for horizon in (5,20):
            target=stop+horizon
            post[f'post_exit_{horizon}d_pct']=float((panel.close[target,col]/panel.open[stop,col]-1)*100) if closed and target<=last else np.nan
        selection=selected.loc[(signal,trade.symbol)]
        rows.append(dict(trade_id=trade.trade_id,symbol=trade.symbol,name=names.get(trade.symbol,''),status=trade.status,
            signal_asof=signal,opened_at=entry,closed_at=exit_date if closed else np.nan,
            entry_year=entry//10000,entry_book_cash=book,entry_weight_pct=book/float(order.portfolio_equity_asof)*100,
            realized_pnl_cash=realized,unrealized_pnl_cash=unrealized,terminal_market_value=market_value,paid_dividend_cash=dividend,net_pnl_cash=net,
            net_return_pct=net/book*100,contribution_pp=net/10000,initial_r_cash=float(trade.initial_r_cash_frozen),
            pnl_r=risk_multiple(net,trade.initial_r_cash_frozen),holding_sessions=end-start,
            exit_reason=str(ds.exit_reason.iloc[-1]) if len(ds) else 'OPEN',
            mfe_close_pct=float(np.nanmax(path)*100),mae_close_pct=float(np.nanmin(path)*100),
            entry_gap_pct=float((panel.exec_open[start,col]/panel.exec_close[dates[signal],col]-1)*100),
            daily_rank=int(selection.daily_rank),alpha_score=float(selection.score),
            **forward,**post))
    result=pd.DataFrame(rows)
    engine=Alpha158LiteFeatureEngine(panel,comparison.source)
    feature_rows=[]
    for signal,group in result.groupby('signal_asof',sort=True):
        day=dates[int(signal)];matrix=engine.raw_features(day)
        for row in group.itertuples():
            col=panel.symbol_index[row.symbol]
            values=dict(zip(ALPHA158_LITE_FEATURES,map(float,matrix[col])))
            feature_rows.append(dict(trade_id=row.trade_id,**values,
                market_return_20d=float(panel.benchmark_close[day]/panel.benchmark_close[day-20]-1),
                industry_at_entry=int(panel.industry[day,col])))
        print(f'FEATURES {signal}',flush=True)
    result=result.merge(pd.DataFrame(feature_rows),on='trade_id',validate='one_to_one')
    expected=float(audit['curve'].capital.iloc[-1]-1e6)
    assert abs(result.terminal_market_value.sum()-audit['curve'].stocks.iloc[-1])<1e-5, 'terminal holdings mismatch'
    error=float(result.net_pnl_cash.sum()-expected)
    assert abs(error)<1e-5, f'trade-account reconciliation failed: {error}'
    assert not result[list(FEATURES)].isna().any().any(), 'missing registered entry features'
    result.to_csv(output/'trade_cases.csv',index=False)
    pd.DataFrame(path_rows).to_csv(output/'holding_paths.csv',index=False)
    reconciliation=dict(account_pnl=expected,closed_net_pnl=float(result.loc[result.status.eq('CLOSED'),'net_pnl_cash'].sum()),
        open_trade_total_pnl=float(result.loc[~result.status.eq('CLOSED'),'net_pnl_cash'].sum()),
        realized_disposition_pnl=float(result.realized_pnl_cash.sum()),unrealized_pnl=float(result.unrealized_pnl_cash.sum()),
        cash_dividends=paid,residual=error,filled_entries=len(result),unfunded_logical_records=len(logical)-len(result))
    write_json(output/'pnl_reconciliation.json',reconciliation)
    return result


def write_report(output,trades,checks,comparison,benchmark):
    import numpy as np
    import pandas as pd
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    closed=trades[trades.status.eq('CLOSED')].copy();opened=trades[~trades.status.eq('CLOSED')].copy()
    q25,q75=closed.net_return_pct.quantile([.25,.75])
    groups={'top_return_quartile':closed[closed.net_return_pct>=q75],
            'bottom_return_quartile':closed[closed.net_return_pct<=q25],
            'middle_half':closed[(closed.net_return_pct>q25)&(closed.net_return_pct<q75)],
            'all_losers':closed[closed.net_pnl_cash<0]}
    positive=closed[closed.net_pnl_cash>=0]
    groups['low_positive']=positive[positive.net_return_pct<=positive.net_return_pct.quantile(.25)]
    summaries=[]
    for label,group in groups.items():
        summaries.append(dict(group=label,**summarize(group),**{f:float(group[f].median()) for f in FEATURES},
            mfe_close_pct=float(group.mfe_close_pct.median()),mae_close_pct=float(group.mae_close_pct.median())))
    summaries=pd.DataFrame(summaries);summaries.to_csv(output/'outcome_group_features.csv',index=False)
    checks.to_csv(output/'feature_stability.csv',index=False)
    reasons=[]
    for reason,g in closed.groupby('exit_reason'):
        reasons.append(dict(reason=reason,**summarize(g),median_mfe_pct=float(g.mfe_close_pct.median()),
            post_exit20_n=int(g.post_exit_20d_pct.notna().sum()),mean_post_exit20_pct=float(g.post_exit_20d_pct.mean())))
    pd.DataFrame(reasons).to_csv(output/'exit_reason_summary.csv',index=False)
    top=closed.nlargest(5,'net_pnl_cash');bottom=closed.nsmallest(5,'net_pnl_cash')
    wins=closed[closed.net_pnl_cash>0];losses=closed[closed.net_pnl_cash<0]
    stats=dict(closed_trades=len(closed),open_trades=len(opened),win_rate_pct=float(len(wins)/len(closed)*100),
        mean_win_pct=float(wins.net_return_pct.mean()),mean_loss_pct=float(losses.net_return_pct.mean()),
        profit_factor=float(wins.net_pnl_cash.sum()/abs(losses.net_pnl_cash.sum())),
        top5_share_gross_wins_pct=float(top.net_pnl_cash.sum()/wins.net_pnl_cash.sum()*100),
        top5_cash=float(top.net_pnl_cash.sum()),closed_cash_excluding_top5=float(closed.net_pnl_cash.sum()-top.net_pnl_cash.sum()),
        median_closed_holding_sessions=float(closed.holding_sessions.median()),
        highest_return_quartile_cutoff=float(q75),lowest_return_quartile_cutoff=float(q25))
    write_json(output/'case_summary.json',stats)
    def table(frame,cols,labels=None):
        lines=['| '+' | '.join(labels or cols)+' |','| '+' | '.join(['---']*len(cols))+' |']
        for row in frame.to_dict('records'):
            values=[]
            for k in cols:
                v=row[k]
                if isinstance(v,(float,np.floating)):
                    v='—' if not np.isfinite(v) else (str(int(v)) if k in ('opened_at','closed_at') else f'{v:.2f}')
                values.append(str(v))
            lines.append('| '+' | '.join(values)+' |')
        return '\n'.join(lines)
    frame=pd.DataFrame(comparison.rows)
    text=['# 2025—2026当前候选回测与案例诊断','',
        '使用已提交版本0b3c997，2025-01-02从100万元现金开始，到2026-09-30；2026并非全年。保持取消排名退出、原风险预算、不同步动态止损、不加仓和不分批止盈。所有成交使用次日开盘与原执行约束，单边25bp为主情景，40/60bp为压力情景，另跑原v3对照。', '',
        '本轮仍是已观察历史的诊断，2026只作为时间后段一致性检查，不能称为新的未见样本外验证。没有根据案例改变任何策略规则。', '',
        table(frame,['strategy','slippage_bps','return_pct','cagr_pct','max_drawdown_pct','average_exposure_pct','filled_buys'],['策略','滑点bp','累计收益%','年化收益%','最大回撤%','平均仓位%','买入成交']), '',
        f"沪深300价格指数同期毛收益 {benchmark['return_pct']:.2f}%，最大回撤 {benchmark['max_drawdown_pct']:.2f}%；不含股息和费用，风险暴露与低仓位策略不同。", '',
        table(pd.concat(comparison.annual,ignore_index=True).query("strategy == 'alpha_no_rank_exit' and slippage_bps == 25"),['year','return_pct'],['年份','连续资金年度收益%']), '',
        '## 逐笔盈亏与集中度','',
        f"已平仓{len(closed)}笔，期末仍持有{len(opened)}笔。胜率{stats['win_rate_pct']:.2f}%，盈利交易平均收益{stats['mean_win_pct']:.2f}%，亏损交易平均收益{stats['mean_loss_pct']:.2f}%，现金盈亏比（盈利总额/亏损总额）{stats['profit_factor']:.2f}。", '',
        f"前五大盈利贡献占所有盈利交易毛盈利的{stats['top5_share_gross_wins_pct']:.2f}%；去掉这五笔的静态会计剩余已平仓盈亏为{stats['closed_cash_excluding_top5']:.2f}元。该剔除不是重新回测，不能解释为删去交易后的可实现收益。", '',
        '逐笔净收益包含买卖手续费、已成交价格中的滑点、归属于该交易的已支付分红；期末未平仓盈亏按账户记账市值计算。账户总盈亏与全部交易归因核对见pnl_reconciliation.json。取消/未成交的逻辑记录不算交易。', '',
        '## 高收益与低收益案例','',
        '下表先按实际现金贡献选择各五笔；trade_cases.csv提供全部交易。小仓位的高收益率与大额账户贡献不是同一件事。', '']
    casecols=['symbol','name','opened_at','closed_at','net_return_pct','net_pnl_cash','entry_weight_pct','holding_sessions','exit_reason','mfe_close_pct']
    caselabels=['代码','名称（显示标签）','买入日','卖出日','净收益%','贡献元','初始仓位%','持仓日','退出原因','持仓最高收盘浮盈%']
    for title,group in [('最大盈利贡献',top),('最大亏损贡献',bottom),('最高单笔收益率',closed.nlargest(5,'net_return_pct')),('最低单笔收益率',closed.nsmallest(5,'net_return_pct')),('低正收益案例',groups['low_positive'].nsmallest(5,'net_return_pct'))]:
        text += [f'### {title}','',table(group,casecols,caselabels),'']
    text += ['## 赢家与输家的入场特征','',
        '高低组按已平仓净收益率的上、下四分位事后划分，分组本身不可用于实盘选股。特征为信号日原始值（涨幅/ATR等为比例，非百分位排名），全部使用信号收盘及更早数据；这些描述不是因果关系。', '',
        table(summaries,['group','n','median_return_pct','median_holding',*FEATURES],['结果组','笔数','中位收益%','中位持仓日',*[FEATURE_NAMES[f] for f in FEATURES]]), '',
        '## 2025发现、2026检查','',
        '只用2025年内已经平仓的2025入场交易计算特征中位数及较优方向，原样应用于2026入场且已平仓交易。方向按2025高低组平均净收益确定；2025跨年未平仓交易不参与发现，避免使用2026结果。每个特征均披露，无优化阈值搜索。', '',
        '区间按2026入场日聚类、3000次固定种子重采样，未对10次比较作校正；即使方向保持也只能形成假设。至少两年每个分组各15笔、后段区间下界为正且固定20日诊断同向才通过描述性筛查。固定20日结果是复权价格变动，未按真实退出和全部费用结算，不等于策略净收益。', '',
        table(checks,['feature','threshold','direction','dev_selected_n','dev_other_n','dev_delta_pp','later_selected_n','later_other_n','later_delta_pp','later_ci95_low_pp','later_ci95_high_pp','mature20_delta_pp','descriptive_screen']), '',
        '## 退出类型','',table(pd.DataFrame(reasons),['reason','n','mean_return_pct','median_return_pct','pnl_cash','median_holding','median_mfe_pct','post_exit20_n','mean_post_exit20_pct']), '',
        '持仓时间、最高/最低浮盈和退出后涨跌均是事后路径诊断，不能回填为入场特征；退出后20日观察有尾部截断，不能直接证明延迟卖出更好。', '',
        '## 期末未平仓（单独估值，不强制卖出）','',
        table(opened,['symbol','name','opened_at','entry_book_cash','net_return_pct','net_pnl_cash','holding_sessions']), '',
        '## 数据和结论边界','',
        '- 使用原模型股票池与冻结行情。当前证券名称仅用于展示，不是历史筛选条件。行业特征使用信号日行业记录。',
        '- 2025现金起步会改变评审节奏、持仓和资金路径，不能与从2023持仓延续过来的年度切片混为一谈。',
        '- 只研究实际成交有选择偏差；这些结果不能推广到被拒绝的候选或整个股票池。未平仓右截断通过独立列表及成熟20日结果提示，不能完全消除。',
        '- 日线价格、复权、历史ST/退市和公司行为存在原研究数据局限；止损不能保证跳空或跌停时成交。',
        '- 保留全部10个预登记特征与统计；不根据最大赢家事后添加过滤器，不改变影子账户。', '',
        '证据：case_registration.json、runtime/、case2025/逐日净值和成交、trade_cases.csv、feature_stability.csv、holding_paths.csv、pnl_reconciliation.json、completion.json。']
    (output/'REPORT.md').write_text('\n'.join(text)+'\n')
    plt.style.use('default')
    fig,axes=plt.subplots(2,2,figsize=(14,9),layout='constrained')
    for name,cost in [('alpha_no_rank_exit',25),('alpha_v3',25),('alpha_no_rank_exit',60)]:
        curve=comparison.navs[('case2025',name,cost)];x=pd.to_datetime(curve.date.astype(str));cap=curve.capital/1e6
        axes[0,0].plot(x,cap,label=f'{name} {cost}bp')
        axes[0,1].plot(x,(cap/cap.cummax()-1)*100,label=f'{name} {cost}bp')
    axes[0,0].legend(fontsize=8);axes[0,0].set_title('Continuous NAV: fresh cash start in 2025')
    axes[0,1].set_title('Drawdown (%)')
    for ax in axes[0]:ax.xaxis.set_major_locator(mdates.MonthLocator(interval=4));ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
    axes[1,0].hist(closed.net_return_pct,bins=20,color='#267899',alpha=.85);axes[1,0].set(xlabel='Closed trade net return (%)',ylabel='Trades',title='All closed trades, including paid dividends')
    for year,color in ((2025,'#4775bd'),(2026,'#c94747')):
        subset=closed[closed.entry_year.eq(year)]
        axes[1,1].scatter(subset.mfe_close_pct,subset.net_return_pct,color=color,alpha=.75,label=f'Entry year {year}')
    axes[1,1].legend(fontsize=8)
    axes[1,1].axhline(0,color='gray',lw=.8);axes[1,1].set(xlabel='Maximum close-to-entry gain while held (%)',ylabel='Realized net return (%)',title='Ex-post path diagnostic; not an entry signal')
    for ax in axes.flat:ax.grid(alpha=.2)
    fig.savefig(output/'case_overview.png',dpi=160);plt.close(fig)
    paths=pd.read_csv(output/'holding_paths.csv')
    fig,axes=plt.subplots(2,3,figsize=(14,8),layout='constrained')
    examples=pd.concat([top.head(3),bottom.head(3)])
    for ax,case in zip(axes.flat,examples.itertuples()):
        path=paths[paths.trade_id.eq(case.trade_id)]
        ax.plot(path.held_session,path.adjusted_close_return_pct,label='Adjusted close path')
        ax.scatter([case.holding_sessions+1],[case.net_return_pct],color='#b82a30',zorder=3,label='Net realized incl. dividend')
        ax.axhline(0,color='gray',lw=.8);ax.grid(alpha=.2)
        ax.set(title=f'{case.symbol}: net {case.net_return_pct:+.1f}% / CNY {case.net_pnl_cash:+,.0f}',xlabel='Trading sessions since entry',ylabel='Return (%)')
        ax.text(.02,.97,f'{int(case.opened_at)} to {int(case.closed_at)}',transform=ax.transAxes,va='top',fontsize=8)
    axes[0,0].legend(fontsize=8,loc='lower right')
    fig.savefig(output/'case_paths.png',dpi=160);plt.close(fig)


def run(output, analyze_only=False):
    sys.path.insert(0,str(output/'runtime'))
    import numpy as np
    import pandas as pd
    from scripts import compare_current_strategies_v1 as c
    from scripts.backtest_alpha158_lite_low_turnover_v3 import run_low_turnover
    registration=json.loads((output/'registration.json').read_text())
    experiment=json.loads((output/'case_registration.json').read_text())
    def verify():
        changed=[p for p,h in registration['data_files'].items() if sha(p)!=h]
        if changed:raise ValueError('input data changed: '+str(changed[:5]))
    verify();comparison=c.Comparison(output)
    # Capture the executor's already recorded corporate-action events; no behavior override.
    base_executor=run_low_turnover.__globals__['PortfolioExecutor'];captured=[]
    class ObservedExecutor(base_executor):
        def __init__(self,*args,**kwargs):
            super().__init__(*args,**kwargs);captured.append(self)
    run_low_turnover.__globals__['PortfolioExecutor']=ObservedExecutor
    primary=None
    if analyze_only:
        comparison.rows=pd.read_csv(output/'results.csv').to_dict('records')
        comparison.annual=[pd.read_csv(output/'annual_returns.csv')]
        for name,cost in experiment['cases']:
            directory=output/'case2025'/f'{name}_{cost}bp'
            curve=pd.read_csv(directory/'daily_nav.csv')
            comparison.navs[('case2025',name,cost)]=curve
            if name=='alpha_no_rank_exit' and cost==25:
                primary={}
                for key in ('fills','orders','position_lots','lot_dispositions','logical_trades','risk_positions_daily','position_events','selection'):
                    try:primary[key]=pd.read_csv(directory/(key+'.csv'))
                    except pd.errors.EmptyDataError:primary[key]=pd.DataFrame()
                primary['curve']=curve
    else:
        for name,cost in experiment['cases']:
            print(f'RUN {name} {cost}bp',flush=True)
            audit=comparison.alpha(name,cost,begin=START,end=END)
            audit['position_events']=list(captured[-1].position_events)
            comparison.save(name,cost,audit,scope='case2025',begin=START,end=END)
            if name=='alpha_no_rank_exit' and cost==25:primary=audit
            captured.clear()
    run_low_turnover.__globals__['PortfolioExecutor']=base_executor
    # Independent earlier replay is a financial golden, not the source of this run.
    reference=Path('/Users/wjy/abu/backtests/alpha158_event_exit_only_2025_2026_frozen_20261004/backtest')
    if reference.exists():
        cols=['date','cash','stocks','capital','exposure']
        pd.testing.assert_frame_equal(primary['curve'][cols],pd.read_csv(reference/'daily_nav.csv')[cols],check_dtype=False,rtol=1e-10,atol=1e-7)
        cols=['date','symbol','side','status','quantity','fill_price_raw','commission','transfer_fee','stamp_tax','slippage_cost']
        pd.testing.assert_frame_equal(primary['fills'][cols],pd.read_csv(reference/'fills.csv')[cols],check_dtype=False,rtol=1e-10,atol=1e-7)
        write_json(output/'baseline_golden.json',dict(passed=True,reference=str(reference)))
    trades=trade_cases(comparison,primary,output)
    checks=feature_check(trades)
    mask=(comparison.panel.dates>=START)&(comparison.panel.dates<=END)
    values=comparison.panel.benchmark_close[mask];nav=values/values[0]*1e6
    benchmark=dict(return_pct=float((nav[-1]/1e6-1)*100),max_drawdown_pct=float((nav/np.maximum.accumulate(nav)-1).min()*100))
    pd.DataFrame({'date':comparison.panel.dates[mask],'capital':nav}).to_csv(output/'benchmark.csv',index=False)
    write_report(output,trades,checks,comparison,benchmark)
    verify()
    protected=all(sha(p)==h for p,h in experiment['protected_account_hashes'].items())
    write_json(output/'completion.json',dict(status='COMPLETE',strategy_runs=len(comparison.rows),funded_trades=len(trades),
        inputs_unchanged=True,protected_accounts_unchanged=protected,new_holdout=False,policy_changed=False,
        registered_runner_unchanged=sha(output/'runtime/scripts'/Path(__file__).name)==experiment['runner_sha256'],
        analysis_runner_sha256=sha(__file__)))
    print('COMPLETE',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,default=OUTPUT);p.add_argument('--frozen',action='store_true');p.add_argument('--analyze-only',action='store_true');args=p.parse_args()
    if args.analyze_only:run(args.output.resolve(),analyze_only=True)
    elif args.frozen:run(args.output.resolve())
    else:freeze(args.output.resolve())
