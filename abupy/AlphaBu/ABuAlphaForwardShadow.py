"""Frozen, paired prospective Alpha accounts with immutable daily commits.

Only our own hash-verified pickle artifacts are loaded. Pickles are internal
checkpoints, not an interchange format for third-party data. No broker/network
or messaging API is imported here. Collection is a separate CLI operation.
"""
from __future__ import annotations
import copy
import gzip
import json
import os
import pickle
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd

from .ABuAlpha158Lite import Alpha158LiteFeatureEngine, Alpha158LiteModel, Alpha158LiteExitEngine, Alpha158LiteLowTurnoverPolicy
from .ABuArtifactManifest import sha256_file
from .ABuPortfolioExecutor import ExecutionConfig, PortfolioExecutor
from .ABuPortfolioRisk import PortfolioRiskEngine
from .ABuTradeIntent import TradeIntent, make_record_id
from .ABuVCPPaperTrading import _bind_next_session, _freeze_new_orders

ARMS = ('baseline', 'event_exit_only')


def now_shanghai():
    return datetime.now(ZoneInfo('Asia/Shanghai'))


def json_write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   sort_keys=True, allow_nan=False)+'\n')


def dump_pickle(path, value):
    with gzip.open(path, 'wb', compresslevel=1) as stream:
        pickle.dump(value, stream, protocol=5)


def load_pickle(path):
    with gzip.open(path, 'rb') as stream:
        return pickle.load(stream)


def file_manifest(directory):
    directory = Path(directory)
    return {str(p.relative_to(directory)):sha256_file(p)
            for p in sorted(directory.rglob('*')) if p.is_file() and p.name != 'commit.json'
            and '__pycache__' not in p.parts and p.suffix != '.pyc'}


def verify_files(directory, records):
    directory = Path(directory).resolve()
    for name, digest in records.items():
        path = (directory/name).resolve()
        if directory not in path.parents or not path.is_file() or sha256_file(path) != digest:
            raise ValueError('archive integrity failure: '+str(path))


def commit_directory(staging, destination, previous_hash):
    staging, destination = Path(staging), Path(destination)
    if destination.exists():
        raise FileExistsError('immutable commit already exists: '+str(destination))
    json_write(staging/'commit.json', dict(previous_commit=previous_hash,files=file_manifest(staging)))
    destination.parent.mkdir(parents=True,exist_ok=True)
    os.rename(staging,destination)


def committed_days(root):
    return sorted(p for p in (Path(root)/'sessions').glob('[0-9]'*8) if p.is_dir())


def verify_chain(root):
    root = Path(root)
    previous = 'GENESIS'
    paths = [root/'genesis']+committed_days(root)
    for path in paths:
        manifest = json.loads((path/'commit.json').read_text())
        if manifest['previous_commit'] != previous:
            raise ValueError('broken shadow commit chain')
        verify_files(path,manifest['files'])
        previous = sha256_file(path/'commit.json')
    return paths,previous


def validate_protocol(protocol):
    if (protocol['real_orders_allowed'] or protocol['automatic_admission'] or
            protocol['notify_external_channels']):
        raise ValueError('shadow must not trade, notify, or auto-admit')
    if protocol['model_update_policy'] != 'fit_once_at_registration_then_frozen':
        raise ValueError('model update rule differs from frozen protocol')
    if protocol['evaluation_months'] != [6,9,12,18]:
        raise ValueError('fixed evaluation checkpoints required')
    if protocol['minimum_entry_date_clusters_per_arm'] < 30:
        raise ValueError('minimum cluster count cannot be relaxed')


