#!/usr/bin/env python3
"""Preregistered Alpha158 entry meta-label and continuation-exit experiment."""
from __future__ import annotations

import argparse
from dataclasses import fields, replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
PARENT = Path('/Users/wjy/abu/backtests/alpha158_batch_allocation_20261004')
OUTPUT = Path('/Users/wjy/abu/backtests/alpha158_timing_ml_20261005_v3')
ARMS = ('alpha_no_rank_exit', 'alpha_entry_meta', 'alpha_exit_continuation')
LABELS = {
    'alpha_no_rank_exit': '当前候选',
    'alpha_entry_meta': '仅入场元标签',
    'alpha_exit_continuation': '仅持仓继续模型',
}
FILES = (
    'abupy/AlphaBu/ABuAlphaTimingML.py',
    'configs/selection/alpha158_timing_ml_research_v1.json',
    'scripts/backtest_alpha158_batch_allocation_v1.py',
    'scripts/validate_alpha158_timing_ml_v1.py',
    'tests/test_alpha_timing_ml.py',
)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(8*1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(
        value, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')


def freeze(output):
    output.mkdir(parents=True, exist_ok=False)
    runtime = output/'runtime'
    shutil.copytree(PARENT/'runtime', runtime)
    for name in FILES:
        destination = runtime/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT/name, destination)
    registration = json.loads((PARENT/'registration.json').read_text())
    registration['runtime_files'] = {
        str(path.relative_to(runtime)): sha(path)
        for path in runtime.rglob('*') if path.is_file()}
    write_json(output/'registration.json', registration)
    cases = [
        dict(scope=scope, start=start, end=end, cost=cost, strategy=arm)
        for scope, start, end in (
            ('history2024_2025', 20240102, 20251231),
            ('reset2026', 20260105, 20260930),
        )
        for cost in (25, 40, 60)
        for arm in ARMS
    ]
    protected = json.loads((PARENT/'experiment.json').read_text())[
        'protected_account_hashes']
    config = json.loads((ROOT/'configs/selection/alpha158_timing_ml_research_v1.json').read_text())
    write_json(output/'experiment.json', {
        'registered_at': datetime.now().astimezone().isoformat(),
        'parent_frozen_experiment': str(PARENT),
        'committed_base': '0b3c997',
        'new_code_sha256': {name: sha(runtime/name) for name in FILES},
        'config': config,
        'cases': cases,
        'model_specification': (
            'Median imputation, standard scaling and L2 logistic regression. '
            'C=0.1, balanced classes, seed=0. No hyperparameter search.'),
        'entry_label': (
            'Next-open to signal+20 close absolute return minus 70 bps is positive. '
            'Training uses top-50 rows every fifth session; inference only gates '
            'candidates already selected by the frozen policy.'),
        'exit_label': (
            'Next-open to signal+5 close absolute continuation return is positive. '
            'Training uses every fifth holding observation from the frozen baseline, '
            'excluding days where an existing protective exit fired.'),
        'purging': (
            'A year model uses only labels whose label_end is strictly before '
            'January 1 of that year. Models refit only at year boundaries.'),
        'frozen_actions': (
            'Entry model uses p>=0.50. Exit model requires p<=0.35 on two '
            'consecutive sessions. Existing initial, trailing and stagnation exits '
            'remain authoritative. Risk budgets and sizing are unchanged.'),
        'historical_screen': (
            'Each overlay is assessed alone at 25/40/60 bps in 2024-2025 and '
            'a fresh-cash 2026 replay. Report annual results, exposure, signal '
            'coverage, predictive calibration and paired uncertainty. Do not tune '
            'thresholds after results and never auto-admit.'),
        'limitations': (
            'All dates are previously observed historical data. The 2023 history '
            'available for the first yearly model is short. Exit training states '
            'come from the baseline path. This is a screening experiment, not a '
            'new untouched holdout.'),
        'registration_amendment': (
            'The first frozen execution stopped before any model fit or return '
            'result because the 2024 exit model had 132 purged rows versus the '
            'predeclared 200-row minimum. The minimum was retained; an unavailable '
            'year model is now skipped and produces no overlay action. The failed '
            'artifact is preserved at alpha158_timing_ml_20261005. A second frozen '
            'execution completed every account replay but stopped while composing '
            'the report because the annual table did not yet contain a drawdown '
            'field. That reporting-only artifact is preserved at '
            'alpha158_timing_ml_20261005_v2; model rules and results were not used '
            'to alter any parameter.'),
        'protected_account_hashes': {path: sha(path) for path in protected},
        'new_holdout': False,
        'parameter_search': False,
        'automatic_admission': False,
    })
    os.execv(sys.executable, [
        sys.executable, '-B', str(runtime/'scripts/validate_alpha158_timing_ml_v1.py'),
        '--output', str(output), '--frozen'])


def frame(value):
    import pandas as pd
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame(value)


def verify_inputs(output, registration, experiment):
    for path, digest in registration['data_files'].items():
        if sha(path) != digest:
            raise ValueError('data changed: '+path)
    for path, digest in registration['runtime_files'].items():
        if sha(output/'runtime'/path) != digest:
            raise ValueError('runtime changed: '+path)
    for path, digest in experiment['protected_account_hashes'].items():
        if sha(path) != digest:
            raise ValueError('protected account changed: '+path)


def run(output):
    sys.path.insert(0, str(output/'runtime'))
    import numpy as np
    import pandas as pd
    from abupy.AlphaBu.ABuAlphaTimingML import (
        ENTRY_FEATURES, EXIT_FEATURES, EntryMetaOverlay,
        ExitContinuationOverlay, TimingMLConfig, YearBoundaryLogisticModel,
        build_entry_frame, build_exit_training_frame,
    )
    from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview
    from scripts import compare_current_strategies_v1 as comparison_module
    from scripts.backtest_alpha158_batch_allocation_v1 import run_low_turnover
    from scripts.validate_alpha158_cost_gate_v1 import paired_block_interval

    registration = json.loads((output/'registration.json').read_text())
    experiment = json.loads((output/'experiment.json').read_text())
    verify_inputs(output, registration, experiment)
    config = TimingMLConfig(**experiment['config'])
    comparison = comparison_module.Comparison(output)
    scores = comparison.scores
    raw = comparison.raw
    source25 = replace(comparison.source, label_slippage_bps=25)
    print('BUILD full baseline training path', flush=True)
    _, training_audit = run_low_turnover(
        comparison.panel, scores, source25, comparison.low, comparison.risk,
        20260930, review_overlay=CostAwareReview(suppress_rank_exits=True))

    print('BUILD entry examples', flush=True)
    entry_frame = build_entry_frame(comparison.panel, scores, config)
    entry_training = entry_frame[entry_frame.training_sample].copy()
    entry_model = YearBoundaryLogisticModel(ENTRY_FEATURES, config).fit(
        entry_training, (2024, 2025, 2026), 'date')
    score_lookup = {
        (int(row.signal_asof), str(row.symbol)): float(row.alpha_score)
        for row in raw[['signal_asof', 'symbol', 'alpha_score']].itertuples(index=False)}

    print('BUILD exit examples', flush=True)
    exit_frame = build_exit_training_frame(
        comparison.panel, frame(training_audit['risk_positions_daily']),
        frame(training_audit['logical_trades']), frame(training_audit['exits']),
        score_lookup, config)
    exit_training = exit_frame[exit_frame.training_sample].copy()
    exit_model = YearBoundaryLogisticModel(EXIT_FEATURES, config).fit(
        exit_training, (2024, 2025, 2026), 'trade_id',
        skip_insufficient=True)

    entry_columns = ['date', 'symbol', 'label_end', 'label_return', 'label',
                     'training_sample', *ENTRY_FEATURES]
    exit_columns = ['date', 'symbol', 'trade_id', 'label_end', 'label_return',
                    'label', 'training_sample', 'base_exit_day', *EXIT_FEATURES]
    entry_frame[entry_columns].to_csv(output/'entry_examples.csv.gz', index=False)
    exit_frame[exit_columns].to_csv(output/'exit_examples.csv.gz', index=False)
    write_json(output/'model_manifest.json', {
        'entry': entry_model.manifest, 'exit': exit_model.manifest,
        'entry_features': ENTRY_FEATURES, 'exit_features': EXIT_FEATURES,
    })
    diagnostics = []
    for kind, model, dataset in (
            ('entry', entry_model, entry_training),
            ('exit', exit_model, exit_training)):
        for row in model.score(dataset):
            diagnostics.append({'model': kind, **row})
    pd.DataFrame(diagnostics).to_csv(output/'predictive_diagnostics.csv', index=False)

    decision_rows = []
    for case in experiment['cases']:
        scope, begin, end, cost, arm = (
            case[key] for key in ('scope', 'start', 'end', 'cost', 'strategy'))
        print('RUN', scope, arm, cost, flush=True)
        period_scores = scores[scores.signal_asof.between(begin, end)]
        options = {'review_overlay': CostAwareReview(suppress_rank_exits=True)}
        if arm == 'alpha_entry_meta':
            options['entry_overlay'] = EntryMetaOverlay(entry_model, entry_frame, config)
        elif arm == 'alpha_exit_continuation':
            options['exit_overlay'] = ExitContinuationOverlay(exit_model, score_lookup, config)
        result, audit = run_low_turnover(
            comparison.panel, period_scores,
            replace(comparison.source, label_slippage_bps=cost),
            comparison.low, comparison.risk, end, **options)
        comparison.save(arm, cost, audit, scope=scope, begin=begin, end=end)
        if arm == 'alpha_entry_meta':
            decisions = pd.DataFrame(audit['entry_ml_decisions'])
            decision_rows.append({
                'scope': scope, 'strategy': arm, 'slippage_bps': cost,
                'evaluations': len(decisions),
                'ready': int(decisions.status.eq('READY').sum()),
                'allowed': int(decisions.allowed.sum()),
                'triggered': 0,
            })
        elif arm == 'alpha_exit_continuation':
            decisions = pd.DataFrame(audit['exit_ml_decisions'])
            decision_rows.append({
                'scope': scope, 'strategy': arm, 'slippage_bps': cost,
                'evaluations': len(decisions),
                'ready': int(decisions.status.eq('READY').sum()),
                'allowed': 0,
                'triggered': int(decisions.triggered.sum()),
            })
    pd.DataFrame(decision_rows).to_csv(output/'decision_statistics.csv', index=False)

    uncertainty = []
    for scope in ('history2024_2025', 'reset2026'):
        for cost in (25, 40, 60):
            baseline = comparison.navs[(scope, ARMS[0], cost)].capital.to_numpy()
            for arm in ARMS[1:]:
                candidate = comparison.navs[(scope, arm, cost)].capital.to_numpy()
                uncertainty.append({
                    'scope': scope, 'baseline': ARMS[0], 'candidate': arm,
                    'slippage_bps': cost,
                    **paired_block_interval(baseline, candidate),
                })
    pd.DataFrame(uncertainty).to_csv(output/'paired_uncertainty.csv', index=False)
    summarize(output, comparison, pd.DataFrame(decision_rows),
              pd.DataFrame(diagnostics), pd.DataFrame(uncertainty))
    verify_inputs(output, registration, experiment)
    write_json(output/'completion.json', {
        'status': 'COMPLETE', 'strategy_runs': len(comparison.rows),
        'entry_models': len(entry_model.models), 'exit_models': len(exit_model.models),
        'inputs_unchanged': True, 'protected_accounts_unchanged': True,
        'new_holdout': False, 'parameter_search': False,
        'automatic_admission': False,
    })
    print('COMPLETE', flush=True)


def summarize(output, comparison, decisions, diagnostics, uncertainty):
    import numpy as np
    import pandas as pd

    results = pd.DataFrame(comparison.rows)
    annual = pd.concat(comparison.annual, ignore_index=True)
    annual_drawdowns = []
    for row in annual.itertuples():
        curve = comparison.navs[(row.scope, row.strategy, row.slippage_bps)]
        years = curve.date.astype(int)//10000
        year_curve = curve[years.eq(row.year)]
        earlier = curve[years.lt(row.year)]
        anchor = (float(earlier.capital.iloc[-1]) if len(earlier)
                  else 1_000_000.0)
        capital = np.r_[anchor, year_curve.capital.to_numpy(dtype=float)]
        annual_drawdowns.append(float(
            (capital/np.maximum.accumulate(capital)-1).min()*100))
    annual['max_drawdown_pct'] = annual_drawdowns
    annual.to_csv(output/'annual_metrics.csv', index=False)
    criteria = {}
    for arm in ARMS[1:]:
        for scope in ('history2024_2025', 'reset2026'):
            for cost in (25, 40, 60):
                group = results[(results.scope == scope) &
                                (results.slippage_bps == cost)].set_index('strategy')
                criteria[f'{arm}_{scope}_{cost}bp_return'] = bool(
                    group.loc[arm, 'return_pct'] > group.loc[ARMS[0], 'return_pct'])
            for year, group in annual[(annual.scope == scope) &
                                      (annual.slippage_bps == 25)].groupby('year'):
                group = group.set_index('strategy')
                criteria[f'{arm}_{year}_return'] = bool(
                    group.loc[arm, 'return_pct'] > group.loc[ARMS[0], 'return_pct'])
                criteria[f'{arm}_{year}_drawdown'] = bool(
                    group.loc[arm, 'max_drawdown_pct'] >=
                    group.loc[ARMS[0], 'max_drawdown_pct'])
    arm_pass = {
        arm: all(value for key, value in criteria.items() if key.startswith(arm+'_'))
        for arm in ARMS[1:]}
    write_json(output/'historical_screen.json', {
        'arm_pass': arm_pass, 'criteria': criteria,
        'automatic_admission': False, 'new_holdout': False})

    lines = [
        '# Alpha158 买入与卖出时点机器学习实验', '',
        '本轮只检验两个独立覆盖层：入场元标签、持仓继续概率。当前取消排名退出候选、风险预算、仓位数量与保护性退出均保持不变；没有组合两个模型，也没有根据结果调阈值。全部区间此前均已观察，因此只能作为历史筛选。', '',
        '## 组合结果（25 bp）', '',
        '| 范围 | 版本 | 收益 | 最大回撤幅度 | 平均仓位 | 买入笔数 |',
        '|---|---|---:|---:|---:|---:|',
    ]
    for row in results[results.slippage_bps.eq(25)].itertuples():
        lines.append('| {} | {} | {:.2f}% | {:.2f}% | {:.2f}% | {} |'.format(
            row.scope, LABELS[row.strategy], row.return_pct,
            abs(row.max_drawdown_pct), row.average_exposure_pct, row.filled_buys))
    lines += ['', '## 逐年结果（25 bp）', '',
              '| 范围 | 年份 | 版本 | 收益 | 最大回撤幅度 |',
              '|---|---:|---|---:|---:|']
    for row in annual[annual.slippage_bps.eq(25)].itertuples():
        lines.append('| {} | {} | {} | {:.2f}% | {:.2f}% |'.format(
            row.scope, row.year, LABELS[row.strategy], row.return_pct,
            abs(row.max_drawdown_pct)))
    lines += ['', '## 成本压力', '',
              '| 范围 | 滑点 | 当前候选 | 入场元标签 | 持仓继续模型 |',
              '|---|---:|---:|---:|---:|']
    for scope in ('history2024_2025', 'reset2026'):
        for cost in (25, 40, 60):
            group = results[(results.scope == scope) &
                            (results.slippage_bps == cost)].set_index('strategy')
            lines.append('| {} | {} bp | {:.2f}% | {:.2f}% | {:.2f}% |'.format(
                scope, cost, *(group.loc[arm, 'return_pct'] for arm in ARMS)))
    lines += ['', '## 模型诊断', '',
              '| 模型 | 年份 | 样本 | 正例率 | 平均预测概率 | AUC | Brier |',
              '|---|---:|---:|---:|---:|---:|---:|']
    for row in diagnostics.itertuples():
        lines.append('| {} | {} | {} | {:.1%} | {:.1%} | {:.3f} | {:.3f} |'.format(
            row.model, row.year, row.rows, row.positive_rate,
            row.mean_probability, row.auc, row.brier))
    lines += ['', '## 决策覆盖（25 bp）', '',
              '| 范围 | 版本 | 评估次数 | 可用预测 | 放行 / 触发退出 |',
              '|---|---|---:|---:|---:|']
    for row in decisions[decisions.slippage_bps.eq(25)].itertuples():
        action = row.allowed if row.strategy == 'alpha_entry_meta' else row.triggered
        lines.append('| {} | {} | {} | {} | {} |'.format(
            row.scope, LABELS[row.strategy], row.evaluations, row.ready, action))
    lines += ['', '## 预登记筛选', '']
    for arm in ARMS[1:]:
        lines.append('- {}：{}'.format(LABELS[arm], '通过' if arm_pass[arm] else '未通过'))
    lines += [
        '',
        '通过历史筛选也不会自动替换模拟盘；必须先检查收益是否来自仓位下降、少数交易或账户路径重分配，再进入冻结后的新增前瞻观察。未通过的版本不继续围绕这些年份搜索阈值。',
        '',
        '完整逐日净值、成交、模型决策、预测诊断、年度结果与配对区块重采样区间均保存在本目录。',
    ]
    (output/'REPORT.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    parser.add_argument('--frozen', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if not args.frozen:
        freeze(output)
    run(output)


if __name__ == '__main__':
    main()
