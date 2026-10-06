#!/usr/bin/env python3
"""Stage-gated ML diagnostics for entry and holding protective-exit risk."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys


ROOT = Path(__file__).resolve().parents[1]
PARENT = Path('/Users/wjy/abu/backtests/alpha158_timing_ml_20261005_v3')
OUTPUT = Path('/Users/wjy/abu/backtests/alpha158_tail_risk_ml_20261005')
FILES = (
    'abupy/AlphaBu/ABuAlphaTimingML.py',
    'configs/selection/alpha158_tail_risk_research_v1.json',
    'scripts/backtest_alpha158_batch_allocation_v1.py',
    'scripts/validate_alpha158_tail_risk_ml_v1.py',
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
    protected = json.loads((PARENT/'experiment.json').read_text())[
        'protected_account_hashes']
    config = json.loads((ROOT/'configs/selection/alpha158_tail_risk_research_v1.json').read_text())
    write_json(output/'experiment.json', {
        'registered_at': datetime.now().astimezone().isoformat(),
        'parent_frozen_experiment': str(PARENT),
        'committed_base': '0b3c997',
        'new_code_sha256': {name: sha(runtime/name) for name in FILES},
        'config': config,
        'primary_model': (
            'L2 logistic regression, C=0.1, median imputation, standard scaling, '
            'balanced classes, group-equal sample weights, seed=0.'),
        'diagnostic_challenger': (
            'Histogram gradient boosting with 7 leaves, 100 iterations, learning '
            'rate 0.05, minimum leaf 50 and L2=10. It cannot be selected for '
            'trading from this experiment.'),
        'entry_label': (
            'After a next-open entry, initial stop at signal close minus 2 ATR. '
            'Positive when the stop is touched before +1R within 20 sessions; '
            'no touch is negative and same-bar first touches are excluded.'),
        'holding_label': (
            'Positive when the unchanged baseline emits INITIAL_STOP or '
            'TRAILING_STOP within the next five sessions. A nearer stagnation '
            'exit is censored.'),
        'purging': (
            'Annual models use only labels ending strictly before January 1. '
            'Every fifth observation is sampled; entry dates and trades receive '
            'equal total weight.'),
        'stage_gate': (
            'For every available evaluation year: AUC >= 0.55, top 10% risk '
            'event lift >= 1.25, and absolute mean-probability calibration error '
            '<= 0.10. Entry requires 2024-2026. Holding requires 2025-2026; '
            '2024 must remain disabled if fewer than 200 purged rows exist.'),
        'next_action': (
            'This run is signal-only. No account replay, threshold choice or '
            'strategy admission occurs even if a model passes.'),
        'limitations': (
            'All dates were observed by prior research. Daily bars make same-bar '
            'barrier order unknowable. Baseline holding states are endogenous.'),
        'protected_account_hashes': {path: sha(path) for path in protected},
        'new_holdout': False,
        'parameter_search': False,
        'automatic_admission': False,
    })
    os.execv(sys.executable, [
        sys.executable, '-B', str(runtime/'scripts/validate_alpha158_tail_risk_ml_v1.py'),
        '--output', str(output), '--frozen'])


def frame(value):
    import pandas as pd
    return value if isinstance(value, pd.DataFrame) else pd.DataFrame(value)


def verify(output, registration, experiment):
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
    from sklearn.metrics import roc_auc_score
    from abupy.AlphaBu.ABuAlphaTimingML import (
        ENTRY_FEATURES, EXIT_FEATURES, TimingMLConfig,
        YearBoundaryLogisticModel, YearBoundaryShallowBoostingModel,
        add_entry_tail_risk_labels, add_holding_protective_exit_labels,
        build_entry_frame, build_exit_training_frame, risk_lift_diagnostics,
    )
    from abupy.AlphaBu.ABuCostAwareAlpha import CostAwareReview
    from scripts import compare_current_strategies_v1 as comparison_module
    from scripts.backtest_alpha158_batch_allocation_v1 import run_low_turnover

    registration = json.loads((output/'registration.json').read_text())
    experiment = json.loads((output/'experiment.json').read_text())
    verify(output, registration, experiment)
    raw_config = experiment['config']
    timing_config = TimingMLConfig(
        training_sample_stride=raw_config['training_sample_stride'],
        logistic_c=raw_config['logistic_c'],
        minimum_training_rows=raw_config['minimum_training_rows'],
        minimum_class_rows=raw_config['minimum_class_rows'],
        random_state=raw_config['random_state'])
    comparison = comparison_module.Comparison(output)
    print('BUILD unchanged baseline holding path', flush=True)
    _, audit = run_low_turnover(
        comparison.panel, comparison.scores,
        replace(comparison.source, label_slippage_bps=25),
        comparison.low, comparison.risk, 20260930,
        review_overlay=CostAwareReview(suppress_rank_exits=True))
    score_lookup = {
        (int(row.signal_asof), str(row.symbol)): float(row.alpha_score)
        for row in comparison.raw[
            ['signal_asof', 'symbol', 'alpha_score']].itertuples(index=False)}

    print('BUILD tail-risk labels', flush=True)
    entry = build_entry_frame(comparison.panel, comparison.scores, timing_config)
    entry = add_entry_tail_risk_labels(
        comparison.panel, entry,
        raw_config['entry_horizon_sessions'],
        raw_config['entry_profit_r_multiple'])
    holding = build_exit_training_frame(
        comparison.panel, frame(audit['risk_positions_daily']),
        frame(audit['logical_trades']), frame(audit['exits']),
        score_lookup, timing_config)
    holding = add_holding_protective_exit_labels(
        comparison.panel, holding, frame(audit['logical_trades']),
        frame(audit['exits']), raw_config['holding_horizon_sessions'])
    entry_sample = entry[entry.training_sample & entry.label.notna()].copy()
    holding_sample = holding[holding.training_sample & holding.label.notna()].copy()

    models = {}
    diagnostics = []
    manifests = {}
    for task, data, features, group, years in (
            ('entry', entry_sample, ENTRY_FEATURES, 'date', (2024, 2025, 2026)),
            ('holding', holding_sample, EXIT_FEATURES, 'trade_id', (2024, 2025, 2026))):
        for model_name, model_type in (
                ('logistic', YearBoundaryLogisticModel),
                ('shallow_boosting', YearBoundaryShallowBoostingModel)):
            print('FIT', task, model_name, flush=True)
            model = model_type(features, timing_config).fit(
                data, years, group, skip_insufficient=True)
            models[(task, model_name)] = model
            manifests[task+'_'+model_name] = model.manifest
            for row in risk_lift_diagnostics(
                    model, data, raw_config['top_risk_fraction']):
                diagnostics.append({'task': task, 'model': model_name, **row})
    diagnostics = pd.DataFrame(diagnostics)

    baseline_rows = []
    for task, data, feature, sign in (
            ('entry', entry_sample, 'atr_fraction', 1.0),
            ('holding', holding_sample, 'stop_distance_r', -1.0)):
        for year, group in data.groupby(data.date//10000):
            label = group.label.astype(int).to_numpy()
            score = sign*group[feature].to_numpy(dtype=float)
            valid = np.isfinite(score)
            baseline_rows.append({
                'task': task, 'year': int(year), 'baseline_feature': feature,
                'rows': int(valid.sum()),
                'auc': (float(roc_auc_score(label[valid], score[valid]))
                        if valid.sum() and len(np.unique(label[valid])) == 2
                        else np.nan),
            })
    pd.DataFrame(baseline_rows).to_csv(output/'simple_baseline_diagnostics.csv', index=False)
    diagnostics.to_csv(output/'risk_diagnostics.csv', index=False)
    write_json(output/'model_manifest.json', manifests)
    entry[[
        'date', 'symbol', 'label', 'label_end', 'tail_stop_first',
        'tail_ambiguous', 'tail_event_offset', 'training_sample',
        *ENTRY_FEATURES]].to_csv(output/'entry_tail_risk_examples.csv.gz', index=False)
    holding[[
        'date', 'symbol', 'trade_id', 'label', 'label_end',
        'protective_exit_within_horizon', 'label_event_reason',
        'training_sample', *EXIT_FEATURES,
    ]].to_csv(output/'holding_tail_risk_examples.csv.gz', index=False)

    gates = {}
    for task, required_years in (('entry', (2024, 2025, 2026)),
                                 ('holding', (2025, 2026))):
        for model_name in ('logistic', 'shallow_boosting'):
            selected = diagnostics[(diagnostics.task == task) &
                                   (diagnostics.model == model_name)].set_index('year')
            criteria = {}
            for year in required_years:
                available = year in selected.index
                criteria[f'{year}_available'] = available
                if available:
                    row = selected.loc[year]
                    criteria[f'{year}_auc'] = bool(
                        row.auc >= raw_config['minimum_year_auc'])
                    criteria[f'{year}_lift'] = bool(
                        row.top_lift >= raw_config['minimum_top_risk_lift'])
                    criteria[f'{year}_calibration'] = bool(
                        abs(row.mean_probability-row.event_rate) <=
                        raw_config['maximum_calibration_error'])
            gates[task+'_'+model_name] = {
                'passed': bool(criteria) and all(criteria.values()),
                'criteria': criteria,
            }
    write_json(output/'stage_gate.json', {
        'gates': gates, 'account_replay_allowed': False,
        'automatic_admission': False})
    summarize(output, diagnostics, pd.DataFrame(baseline_rows), gates, manifests)
    verify(output, registration, experiment)
    write_json(output/'completion.json', {
        'status': 'COMPLETE', 'account_replays': 0,
        'models_fitted': int(sum(len(model.models) for model in models.values())),
        'inputs_unchanged': True, 'protected_accounts_unchanged': True,
        'new_holdout': False, 'parameter_search': False,
        'automatic_admission': False})
    print('COMPLETE', flush=True)


def summarize(output, diagnostics, baselines, gates, manifests):
    lines = [
        '# Alpha158 尾部止损风险机器学习诊断', '',
        '本轮把目标改为与原策略风险机制一致的事件：新候选是否在 +1R 前先触发初始止损，以及已有持仓是否会在未来 5 个交易日触发初始或移动止损。只评估预测，不改变交易账户。', '',
        '## 年度诊断', '',
        '| 任务 | 模型 | 年份 | 样本 | 事件率 | AUC | Top 10% 事件率 | Lift | 概率均值 | Brier |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|',
    ]
    for row in diagnostics.itertuples():
        lines.append('| {} | {} | {} | {} | {:.1%} | {:.3f} | {:.1%} | {:.2f} | {:.1%} | {:.3f} |'.format(
            row.task, row.model, row.year, row.rows, row.event_rate,
            row.auc, row.top_event_rate, row.top_lift,
            row.mean_probability, row.brier))
    lines += ['', '## 简单规则基线', '',
              '| 任务 | 年份 | 单一特征 | AUC |', '|---|---:|---|---:|']
    for row in baselines.itertuples():
        lines.append('| {} | {} | {} | {:.3f} |'.format(
            row.task, row.year, row.baseline_feature, row.auc))
    lines += ['', '## 预登记阶段门', '']
    for name, result in gates.items():
        lines.append('- {}：{}'.format(name, '通过' if result['passed'] else '未通过'))
    lines += [
        '',
        '无论阶段门结果如何，本轮都不运行账户级回测，也不从逻辑回归和提升模型中挑选历史表现最好者。只有冻结后的新增数据仍显示稳定风险区分能力，才讨论低比例风险提示或候选重排。',
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