def train_once(panel, source, protocol):
    """Mature labels strictly before registration snapshot, fixed Ridge config."""
    engine = Alpha158LiteFeatureEngine(panel,source)
    last = len(panel.dates)-1
    days = np.flatnonzero((panel.dates >= protocol['initial_training_start']) &
                         (np.arange(len(panel.dates)) >= source.minimum_history_sessions) &
                         (np.arange(len(panel.dates))+source.label_horizon_sessions < last))
    if len(days) < source.minimum_train_dates:
        raise ValueError('insufficient mature training dates')
    days = days[::source.training_date_stride]
    frames = []
    for count,day in enumerate(days):
        frame = engine.snapshot(int(day),include_labels=True)
        frames.append(frame)
        if count % 40 == 0:
            print(f'Training snapshot {count+1}/{len(days)}',flush=True)
    training = pd.concat(frames,ignore_index=True)
    model = Alpha158LiteModel(source).fit(training,panel.dates[days])
    model.manifest.update(training_snapshot_date=int(panel.dates[-1]),
        maximum_label_end=int(panel.dates[int(days[-1])+source.label_horizon_sessions]),
        training_date_stride=source.training_date_stride,
        update_policy=protocol['model_update_policy'])
    assert model.manifest['maximum_label_end'] < int(panel.dates[-1])
    return model


def feature_scores(panel, source, model, minimum):
    engine = Alpha158LiteFeatureEngine(panel,source)
    frame = engine.snapshot(len(panel.dates)-1,include_labels=False)
    if len(frame) < minimum:
        raise ValueError('insufficient eligible forward candidates')
    frame['alpha_score'] = model.predict(frame)
    frame = frame.sort_values(['alpha_score','symbol'],ascending=[False,True],kind='mergesort')
    frame['daily_rank'] = np.arange(1,len(frame)+1)
    return frame.reset_index(drop=True)


def new_state(panel, source, policy, risk, protocol, registered_at):
    accounts = {}
    for arm in ARMS:
        executor = PortfolioExecutor(panel,ExecutionConfig(
            initial_cash=protocol['initial_cash_per_account'],
            slippage_bps=protocol['primary_slippage_bps'],mode='pit_corrected',max_positions=10))
        for col in range(len(panel.symbols)):
            valid = panel.exec_close[:,col]
            valid = valid[np.isfinite(valid)&(valid>0)]
            if len(valid):
                executor.last_close[col] = valid[-1]
        accounts[arm] = dict(executor=executor,exit_states={},entry_streak={},weak_streak={},
            intents={},entry_intents={},risk_decisions=[],exit_reasons=[])
    return dict(version=protocol['version'],registered_at=registered_at,
        historical_cutoff=int(panel.dates[-1]),last_processed_date=int(panel.dates[-1]),
        source=source,policy=policy,risk=risk,protocol=protocol,accounts=accounts,
        first_forward_date=None,sessions=0,evaluation_checkpoint=0,
        evaluation_status='WAITING_FOR_FIRST_FORWARD_SESSION',evaluation_asof=None)


def checkpoint_state(state):
    # Keep complete ledger/receivables/exit/rank state without duplicating panel.
    saved = dict(state,accounts={})
    for name,account in state['accounts'].items():
        item = dict(account)
        item['executor'] = copy.copy(account['executor'])
        item['executor'].panel = None
        saved['accounts'][name] = item
    return saved


def assert_append_only(previous, current):
    if previous.symbols != current.symbols:
        raise ValueError('frozen security universe changed; new version required')
    old = len(previous.dates)
    if len(current.dates) != old+1 or not np.array_equal(previous.dates,current.dates[:old]):
        raise ValueError('exactly one new session required; gaps/backfills are not prospective')
    # Revisions of history cannot silently rewrite the known information set.
    for left,right in ((previous.base,current.base),(previous,current)):
        for name,value in vars(left).items():
            candidate = vars(right).get(name)
            if isinstance(value,np.ndarray) and isinstance(candidate,np.ndarray) and value.ndim and candidate.shape == (old+1,)+value.shape[1:] and value.shape[0] == old:
                same = np.array_equal(value,candidate[:old],equal_nan=True) if value.dtype.kind in 'fciub' else np.array_equal(value,candidate[:old])
                if not same:
                    raise ValueError('historical panel revision: '+name)


