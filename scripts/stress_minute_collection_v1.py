#!/usr/bin/env python3
"""Bounded live read benchmark of the frozen collector, using an isolated store.

This measures historical retrieval throughput, not live bar-arrival latency.
No production configuration, account, snapshot, or scheduler is modified.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import signal
import sys
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


class StopBenchmark(BaseException):
    pass


class SymbolDeadline(BaseException):
    pass


def summary(values):
    values = sorted(values)
    if not values:
        return {}
    def quantile(p):
        index = (len(values) - 1) * p
        lo = int(index)
        return values[lo] + (values[min(lo + 1, len(values) - 1)] - values[lo]) * (index - lo)
    return {"count": len(values), "median": quantile(.5),
            "p95": quantile(.95), "max": max(values)}


def bytes_under(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())


def sample_symbols(path, size):
    import csv
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    listed = sorted({r['symbol'] for r in rows if r['status'] == 'listed'})
    result = ['sh600519', 'sz000001', 'sz300750', 'sh510300', 'sz002241']
    groups = [[s for s in listed if s.startswith(prefix) and s not in result]
              for prefix in ('sh60', 'sh68', 'sz00', 'sz30')]
    rng = random.Random(20261009)
    for group in groups:
        rng.shuffle(group)
    while len(result) < size and any(groups):
        for group in groups:
            if group and len(result) < size:
                result.append(group.pop())
    if len(result) < size:
        raise ValueError('insufficient symbols in frozen universe')
    return result[:size], len(listed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--universe', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--session', type=int, required=True)
    parser.add_argument('--sizes', default='4,20,60')
    parser.add_argument('--http-rps', type=float, default=2.0)
    parser.add_argument('--max-seconds', type=float, default=480)
    args = parser.parse_args()
    sizes = [int(x) for x in args.sizes.split(',')]
    if not sizes or min(sizes) < 1 or max(sizes) > 100 or not 0 < args.http_rps <= 2:
        parser.error('live test limited to 100 symbols per stage and 2 HTTP requests/second')
    args.output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(args.runtime_root.resolve()))
    import requests
    from abupy.MarketBu.ABuRealtimeMarket import AKShareRealtimeMarketData
    from abupy.MarketBu.ABuMinuteBarStore import MinuteBarStore
    from abupy.ServiceBu.ABuMinuteMarketHub import MinuteCollector
    from abupy.ServiceBu.ABuDailyDataCenter import ProviderRateLimiter
    module_path = Path(sys.modules[AKShareRealtimeMarketData.__module__].__file__).resolve()
    if args.runtime_root.resolve() not in module_path.parents:
        raise ValueError('collector did not load from selected frozen runtime')

    symbols, universe_count = sample_symbols(args.universe, max(sizes))
    config = json.loads((args.runtime_root / 'configs/service/minute_shadow_v1.json').read_text())
    date = datetime.strptime(str(args.session), '%Y%m%d').strftime('%Y-%m-%d')
    plan = {'schema_version': 'minute_stress_v1', 'created_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'mode': 'HISTORICAL_RETRIEVAL_NOT_LIVE_ARRIVAL', 'runtime_root': str(args.runtime_root),
            'universe_sha256': hashlib.sha256(args.universe.read_bytes()).hexdigest(),
            'listed_universe_count': universe_count, 'universe_scope': 'frozen_SH_SZ_excludes_BJ',
            'symbols': symbols, 'sizes': sizes, 'session': args.session,
            'http_rps_cap': args.http_rps, 'request_timeout_seconds': 15,
            'per_symbol_hard_deadline_seconds': 45, 'production_config': config}
    (args.output / 'plan.json').write_text(json.dumps(plan, indent=2) + '\n')
    http = []
    limiter = ProviderRateLimiter(args.http_rps)
    original_request = requests.sessions.Session.request
    started = time.monotonic()

    def bounded_request(session, method, url, **kwargs):
        if time.monotonic() - started > args.max_seconds:
            raise StopBenchmark('TOTAL_TIME_BUDGET')
        limiter.wait()
        kwargs['timeout'] = 15
        before = time.monotonic()
        item = {'host': urlsplit(url).hostname}
        try:
            response = original_request(session, method, url, **kwargs)
            item['status_code'] = response.status_code
            item['response_bytes'] = len(response.content)
            if response.status_code in (403, 429):
                raise StopBenchmark('PROVIDER_ACCESS_OR_RATE_LIMIT')
            return response
        except Exception as error:
            item['error_type'] = type(error).__name__
            raise
        finally:
            item['seconds'] = time.monotonic() - before
            http.append(item)

    requests.sessions.Session.request = bounded_request
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(SymbolDeadline()))
    stages = []
    stop_reason = None
    try:
        # Import and initialize the AKShare module outside timed stage measurements.
        warmup = AKShareRealtimeMarketData()
        warmup.ak
        for size in sizes:
            folder = args.output / f'stage_{size}'
            store = MinuteBarStore(folder / 'minute_store')
            adapter = AKShareRealtimeMarketData(raw_archive=store.append_raw_response,
                retries=config['provider_retries'], retry_wait=config['provider_retry_wait_seconds'])
            collector = MinuteCollector(adapter, store, batch_size=config['batch_size'],
                request_timeout_seconds=config['request_timeout_seconds'],
                rate_limiter=ProviderRateLimiter(config['provider_requests_per_second']).wait)
            records = []
            stage_start = time.monotonic()
            http_start = len(http)
            for symbol in symbols[:size]:
                if time.monotonic() - started > args.max_seconds:
                    stop_reason = 'TOTAL_TIME_BUDGET'
                    break
                before = time.monotonic()
                signal.setitimer(signal.ITIMER_REAL, 45)
                try:
                    result = collector.collect([symbol], args.session, date + ' 15:01:00')[symbol]
                except SymbolDeadline:
                    result = {'terminal_status': 'PROVIDER_ERROR', 'error': 'HARD_DEADLINE'}
                except StopBenchmark as error:
                    stop_reason = str(error)
                    result = {'terminal_status': 'PROVIDER_ERROR', 'error': stop_reason}
                finally:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                result = dict(result, symbol=symbol, total_seconds=time.monotonic()-before)
                if result['terminal_status'] in ('AVAILABLE', 'STALE'):
                    bars = store.read(symbol, args.session, source=result['source'])
                    result.update(bar_count=len(bars), first_bar=bars[0].bar_end,
                                  last_bar=bars[-1].bar_end,
                                  amount_missing_count=sum(b.amount_raw is None for b in bars))
                    expected = {f'{h:02}:{m:02}' for h in range(9, 16) for m in range(60)
                                if '09:31' <= f'{h:02}:{m:02}' <= '11:30'
                                or '13:01' <= f'{h:02}:{m:02}' <= '14:57'
                                or f'{h:02}:{m:02}' == '15:00'}
                    actual = {b.bar_end[11:16] for b in bars}
                    result['missing_expected_minutes'] = sorted(expected - actual)
                records.append(result)
                folder.mkdir(parents=True, exist_ok=True)
                with (folder / 'symbols.jsonl').open('a') as stream:
                    stream.write(json.dumps(result, ensure_ascii=False) + '\n')
                print(json.dumps({'stage': size, 'done': len(records), 'symbol': symbol,
                                  'status': result['terminal_status'],
                                  'seconds': round(result['total_seconds'], 3)}), flush=True)
                failures = sum(x['terminal_status'] not in ('AVAILABLE', 'STALE') for x in records)
                if len(records) >= 10 and failures / len(records) > .2:
                    stop_reason = 'FAILURE_RATE_ABOVE_20_PERCENT'
                if stop_reason:
                    break
            elapsed = time.monotonic() - stage_start
            successes = [x for x in records if x['terminal_status'] in ('AVAILABLE', 'STALE')]
            byte_count = bytes_under(folder / 'minute_store')
            stage = {'requested_symbols': size, 'attempted_symbols': len(records),
                     'archived_symbols': len(successes), 'elapsed_seconds': elapsed,
                     'one_minute_cycle_feasible': len(successes) == size and elapsed <= 60,
                     'status_counts': dict(Counter(x['terminal_status'] for x in records)),
                     'provider_counts': dict(Counter(x.get('source', 'none') for x in successes)),
                     'complete_session_symbols': sum(not x['missing_expected_minutes'] for x in successes),
                     'symbol_seconds': summary([x['total_seconds'] for x in records]),
                     'http_requests': len(http)-http_start,
                     'store_bytes': byte_count,
                     'linear_full_universe_hours': elapsed/max(1,len(records))*universe_count/3600,
                     'linear_full_universe_store_gib_one_capture': byte_count/max(1,len(records))*universe_count/1024**3,
                     'stop_reason': stop_reason}
            stages.append(stage)
            (folder / 'summary.json').write_text(json.dumps(stage, indent=2) + '\n')
            print('STAGE ' + json.dumps(stage), flush=True)
            if stop_reason:
                break
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        requests.sessions.Session.request = original_request
        result = {'plan': plan, 'stages': stages, 'stop_reason': stop_reason,
                  'http_status_counts': dict(Counter(str(x.get('status_code', x.get('error_type'))) for x in http)),
                  'http_seconds': summary([x['seconds'] for x in http]),
                  'elapsed_seconds': time.monotonic()-started}
        (args.output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
        (args.output / 'http.json').write_text(json.dumps(http, indent=2) + '\n')


if __name__ == '__main__':
    main()
