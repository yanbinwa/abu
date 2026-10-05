#!/usr/bin/env python3
"""Exact accounting attribution of two frozen paths, not a counterfactual test."""
from __future__ import annotations
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import sys

PARENT = Path('/Users/wjy/abu/backtests/alpha158_industry_gate_2024_2025_20261004')
OUTPUT = PARENT/'attribution'
END = 20251231
ARMS = ('alpha_no_rank_exit', 'alpha_industry_gate')


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,default=str)+'\n')


def run():
    OUTPUT.mkdir(exist_ok=False)
    shutil.copy2(__file__,OUTPUT/'analysis_source.py')
    files = [p for arm in ARMS for p in (PARENT/'continuous'/(arm+'_25bp')).glob('*.csv')]
    protected = json.loads((PARENT/'experiment.json').read_text())['protected_account_hashes']
    sources = {str(p):sha(p) for p in files}
    write_json(OUTPUT/'registration.json',dict(source_hashes=sources,
        analysis_sha256=sha(__file__),rule='Match on symbol and signal date; include all funded trades, open marks and paid dividends; reconcile calendar endpoints; post-hoc path attribution only.',
        protected_account_hashes=protected))
    sys.path.insert(0,str(PARENT/'runtime'))
    import numpy as np
    import pandas as pd
    from scripts.compare_current_strategies_v1 import Comparison, RESEARCH
    from abupy.AlphaBu.ABuAlpha158Lite import Alpha158LiteFeatureEngine, ALPHA158_LITE_FEATURES
    from abupy.AlphaBu.ABuSecurityLifecycle import accounting_mark
    comparison = Comparison(PARENT)
    panel = comparison.panel
    dates = {int(date):i for i,date in enumerate(panel.dates)}
    engine = Alpha158LiteFeatureEngine(panel,comparison.source)
    feature_cache = {}
    names = pd.read_csv(RESEARCH/'security_master.csv').drop_duplicates('symbol').set_index('symbol')['name'].to_dict()
    tables, reconciliations, costs, exit_rows = {}, [], [], []
    for arm in ARMS:
        print('ATTRIBUTION',arm,flush=True)
        directory = PARENT/'continuous'/(arm+'_25bp')
        fills = pd.read_csv(directory/'fills.csv')
        filled = fills[fills.status.eq('filled')]
        buys = filled[filled.side.eq('buy')].set_index('intent_id')
        logical = pd.read_csv(directory/'logical_trades.csv')
        logical = logical[logical.entry_intent_id.isin(buys.index)]
        orders = pd.read_csv(directory/'orders.csv').set_index('intent_id')
        dispositions = pd.read_csv(directory/'lot_dispositions.csv')
        lots = pd.read_csv(directory/'position_lots.csv')
        positions = pd.read_csv(directory/'risk_positions_daily.csv')
        positions = positions[positions.pending.eq(False)]
        curve = pd.read_csv(directory/'daily_nav.csv').set_index('date')
        by_date = {date:group for date,group in positions.groupby('date')}
        dividends = []
        for day,actions in panel.corporate_actions.items():
            date = int(panel.dates[day])
            if date>END or date not in by_date:
                continue
            for action in actions:
                payment = action['cash_day']
                if not action['cash_per_share'] or payment is None or not day<payment<=dates[END]:
                    continue
                symbol = panel.symbols[action['symbol']]
                for row in by_date[date].loc[lambda x:x.symbol.eq(symbol)].itertuples():
                    dividends.append(dict(trade_id=row.trade_id,record_date=date,
                        payment_date=int(panel.dates[payment]),cash=float(row.quantity*action['cash_per_share'])))
        dividends = pd.DataFrame(dividends,columns=['trade_id','record_date','payment_date','cash'])
        dividends.to_csv(OUTPUT/(arm+'_dividends.csv'),index=False)
        rows=[]
        for trade in logical.itertuples():
            buy = buys.loc[trade.entry_intent_id]
            order = orders.loc[trade.entry_intent_id]
            book = float(buy.quantity*buy.fill_price_raw+buy.commission+buy.transfer_fee+buy.stamp_tax)
            ds = dispositions[dispositions.trade_id.eq(trade.trade_id)]
            ls = lots[lots.trade_id.eq(trade.trade_id)]
            div = dividends[dividends.trade_id.eq(trade.trade_id)]
            signal = int(order.created_asof)
            if signal not in feature_cache:
                feature_cache[signal] = engine.raw_features(dates[signal])
            col = panel.symbol_index[trade.symbol]
            feature = dict(zip(ALPHA158_LITE_FEATURES,map(float,feature_cache[signal][col])))
            row = dict(strategy=arm,trade_id=trade.trade_id,symbol=trade.symbol,name=names.get(trade.symbol,''),
                signal_asof=signal,opened_at=int(trade.opened_at),closed_at=trade.closed_at,status=trade.status,
                quantity=int(buy.quantity),entry_book_cash=book,entry_weight_pct=book/float(order.portfolio_equity_asof)*100,
                stock_return20=feature['return_20d'],industry_return20=feature['industry_return_20d'],
                excess20=feature['stock_excess_industry_20d'],passes_gate=feature['stock_excess_industry_20d']>=-.05,
                exit_reason=ds.exit_reason.iloc[-1] if len(ds) else 'OPEN',
                realized_pnl=float(ds.realized_pnl_cash.sum()),dividend=float(div.cash.sum()))
            for cutoff,label in [(20241231,'2024'),(END,'total')]:
                if trade.opened_at>cutoff:
                    row['pnl_'+label]=0.; row['market_value_'+label]=0.;continue
                sold = ds[ds.fill_date<=cutoff]
                remaining = book-float(sold.disposed_book_cost_cash.sum())
                if cutoff==END:
                    quantity = float(ls.quantity_remaining.sum())
                    history = panel.exec_close[:dates[cutoff]+1,col]
                    valid = history[np.isfinite(history)&(history>0)]
                    mark = accounting_mark(float(history[-1]),float(valid[-1]) if len(valid) else float('nan'),bool(panel.terminated_mask[dates[cutoff],col]))
                    market_value = quantity*mark
                    assert abs(remaining-ls.remaining_book_cost_cash.sum())<1e-5
                else:
                    day_positions=by_date.get(cutoff,pd.DataFrame(columns=positions.columns))
                    market_value=float(day_positions.loc[day_positions.trade_id.eq(trade.trade_id),'market_value'].sum())
                row['market_value_'+label]=market_value
                row['pnl_'+label]=float(sold.realized_pnl_cash.sum()+market_value-remaining+div.loc[div.payment_date<=cutoff,'cash'].sum())
            row['pnl_2025']=row['pnl_total']-row['pnl_2024']
            row['net_return_pct']=row['pnl_total']/book*100
            rows.append(row)
        frame=pd.DataFrame(rows)
        assert not frame.duplicated(['symbol','signal_asof']).any()
        for cutoff,label in [(20241231,'2024'),(END,'total')]:
            expected=float(curve.loc[cutoff,'capital']-1e6)
            error=float(frame['pnl_'+label].sum()-expected)
            mark_error=float(frame['market_value_'+label].sum()-curve.loc[cutoff,'stocks'])
            assert abs(error)<1e-5,(arm,label,error)
            assert abs(mark_error)<1e-5,(arm,label,mark_error)
            reconciliations.append(dict(strategy=arm,period=label,account_pnl=expected,attributed_pnl=float(frame['pnl_'+label].sum()),residual=error,mark_residual=mark_error))
        frame.to_csv(OUTPUT/(arm+'_trades.csv'),index=False)
        tables[arm]=frame
        costs.append(dict(strategy=arm,**filled[['commission','transfer_fee','stamp_tax','slippage_cost']].sum().to_dict()))
        for reason,group in frame.groupby('exit_reason'):
            exit_rows.append(dict(strategy=arm,reason=reason,n=len(group),pnl=float(group.pnl_total.sum()),
                mean_entry_cash=float(group.entry_book_cash.mean()),win_rate=float(group.pnl_total.gt(0).mean()),mean_return=float(group.net_return_pct.mean())))
    left,right = (tables[arm] for arm in ARMS)
    merged=left.merge(right,on=['symbol','signal_asof'],how='outer',suffixes=('_a','_b'),indicator=True,validate='one_to_one')
    merged['category']=merged['_merge'].astype(str).map({'both':'shared_entry','left_only':'only_current','right_only':'only_filtered'})
    for label in ('2024','2025','total'):
        merged['delta_'+label]=merged['pnl_'+label+'_b'].fillna(0)-merged['pnl_'+label+'_a'].fillna(0)
    merged['name']=merged.name_a.fillna(merged.name_b)
    merged.sort_values('delta_total',ascending=False).to_csv(OUTPUT/'matched_trade_attribution.csv',index=False)
    group_rows=[]
    for category,group in merged.groupby('category'):
        group_rows.append(dict(category=category,n=len(group),pnl_a=float(group.pnl_total_a.sum()),pnl_b=float(group.pnl_total_b.sum()),
            **{f'delta_{label}':float(group['delta_'+label].sum()) for label in ('2024','2025','total')}))
    pd.DataFrame(group_rows).to_csv(OUTPUT/'category_attribution.csv',index=False)
    symbol=merged.groupby(['symbol','name'])[['delta_2024','delta_2025','delta_total']].sum().sort_values('delta_total',ascending=False)
    symbol.to_csv(OUTPUT/'symbol_attribution.csv')
    pd.DataFrame(costs).to_csv(OUTPUT/'friction.csv',index=False)
    pd.DataFrame(exit_rows).to_csv(OUTPUT/'exit_attribution.csv',index=False)
    only=merged[merged.category.eq('only_current')]
    only_groups=[]
    for passed,group in only.groupby('passes_gate_a'):
        only_groups.append(dict(passes_gate=bool(passed),n=len(group),pnl_a=float(group.pnl_total_a.sum()),avoided_pnl=float(group.delta_total.sum())))
    both=merged[merged.category.eq('shared_entry')]
    unmatched_exits=~(both.closed_at_a.fillna(0).eq(both.closed_at_b.fillna(0)) & both.exit_reason_a.eq(both.exit_reason_b))
    summary=dict(reconciliation=reconciliations,category_attribution=group_rows,only_current_by_gate=only_groups,
        total_delta=float(merged.delta_total.sum()),shared_entry_different_exit_count=int(unmatched_exits.sum()),
        shared_entry_different_quantity_count=int(both.quantity_a.ne(both.quantity_b).sum()),
        top5_symbol_delta=float(symbol.delta_total.head(5).sum()),
        delta_excluding_top1_symbol=float(merged.delta_total.sum()-symbol.delta_total.iloc[0]),
        note='Removing a contribution is a static concentration diagnostic, not a new backtest; unmatched entries reflect gate and subsequent portfolio-path changes, not pure causal gate effects.')
    write_json(OUTPUT/'summary.json',summary)
    assert all(sha(p)==h for p,h in sources.items())
    assert all(sha(p)==h for p,h in protected.items())
    write_json(OUTPUT/'completion.json',dict(status='COMPLETE',source_audits_unchanged=True,protected_accounts_unchanged=True,accounting_reconciled=True))
    print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':
    run()