def panel_delta(previous,current):
    """Archive the new row plus metadata, sharing the hash-anchored old history."""
    assert_append_only(previous,current)
    delta = {}
    old = len(previous.dates)
    for key,left,right in (('base',previous.base,current.base),('wrapper',previous,current)):
        fields = {}
        for name,value in vars(right).items():
            if name == 'base':
                continue
            prior = vars(left).get(name)
            append = (isinstance(value,np.ndarray) and isinstance(prior,np.ndarray)
                      and value.ndim and prior.shape == (old,)+value.shape[1:]
                      and value.shape[0] == old+1)
            fields[name] = ('append',value[-1:].copy()) if append else ('replace',value)
        delta[key] = fields
    return delta


def apply_panel_delta(previous,delta):
    result = copy.copy(previous)
    result.base = copy.copy(previous.base)
    for key,item in (('base',result.base),('wrapper',result)):
        for name,(operation,value) in delta[key].items():
            setattr(item,name,np.concatenate([vars(item)[name],value],axis=0)
                    if operation == 'append' else value)
    return result


def replay_panels(paths):
    panel = load_pickle(paths[0]/'panel.pkl.gz')
    yield panel
    for directory in paths[1:]:
        panel = apply_panel_delta(panel,load_pickle(directory/'panel_delta.pkl.gz'))
        yield panel


def last_panel(paths):
    """Rebuild current history with one concatenation per matrix."""
    panel = load_pickle(paths[0]/'panel.pkl.gz')
    chunks = {}
    for directory in paths[1:]:
        delta = load_pickle(directory/'panel_delta.pkl.gz')
        for key,item in (('base',panel.base),('wrapper',panel)):
            for name,(operation,value) in delta[key].items():
                identity = (key,name)
                if operation == 'append':
                    chunks.setdefault(identity,[vars(item)[name]]).append(value)
                else:
                    chunks.pop(identity,None)
                    setattr(item,name,value)
    for (key,name),values in chunks.items():
        setattr(panel.base if key == 'base' else panel,name,np.concatenate(values,axis=0))
    return panel


def forward_actions(panel, frame, after_date=0):
    """Retain future payment dates instead of losing end-of-panel receivables.

    Negative keys encode calendar YYYYMMDD, resolved on the first observed session
    on/after payment. This adapter is exclusive to the new shadow executor.
    """
    symbols = {s:i for i,s in enumerate(panel.symbols)}
    dates = {int(d):i for i,d in enumerate(panel.dates)}
    actions = {}
    seen = set()
    by_record = {}
    for row in frame.to_dict('records'):
        symbol = str(row.get('symbol'))
        record = pd.to_datetime(row.get('股权登记日'),errors='coerce')
        if symbol not in symbols or pd.isna(record):
            continue
        record_date = int(record.strftime('%Y%m%d'))
        if record_date <= after_date:
            continue
        if record_date not in dates:
            continue
        def amount(name):
            value = pd.to_numeric(row.get(name),errors='coerce')
            return 0. if pd.isna(value) else float(value)/10
        cash,stock = amount('派息比例'),amount('送股比例')+amount('转增比例')
        if not cash and not stock:
            continue
        def payment(name, amount_value):
            value = pd.to_datetime(row.get(name),errors='coerce')
            if pd.isna(value):
                value = pd.to_datetime(row.get('除权日'),errors='coerce')
            if pd.isna(value) or int(value.strftime('%Y%m%d')) <= record_date:
                return None
            return -int(value.strftime('%Y%m%d')) if amount_value else None
        event = dict(symbol=symbols[symbol],cash_per_share=cash,stock_per_share=stock,
            cash_day=payment('派息日',cash),stock_day=payment('股份到账日',stock),
            description=str(row.get('实施方案分红说明','')))
        key = (record_date,symbol,cash,stock,event['cash_day'],event['stock_day'])
        identity = (record_date,symbol)
        if identity in by_record and by_record[identity] != key:
            raise ValueError('conflicting corporate-action records: '+str(identity))
        by_record[identity] = key
        ex = pd.to_datetime(row.get('除权日'),errors='coerce')
        event['ex_date'] = None if pd.isna(ex) else int(ex.strftime('%Y%m%d'))
        if key not in seen:
            actions.setdefault(dates[record_date],[]).append(event)
            seen.add(key)
    panel.base.corporate_actions = actions


