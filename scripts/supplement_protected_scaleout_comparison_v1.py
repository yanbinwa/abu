#!/usr/bin/env python3
"""Correct the comparison inventory: historical scale-outs used ProtectedWinner.

Preserve the original run, explicitly register all four existing exits, and
merge the supplementary results only after both batches have completed.
"""
from pathlib import Path
import argparse
import copy
from dataclasses import asdict
from datetime import datetime
import json
import shutil
import sys


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--merge',action='store_true');args=parser.parse_args();root=args.root.resolve()
    sys.path.insert(0,str(root/'runtime'))
    import pandas as pd
    from scripts import compare_current_strategies_v1 as c
    from abupy.AlphaBu.ABuArtifactManifest import sha256_file
    extra=root/'protected_scaleout_supplement'
    if args.merge:
        if not (extra/'completed.json').exists() or not (root/'completion.json').exists():
            raise ValueError('both batches must complete before merge')
        if (root/'completion_before_inventory_correction.json').exists():
            raise ValueError('already merged')
        original=json.loads((root/'completion.json').read_text())
        if original['status']!='COMPLETE':raise ValueError('original run invalid')
        for name in ('results.csv','annual_returns.csv','paired_uncertainty.csv','primary_ranking.csv','completion.json'):
            shutil.copy2(root/name,root/(Path(name).stem+'_before_inventory_correction'+Path(name).suffix))
        for path in (extra/'main').iterdir():
            shutil.copytree(path,root/'main'/path.name)
        for name in ('results.csv','annual_returns.csv','paired_uncertainty.csv'):
            merged=pd.concat([pd.read_csv(root/name),pd.read_csv(extra/name)],ignore_index=True)
            merged.to_csv(root/name,index=False)
        rows=pd.read_csv(root/'results.csv')
        if rows.duplicated(['scope','strategy','slippage_bps']).any():raise ValueError('duplicated runs')
        primary=rows.query("scope == 'main' and slippage_bps == 25").copy()
        primary['pareto_efficient']=[not ((primary.return_pct>=r.return_pct)&(primary.max_drawdown_pct>=r.max_drawdown_pct)&
            ((primary.return_pct>r.return_pct)|(primary.max_drawdown_pct>r.max_drawdown_pct))).any() for r in primary.itertuples()]
        primary.sort_values('return_pct',ascending=False).to_csv(root/'primary_ranking.csv',index=False)
        original.update(runs=len(rows),primary_variants=len(primary),inventory_correction='Original scale-outs use ProtectedWinner; ATR cross-combinations retained separately.',
            supplemental_runs=16,supplemental_data_unchanged=True)
        c.write_json(root/'completion.json',original)
        print(json.dumps(original,indent=2));return
    extra.mkdir(exist_ok=False)
    shutil.copy2(root/'registration.json',extra/'registration.json')
    shutil.copy2(Path(__file__),extra/'registered_runner.py')
    cases=[f'{family}_protected_{scale}' for family in ('alpha','vcp') for scale in c.SCALES]
    c.write_json(extra/'supplement_registration.json',dict(created_at=datetime.now().astimezone().isoformat(),
        reason='Old scale-out proposal trigger STOP_LEVEL_AT_BREAKEVEN identifies ProtectedWinner, not TurtleATR.',
        primary_cases=cases,stress_cases=['alpha_add_protected_winner','vcp_add_protected_winner',
            'alpha_protected_scale_out_2r_50','vcp_protected_scale_out_2r_50'],
        costs=[25,40,60],parameter_search=False,post_hoc_inventory_correction=True,
        runner_sha256=sha256_file(extra/'registered_runner.py')))
    comparison=c.Comparison(extra)
    # These are unfiltered source signals, archived by the original same-panel
    # VCP run. Reuse them only after a full financial golden check below.
    intent_path=root/'main/vcp_add_protected_winner_25bp/intents.csv'
    intents=c.load_frozen_intents(pd.read_csv(intent_path,dtype={'symbol':str}))
    grouped={}
    for intent in intents:grouped.setdefault(intent.signal_asof,[]).append(intent)
    def frozen_intents(strategy,day,variant='core',allow_terminal=False):
        if strategy.panel is not comparison.panel or variant!='residual_core':
            raise ValueError('frozen signal replay outside registered scope')
        return copy.deepcopy(grouped.get(int(strategy.panel.dates[day]),[]))
    c.VCPStrategy.generate_intents=frozen_intents
    def vcp(cost,scale=None):
        audit={}
        _,curve,fills,_,_=c.run_vcp_backtest(comparison.panel,None,'h_residual_stop_trailing_stagnation',cost,
            c.load_vcp_core_config(comparison.c/'vcp_core_v1.json'),c.load_vcp_attention_config(comparison.c/'vcp_attention_v1.json'),
            comparison.risk,c.load_vcp_residual_config(comparison.c/'vcp_residual_v2.json'),
            start_date=int(comparison.dates[1]),end_date=c.END,audit=audit,sync_dynamic_stops=True,
            position_add_policy=c._policy('protected_winner'),
            scale_out_config=comparison.scale(scale) if scale else None)
        audit.update(curve=curve,fills=fills);return audit
    control=vcp(25)
    pd.testing.assert_frame_equal(c.normalize_curve(control['curve'],comparison.dates),
        pd.read_csv(root/'main/vcp_add_protected_winner_25bp/daily_nav.csv'),check_dtype=False,rtol=1e-10,atol=1e-7)
    business=['date','symbol','side','status','quantity','fill_price_raw','commission','transfer_fee','stamp_tax','slippage_cost']
    pd.testing.assert_frame_equal(control['fills'][business],pd.read_csv(root/'main/vcp_add_protected_winner_25bp/fills.csv')[business],check_dtype=False,rtol=1e-10,atol=1e-7)
    c.write_json(extra/'signal_replay_golden.json',dict(passed=True,source_sha256=sha256_file(intent_path)))
    for cost in (25,40,60):
        for family in ('alpha','vcp'):
            if cost!=25:
                audit=comparison.alpha('alpha_add_protected_winner',cost) if family=='alpha' else vcp(cost)
                comparison.save(f'{family}_add_protected_winner',cost,audit)
            for scale in c.SCALES if cost==25 else ('scale_out_2r_50',):
                audit=(comparison.alpha('alpha_'+scale,cost,position_add_policy=c._policy('protected_winner'))
                       if family=='alpha' else vcp(cost,scale))
                comparison.save(f'{family}_protected_{scale}',cost,audit)
    pairs=[]
    for family in ('alpha','vcp'):
        for cost in (25,40,60):
            base=f'{family}_add_protected_winner';candidate=f'{family}_protected_scale_out_2r_50'
            base_root=root if cost==25 else extra
            b=pd.read_csv(base_root/'main'/f'{base}_{cost}bp/daily_nav.csv')
            a=pd.read_csv(extra/'main'/f'{candidate}_{cost}bp/daily_nav.csv')
            pairs.append(dict(baseline=base,candidate=candidate,slippage_bps=cost,**c.paired_block_interval(b.capital,a.capital)))
    pd.DataFrame(pairs).to_csv(extra/'paired_uncertainty.csv',index=False)
    changed=[p for p,h in comparison.registration['data_files'].items() if sha256_file(p)!=h]
    if changed:raise ValueError('data changed')
    c.write_json(extra/'completed.json',dict(runs=len(comparison.rows),inputs_unchanged=True,golden_passed=True))


if __name__=='__main__':main()
