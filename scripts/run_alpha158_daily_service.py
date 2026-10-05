#!/usr/bin/env python3
"""Operate the existing frozen Alpha research ledger and publish daily reports.

This sidecar never changes the registered strategy or its admission flags. Its
activation records the user's separate authorization for scheduled simulation
and notifications. No broker integration is present.
"""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime
import fcntl
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

PROJECT=Path('/Users/wjy/Documents/code/abu')
FORWARD=Path('/Users/wjy/abu/shadow/alpha158_forward_v1')
SERVICE=Path('/Users/wjy/abu/paper/alpha158_event_exit_only_daily')
ARM='event_exit_only'
REASONS={'INITIAL_STOP':'初始止损','TRAILING_STOP':'移动止损','STAGNATION':'停滞退出',
         'PERSISTENT_RANK_EXIT':'排名退出','SAME_DAY_NEW_RISK':'当日新增风险额度不足',
         'PORTFOLIO_OPEN_RISK':'组合风险额度不足','INDUSTRY_OPEN_RISK':'行业风险额度不足'}


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(8*1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name('.'+path.name+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n')
    os.replace(temporary,path)


def local_settings():
    values={}
    for line in (PROJECT/'.env.wecom').read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#') and '=' in line:
            key,value=line.split('=',1);values[key.strip()]=value.strip().strip('\"\'')
    return values


def split_message(content,limit=1700):
    chunks=[];current=''
    for char in content.strip():
        if len((current+char).encode('utf-8'))>limit:
            chunks.append(current);current=''
        current+=char
    if current:chunks.append(current)
    return [f'Alpha158 日报 {i+1}/{len(chunks)}\n'+chunk for i,chunk in enumerate(chunks)]


def queue_once(content,identifier,outbox,target):
    """Stable IDs survive retries and recognize the bot's sent acknowledgement."""
    outbox=Path(outbox);outbox.mkdir(parents=True,exist_ok=True)
    pending=outbox/(identifier+'.json')
    sent=outbox.parent/('sent-'+identifier+'.json')
    # Pending is checked first: the bot atomically moves pending -> sent.
    for path,status in ((pending,'queued'),(sent,'sent')):
        if path.exists():
            job=json.loads(path.read_text())
            if job['content']!=content or job.get('target')!=target:
                raise ValueError('notification ID already belongs to different content/recipient')
            return dict(id=identifier,status=status)
    temporary=outbox/('.'+identifier+'.tmp')
    job=dict(id=identifier,content=content,target=target,createdAt=time.time())
    temporary.write_text(json.dumps(job,ensure_ascii=False)+'\n')
    try:
        os.link(temporary,pending)
    except FileExistsError:
        return queue_once(content,identifier,outbox,target)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(id=identifier,status='queued')


def frozen_command(root,command):
    path=Path(root)/'runtime/scripts/run_alpha158_forward_shadow_v1.py'
    completed=subprocess.run([str(PROJECT/'.venv/bin/python'),'-B',str(path),command,'--root',str(root)],
        cwd=str(PROJECT),capture_output=True,text=True,timeout=5400)
    if completed.returncode:
        raise RuntimeError('frozen '+command+' failed: '+completed.stderr[-1600:])
    return json.loads(completed.stdout)


def is_trading_session(service,now):
    """Distinguish exchange holidays from stale provider data; never guess."""
    if now.weekday()>=5:return False
    path=Path(service)/'trading_calendar.json'
    saved=json.loads(path.read_text()) if path.exists() else {}
    today=int(now.strftime('%Y%m%d'))
    if not saved.get('dates') or max(saved['dates'])<today:
        command='import akshare as ak,json; print(json.dumps([int(x.strftime("%Y%m%d")) for x in ak.tool_trade_date_hist_sina().trade_date]))'
        result=subprocess.run([str(PROJECT/'.venv/bin/python'),'-c',command],capture_output=True,text=True,timeout=45,check=True)
        dates=json.loads(result.stdout)
        if not dates or max(dates)<today:raise ValueError('trading calendar does not cover today')
        saved=dict(source='akshare.tool_trade_date_hist_sina',captured_at=now.isoformat(),dates=dates)
        atomic_json(path,saved)
    return today in saved['dates']


def load_payload(root):
    """Read only this user's own hash-verified checkpoint through frozen code."""
    import pandas as pd
    root=Path(root)
    registration=json.loads((root/'genesis/registration.json').read_text())
    for name,digest in registration['runtime_files'].items():
        if sha(root/'runtime'/name)!=digest:raise ValueError('frozen runtime integrity failure')
    deps=dict(python=sys.version,packages={name:importlib.metadata.version(name) for name in
              ('numpy','pandas','scikit-learn','scipy','akshare')})
    if deps!=registration['dependencies']:raise ValueError('registered dependencies changed')
    sys.path.insert(0,str(root/'runtime'))
    from abupy.AlphaBu import ABuAlphaForwardShadow as shadow
    if Path(shadow.__file__).resolve()!=root/'runtime/abupy/AlphaBu/ABuAlphaForwardShadow.py':
        raise ValueError('report reader did not load frozen implementation')
    paths,digest=shadow.verify_chain(root)
    state=shadow.load_pickle(paths[-1]/'state.pkl.gz')
    summary=shadow.summary(state)
    account=state['accounts'][ARM];executor=account['executor']
    date=state['last_processed_date']
    names={};picks=[];spot={}
    if state['sessions']:
        raw=pd.read_csv(paths[-1]/'source_snapshot/stock_spot.csv',dtype={'symbol':str})
        names=dict(zip(raw.symbol,raw['名称']))
        spot=dict(zip(raw.symbol,raw.close))
        scores=pd.read_csv(paths[-1]/'features_scores.csv.gz',dtype={'symbol':str}).sort_values(['daily_rank','symbol'])
        if scores.signal_asof.astype(int).nunique()!=1 or int(scores.signal_asof.iloc[0])!=date:
            raise ValueError('stale selection scores')
        for row in scores.head(10).itertuples():
            picks.append(dict(symbol=row.symbol,name=str(names.get(row.symbol,'')),rank=int(row.daily_rank),score=float(row.alpha_score)))
    positions=[]
    for symbol,position in sorted(executor.positions.items()):
        positions.append(dict(symbol=symbol,name=str(names.get(symbol,'')),quantity=int(position.quantity),
            stop=float(position.initial_stop_raw or 0.)))
    curve=executor.curve_frame()
    order_reasons={o.order_id:o.reason for o in executor.order_history}
    return dict(summary=summary,commit_hash=digest,account=ARM,market_date=date,
        review_day=bool(state['sessions'] and (state['sessions']-1)%state['policy'].review_interval_sessions==0),
        review_interval=state['policy'].review_interval_sessions,entry_persistence=state['policy'].entry_persistence_reviews,
        picks=picks,positions=positions,names=names,cash=float(executor.cash),
        exposure_pct=float(curve.exposure.iloc[-1]*100) if len(curve) else 0.,
        fills=[dict(asdict(f),exit_reason=order_reasons.get(f.order_id,'')) for f in executor.fills if f.date==date],
        pending=[asdict(o) for o in executor.orders],
        decisions=[d for d in account['risk_decisions'] if int(d['signal_asof'])==date])


def report(payload):
    summary=payload['summary'];metrics=summary['accounts'][ARM]
    if not summary['sessions']:
        return ('## Alpha158 研究模拟盘已就绪\n\n'
            '当前方案：取消排名退出，保留初始止损、移动止损和停滞退出。\n'
            '初始资金：1,000,000 元；当前空仓，尚无前瞻成交。\n'
            '每个交易日 18:20（北京时间）更新，推送当日模拟成交、下一交易日计划和选股观察名单。\n'
            '等待首个真实交易日收盘；不把历史回测补记为模拟交易。\n'
            '每 5 个交易日评审一次，连续 2 次入围才考虑买入；因此启动后可能连续多日无订单。\n'
            '采用日线模拟撮合，次日开盘成交在次日日终核算和推送；不连接券商，不产生真实委托。\n'
            '行业过滤和等风险分配实验未启用。')
    names=payload['names']
    lines=['## Alpha158 研究模拟盘日报',f"行情日：{payload['market_date']}",
        '方案：取消排名退出；原风控与事件退出不变。',
        f"资产 ¥{metrics['capital']:,.2f}｜现金 ¥{payload['cash']:,.2f}",
        f"累计收益 {metrics['return_pct']:+.2f}%｜最大回撤 {abs(metrics['max_drawdown_pct']):.2f}%｜仓位 {payload['exposure_pct']:.2f}%",'',
        '**今日模拟成交 / 未成交**']
    if not payload['fills']:lines.append('无成交或拒单记录。')
    for fill in payload['fills']:
        direction='买入' if fill['side']=='buy' else '卖出'
        label=f"{direction} {fill['symbol']} {names.get(fill['symbol'],'')} {int(fill['quantity'])}股"
        if fill['status']=='filled':
            fees=sum(float(fill.get(k,0.) or 0.) for k in ('commission','transfer_fee','stamp_tax'))
            lines.append(f"{label}，成交价 ¥{fill['fill_price_raw']:.3f}，费用 ¥{fees:.2f}。")
            if fill['side']=='sell':
                lines.append('卖出原因：'+REASONS.get(fill.get('exit_reason',''),fill.get('exit_reason','事件退出'))+'。')
        else:lines.append(f"{label}，未成交：{fill['status']} / {fill.get('reason_code','')}。")
    lines+=['','**下一交易日待执行计划（尚未成交）**']
    if not payload['pending']:lines.append('无买卖计划，继续持有或保持现金。')
    for order in payload['pending']:
        symbol=order['symbol'];direction='买入' if order['side']=='buy' else '卖出'
        text=f"{direction} {symbol} {names.get(symbol,'')} {int(order['quantity'])}股"
        if order['side']=='buy':
            text+=f"；最高买价 ¥{order['max_buy_price_raw']:.3f}；初始止损 ¥{order['initial_stop_raw']:.3f}；连续入围且风控批准。"
        else:text+='；'+REASONS.get(order.get('reason',''),order.get('reason','事件退出'))+'。'
        lines.append(text)
    lines+=['','**选股观察 Top 10（不是买入委托）**']
    lines.append('今日为定期评审日。' if payload['review_day'] else '今日非定期入场评审日；保护性退出仍每日检查。')
    for pick in payload['picks']:
        lines.append(f"{pick['rank']}. {pick['symbol']} {pick['name']}，模型分 {pick['score']:.4f}")
    lines.append('仅观察排名；实际买入需连续评审入围、持仓名额与全部风控通过。')
    rejected=Counter(code for d in payload['decisions'] if d['decision']!='approved' for code in d['reason_codes'])
    if rejected:lines.append('本轮限制：'+'；'.join(REASONS.get(k,k)+f' {v}次' for k,v in sorted(rejected.items())))
    lines+=['','**当前持仓**']
    for position in payload['positions']:
        lines.append(f"{position['symbol']} {position['name']} {position['quantity']}股；初始止损参考 ¥{position['stop']:.3f}")
    if not payload['positions']:lines.append('空仓。')
    if summary.get('evaluation_asof'):
        lines.append('已到预登记评估节点，后续模拟暂停等待复核。')
    lines+=['','研究日线模拟：当日收盘生成计划，下一交易日开盘撮合结果于日终核算。未连接券商；回测收益不是模拟盘收益。']
    return '\n'.join(lines)


def activate(service,forward):
    if (service/'activation.json').exists():
        verify_service(service)
        return dict(status='ALREADY_ACTIVATED',service=str(service))
    payload=load_payload(forward)
    settings=local_settings()
    outbox=Path(settings.get('WECOM_RUNTIME_DIR',str(PROJECT/'runtime/wecom')))/'outbox'
    bot=json.loads((outbox.parent/'state.json').read_text())
    owner=bot.get('ownerUserId')
    if not owner:raise ValueError('WeCom bot has no bound recipient')
    service.mkdir(parents=True,exist_ok=False)
    (service/'code').mkdir()
    saved=service/'code'/Path(__file__).name
    shutil.copy2(__file__,saved)
    config=dict(version='alpha158_daily_service_v1',activated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        authorization='User requested daily selection/action notifications and operation of the current candidate in a simulated account.',
        forward_root=str(forward),arm=ARM,genesis_sha256=sha(forward/'genesis/commit.json'),
        code_sha256=sha(saved),outbox=str(outbox),recipient_sha256=hashlib.sha256(owner.encode()).hexdigest(),
        report_path=settings['WECOM_REPORT_FILE'],schedule_timezone='Asia/Shanghai',schedule_local_time='18:20',
        research_only=True,broker_connected=False,real_orders_allowed=False,
        strategy_admission_unchanged=True,initial_sessions=payload['summary']['sessions'])
    atomic_json(service/'activation.json',config)
    (service/'REPORT.md').write_text(report(payload)+'\n')
    return dict(status='ACTIVATED',service=str(service),summary=payload['summary'])


def verify_service(service):
    config=json.loads((service/'activation.json').read_text())
    if sha(service/'code'/Path(__file__).name)!=config['code_sha256']:
        raise ValueError('service runtime changed')
    if sha(Path(config['forward_root'])/'genesis/commit.json')!=config['genesis_sha256']:
        raise ValueError('registered account was replaced')
    if config['arm']!=ARM or config['real_orders_allowed'] or config['broker_connected']:
        raise ValueError('paper-only contract changed')
    owner=json.loads((Path(config['outbox']).parent/'state.json').read_text()).get('ownerUserId')
    if not owner or hashlib.sha256(owner.encode()).hexdigest()!=config['recipient_sha256']:
        raise ValueError('bound notification recipient changed')
    return config,owner


def publish(service,config,owner,content,event_id):
    records=[]
    for index,chunk in enumerate(split_message(content)):
        records.append(queue_once(chunk,f'alpha158-{event_id}-{index:02}',config['outbox'],owner))
    atomic_json(service/'notifications'/(event_id+'.json'),dict(event_id=event_id,messages=records))
    return records


def operate(service,command):
    config,owner=verify_service(service)
    root=Path(config['forward_root'])
    if command=='status':
        return dict(status='ACTIVE',summary=frozen_command(root,'status'),
            service=str(service),last_run=json.loads((service/'last_run.json').read_text()) if (service/'last_run.json').exists() else None)
    now=datetime.now(ZoneInfo('Asia/Shanghai'));today=int(now.strftime('%Y%m%d'))
    if command=='run':
        result=frozen_command(root,'run')
        summary=result.get('summary',result)
        if not summary.get('sessions') or int(summary.get('last_processed_date',0))!=today:
            status=result.get('status','WAITING_FOR_NEW_SESSION')
            if status=='NO_SAME_DAY_SNAPSHOT':
                if is_trading_session(service,now):
                    raise RuntimeError('trading session has no current close snapshot; account not advanced')
                status='NON_TRADING_DAY'
            waiting=status in ('WAITING_FOR_SESSION_CLOSE','NON_TRADING_DAY','ALREADY_COMMITTED')
            if not waiting and status not in ('WAITING_FOR_FIRST_FORWARD_SESSION','COLLECTING_NO_EFFICACY_DECISION'):
                raise RuntimeError('daily account stopped: '+status)
            value=dict(status=status,checked_at=now.isoformat(),last_processed_date=summary.get('last_processed_date'),queued=0)
            atomic_json(service/'last_run.json',value)
            return value
    payload=load_payload(root)
    content=report(payload)
    event_id='activation' if command=='announce' else str(payload['market_date'])
    if command=='run' and payload['market_date']!=today:
        raise ValueError('refusing to label old report as current')
    if command=='preview':
        (service/'PREVIEW.md').write_text(content+'\n')
        return dict(status='PREVIEW_ONLY',path=str(service/'PREVIEW.md'),sessions=payload['summary']['sessions'])
    if command=='announce' and payload['summary']['sessions']:
        raise ValueError('activation notice is only for the initial empty account')
    directory=service/'reports'/event_id;directory.mkdir(parents=True,exist_ok=True)
    path=directory/'REPORT.md'
    if path.exists() and path.read_text()!=content+'\n':
        raise ValueError('immutable daily report conflict')
    path.write_text(content+'\n')
    atomic_json(directory/'payload.json',payload)
    (service/'REPORT.md').write_text(content+'\n')
    target=Path(config['report_path']);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name('.'+target.name+'.alpha158.tmp');temporary.write_text(content+'\n');os.replace(temporary,target)
    messages=publish(service,config,owner,content,event_id)
    value=dict(status='REPORTED',market_date=payload['market_date'],sessions=payload['summary']['sessions'],
        checked_at=now.isoformat(),messages=messages,report=str(path),new_orders=len(payload['pending']),
        filled_today=sum(f['status']=='filled' for f in payload['fills']))
    atomic_json(service/'last_run.json',value)
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('activate','run','preview','announce','status'))
    parser.add_argument('--service-root',type=Path,default=SERVICE)
    parser.add_argument('--forward-root',type=Path,default=FORWARD)
    args=parser.parse_args();service=args.service_root.resolve()
    if args.command=='activate':
        value=activate(service,args.forward_root.resolve())
    else:
        config,_=verify_service(service)
        frozen=service/'code'/Path(__file__).name
        if Path(__file__).resolve()!=frozen:
            os.execv(sys.executable,[sys.executable,'-B',str(frozen),args.command,'--service-root',str(service)])
        with (service/'.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:value=operate(service,args.command)
            except Exception as error:
                content='## Alpha158 模拟盘运行异常\n本次未生成新的操作日报，请检查；不要手工补造交易。\n'+str(error)[:900]
                atomic_json(service/'last_error.json',dict(at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),error=str(error)))
                if args.command=='run':
                    try:
                        config,owner=verify_service(service)
                        key=datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d')+'-error-'+hashlib.sha256(content.encode()).hexdigest()[:8]
                        publish(service,config,owner,content,key)
                    except Exception:
                        # Preserve and re-raise the original service failure;
                        # notification delivery is best-effort in this path.
                        pass
                raise
    print(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False))


if __name__=='__main__':main()