def credit_calendar_receivables(executor,day,date):
    for receivables in (executor._cash_receivables,executor._share_receivables):
        for key in list(receivables):
            if key < 0 and -key <= date:
                receivables.setdefault(day,[]).extend(receivables.pop(key))


def step_accounts(state,panel,scores,check_checkpoint=True):
    """Process prior frozen orders first; approve today's orders at close."""
    day = len(panel.dates)-1
    date = int(panel.dates[day])
    if date <= state['last_processed_date']:
        raise ValueError('session already processed or historical')
    if state['evaluation_status'] in ('REVIEW_REQUIRED','INSUFFICIENT_OBSERVATIONS','REVIEWED'):
        raise ValueError('experiment stopped at predeclared checkpoint')
    if state['first_forward_date'] is None:
        state['first_forward_date'] = date
    source,policy_config = state['source'],state['policy']
    features = Alpha158LiteFeatureEngine(panel,source)
    risk = PortfolioRiskEngine(panel,state['risk'])
    daily = scores[scores.daily_rank <= policy_config.retention_rank_limit]
    for arm,account in state['accounts'].items():
        executor = account['executor']; executor.panel = panel
        exits = Alpha158LiteExitEngine(panel,source); exits.states = account['exit_states']
        policy = Alpha158LiteLowTurnoverPolicy(policy_config)
        policy.entry_streak = account['entry_streak']; policy.weak_streak = account['weak_streak']
        credit_calendar_receivables(executor,day,date)
        _bind_next_session(executor,date)
        for fill in executor.process_open(day):
            if fill.status != 'filled':
                continue
            if fill.side == 'buy':
                intent = account['intents'][fill.intent_id]
                exits.register_entry(intent,fill,day)
                account['entry_intents'][fill.symbol] = intent
            else:
                exits.remove(fill.symbol); account['entry_intents'].pop(fill.symbol,None)
        held = set(executor.positions)
        for event in panel.corporate_actions.get(day,[]):
            if panel.symbols[event['symbol']] in held and event['stock_per_share']:
                raise ValueError('stock distribution requires an audited entitlement ledger; experiment paused')
            if panel.symbols[event['symbol']] in held and (
                    (event['cash_per_share'] and event['cash_day'] is None) or
                    (event['stock_per_share'] and event['stock_day'] is None)):
                raise ValueError('held corporate-action payment date unavailable')
            if panel.symbols[event['symbol']] in held and (
                    event.get('ex_date') is None or event['ex_date'] <= date or
                    (event['cash_per_share'] and -event['cash_day'] < event['ex_date'])):
                raise ValueError('invalid held corporate-action ex/payment chronology')
        executor.process_close(day)
        # Reserve the maximum 20% dividend tax; no optimistic holding-period rebate.
        for event in panel.corporate_actions.get(day,[]):
            symbol = panel.symbols[event['symbol']]
            if symbol in held and event['cash_per_share']:
                for item in executor._cash_receivables[event['cash_day']]:
                    if item['symbol'] == symbol and 'ex_date' not in item:
                        item['cash'] *= .8
                        item['ex_date'] = event['ex_date']
        # Entitlements enter NAV on ex-date, not on record date or payment date.
        receivable = sum(item['cash'] for items in executor._cash_receivables.values()
                         for item in items if item['ex_date'] <= date)
        nav = executor.curve[-1]
        nav['dividend_receivable'] = receivable
        nav['capital'] += receivable
        nav['exposure'] = nav['stocks']/nav['capital'] if nav['capital'] else 0.
        for key in tuple(nav):
            if key.startswith('liquidation_nav_'):
                nav[key] += receivable
        pending = []
        for symbol in sorted(executor.positions):
            if any(o.side=='sell' and o.symbol==symbol for o in executor.orders):
                continue
            reason = exits.signal(day,symbol)
            if reason:
                pending.append((symbol,reason))
        entry_symbols = []
        if state['sessions'] % policy_config.review_interval_sessions == 0:
            holding_sessions = {s:day-exits.states[s].entry_day+1 for s in executor.positions}
            ranked_exits,entry_symbols = policy.review(daily,executor.positions,holding_sessions,
                                                       blocked_exits={s for s,_ in pending})
            if arm == 'baseline':
                pending.extend((s,'PERSISTENT_RANK_EXIT') for s in ranked_exits)
        created = set()
        for symbol,reason in pending:
            entry = account['entry_intents'][symbol]
            sell = TradeIntent(intent_id=make_record_id('alpha-shadow-exit',arm,date,symbol,reason),
                strategy_id=entry.strategy_id,strategy_version='shadow_v1',signal_asof=date,
                symbol=symbol,side='sell',metadata={'exit_reason':reason})
            order,_ = executor.approve_order(sell,executor.positions[symbol].quantity,date)
            if order is not None:
                created.add(order.order_id)
            account['exit_reasons'].append(dict(date=date,symbol=symbol,reason=reason))
        slots = max(0,policy_config.target_positions-len(executor.positions)+len(pending))
        held_or_ordered = set(executor.positions)|{o.symbol for o in executor.orders if o.side=='buy'}
        for symbol in entry_symbols:
            if slots <= 0:
                break
            if symbol in held_or_ordered:
                continue
            row = daily[daily.symbol==symbol].iloc[0]
            intent = features.make_intent(day,int(row['column']),float(row.alpha_score))
            if intent is None:
                continue
            intent = replace(intent,intent_id=make_record_id('alpha-shadow-entry',arm,date,symbol),
                             strategy_id='alpha_shadow_'+arm)
            order,_,decision = risk.approve(executor,intent,day,day)
            account['risk_decisions'].append(asdict(decision))
            held_or_ordered.add(symbol)
            if order is not None:
                created.add(order.order_id); slots -= 1
                account['intents'][intent.intent_id] = intent
        _freeze_new_orders(executor,created)
        account['exit_states'] = exits.states
        account['entry_streak'],account['weak_streak'] = policy.entry_streak,policy.weak_streak
        row = executor.curve[-1]
        if abs(row['capital']-row['cash']-row['stocks']-row['dividend_receivable']) > 1e-6 or executor.cash < -1e-7:
            raise AssertionError('shadow accounting invariant failed')
    state['last_processed_date'] = date
    state['sessions'] += 1
    if check_checkpoint:
        update_checkpoint(state)
    return state


def entry_clusters(account):
    return len({f.date for f in account['executor'].fills if f.side=='buy' and f.status=='filled'})


def update_checkpoint(state):
    months = state['protocol']['evaluation_months']
    offset = state['evaluation_checkpoint']
    first = pd.Timestamp(str(state['first_forward_date']))
    current = pd.Timestamp(str(state['last_processed_date']))
    deadline = first+pd.DateOffset(months=months[offset])
    if current < deadline:
        state['evaluation_status'] = 'COLLECTING_NO_EFFICACY_DECISION'
        return
    sufficient = all(entry_clusters(a) >= state['protocol']['minimum_entry_date_clusters_per_arm']
                     for a in state['accounts'].values())
    if sufficient:
        state['evaluation_status'] = 'REVIEW_REQUIRED'
        state['evaluation_asof'] = state['last_processed_date']
    elif offset == len(months)-1:
        state['evaluation_status'] = 'INSUFFICIENT_OBSERVATIONS'
        state['evaluation_asof'] = state['last_processed_date']
    else:
        state['evaluation_checkpoint'] += 1
        state['evaluation_status'] = 'COLLECTING_NO_EFFICACY_DECISION'


def account_metrics(account,initial):
    executor = account['executor']
    frame = executor.curve_frame()
    capital = np.r_[initial,frame.capital.to_numpy()] if len(frame) else np.array([initial])
    stress = np.r_[initial,frame.liquidation_nav_3_limits.to_numpy()] if len(frame) else capital
    return dict(return_pct=float((capital[-1]/initial-1)*100),
        max_drawdown_pct=float((capital/np.maximum.accumulate(capital)-1).min()*100),
        stress_return_pct=float((stress[-1]/initial-1)*100),
        stress_max_drawdown_pct=float((stress/np.maximum.accumulate(stress)-1).min()*100),
        capital=float(capital[-1]),entry_date_clusters=entry_clusters(account),
        pending_orders=len(executor.orders),filled_buys=sum(f.status=='filled' and f.side=='buy' for f in executor.fills))


def summary(state):
    return dict(version=state['version'],status=state['evaluation_status'],
        registered_at=state['registered_at'],historical_cutoff=state['historical_cutoff'],
        first_forward_date=state['first_forward_date'],sessions=state['sessions'],
        last_processed_date=state['last_processed_date'],evaluation_asof=state['evaluation_asof'],
        accounts={k:account_metrics(a,state['protocol']['initial_cash_per_account']) for k,a in state['accounts'].items()},
        research_only=True,real_orders_allowed=False,paper_admitted=False,live_admitted=False)


def export_accounts(directory,state):
    for arm,account in state['accounts'].items():
        output = Path(directory)/arm; output.mkdir(parents=True,exist_ok=True)
        executor = account['executor']
        executor.curve_frame().to_csv(output/'daily_nav.csv',index=False)
        executor.fills_frame().to_csv(output/'fills.csv',index=False)
        json_write(output/'fills.json',[asdict(fill) for fill in executor.fills])
        for key,rows in (
                ('orders',[asdict(o) for o in executor.order_history]),
                ('pending_orders',[asdict(o) for o in executor.orders]),
                ('reservations',[asdict(r) for r in executor.reservation_history]),
                ('position_events',[asdict(e) for e in executor.position_events]),
                ('risk_decisions',account['risk_decisions']),('exit_reasons',account['exit_reasons'])):
            pd.DataFrame(rows).to_csv(output/(key+'.csv'),index=False)
    json_write(Path(directory)/'summary.json',summary(state))


def audit_replay(root):
    root=Path(root)
    paths,_=verify_chain(root)
    genesis=paths[0]
    state=load_pickle(genesis/'state.pkl.gz')
    model=load_pickle(genesis/'model.pkl.gz')
    panels = replay_panels(paths)
    next(panels)
    for directory in paths[1:]:
        panel=next(panels)
        scores=feature_scores(panel,state['source'],model,state['protocol']['minimum_candidate_rows'])
        saved=pd.read_csv(directory/'features_scores.csv.gz',dtype={'symbol':str})
        pd.testing.assert_frame_equal(scores,saved,check_dtype=False,rtol=1e-9,atol=1e-10)
        step_accounts(state,panel,scores)
        expected=json.loads((directory/'summary.json').read_text())
        if summary(state)!=expected:
            raise AssertionError('replay summary mismatch')
        for arm,account in state['accounts'].items():
            actual = json.loads(json.dumps([asdict(f) for f in account['executor'].fills],allow_nan=False))
            if actual != json.loads((directory/arm/'fills.json').read_text()):
                raise AssertionError('replay fill mismatch')
    return dict(status='AUDIT_PASSED',sessions=len(paths)-1,feature_predictions_replayed=True,
                financial_fills_replayed=True,summary=summary(state))
