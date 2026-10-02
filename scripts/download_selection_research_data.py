#!/usr/bin/env python3
"""Build a free point-in-time research dataset for A-share selection tests.

AKShare's current universe is augmented with exchange delisting lists.  Signal
prices stay adjusted, while a separate raw-price store is used for fills and
mark-to-market accounting.  CNINFO supplies corporate actions and historical
industry changes.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.MarketBu.ABuDataFeedAkShare import akshare_cn_stock_info  # noqa: E402


SH_A_PREFIXES = ('600', '601', '603', '605', '688', '689')
SZ_A_PREFIXES = ('000', '001', '002', '003', '300', '301')


def _date_number(series):
    return pd.to_datetime(series).dt.strftime('%Y%m%d').astype(int)


def _is_a_share(exchange, code):
    prefixes = SH_A_PREFIXES if exchange == 'sh' else SZ_A_PREFIXES
    return str(code).zfill(6).startswith(prefixes)


def build_security_master(output_dir, start_date):
    import akshare as ak

    current = akshare_cn_stock_info(refresh=True).copy()
    current['list_date'] = pd.to_datetime(current['list_date'], errors='coerce')
    current['delist_date'] = pd.NaT
    current['status'] = 'listed'

    sh = ak.stock_info_sh_delist(symbol='全部').rename(columns={
        '公司代码': 'code', '公司简称': 'name', '上市日期': 'list_date',
        '暂停上市日期': 'delist_date',
    })
    sh['exchange'] = 'sh'
    sz = ak.stock_info_sz_delist(symbol='终止上市公司').rename(columns={
        '证券代码': 'code', '证券简称': 'name', '上市日期': 'list_date',
        '终止上市日期': 'delist_date',
    })
    sz['exchange'] = 'sz'
    old = pd.concat([sh, sz], ignore_index=True)
    old['code'] = old['code'].astype(str).str.zfill(6)
    old = old[[
        _is_a_share(exchange, code)
        for exchange, code in zip(old.exchange, old.code)
    ]].copy()
    old['list_date'] = pd.to_datetime(old['list_date'], errors='coerce')
    old['delist_date'] = pd.to_datetime(old['delist_date'], errors='coerce')
    old = old[old.delist_date >= pd.Timestamp(start_date)].copy()
    old['status'] = 'delisted'
    old['symbol'] = old.exchange + old.code

    columns = ['code', 'name', 'list_date', 'delist_date', 'exchange', 'symbol', 'status']
    master = pd.concat([current[columns], old[columns]], ignore_index=True)
    master.sort_values(['symbol', 'status'], inplace=True)
    master.drop_duplicates('symbol', keep='first', inplace=True)
    master.sort_values('symbol', inplace=True, ignore_index=True)
    master['list_date'] = master.list_date.dt.strftime('%Y-%m-%d')
    master['delist_date'] = master.delist_date.dt.strftime('%Y-%m-%d')
    path = output_dir / 'security_master.csv'
    master.to_csv(path, index=False)
    return master


def _standardize_sina(frame):
    result = pd.DataFrame({
        'date': _date_number(frame['date']),
        'open': pd.to_numeric(frame['open'], errors='coerce'),
        'high': pd.to_numeric(frame['high'], errors='coerce'),
        'low': pd.to_numeric(frame['low'], errors='coerce'),
        'close': pd.to_numeric(frame['close'], errors='coerce'),
        'volume': pd.to_numeric(frame['volume'], errors='coerce'),
        'amount': pd.to_numeric(frame.get('amount'), errors='coerce'),
        'outstanding_share': pd.to_numeric(
            frame.get('outstanding_share'), errors='coerce'),
        'turnover': pd.to_numeric(frame.get('turnover'), errors='coerce'),
    })
    return result


def _standardize_tencent(frame):
    # Tencent calls its volume-in-lots column ``amount``.
    volume = pd.to_numeric(frame['amount'], errors='coerce') * 100.0
    return pd.DataFrame({
        'date': _date_number(frame['date']),
        'open': pd.to_numeric(frame['open'], errors='coerce'),
        'high': pd.to_numeric(frame['high'], errors='coerce'),
        'low': pd.to_numeric(frame['low'], errors='coerce'),
        'close': pd.to_numeric(frame['close'], errors='coerce'),
        'volume': volume,
        'amount': pd.NA,
        'outstanding_share': pd.NA,
        'turnover': pd.NA,
    })


def _download_prices(job):
    symbol, status, start_date, end_date, raw_dir, signal_extra_dir = job
    import akshare as ak

    raw_path = Path(raw_dir) / (symbol + '.csv')
    signal_path = Path(signal_extra_dir) / (symbol + '.csv')
    provider = 'cached'
    if not raw_path.exists():
        frame = None
        if status != 'delisted':
            try:
                frame = ak.stock_zh_a_daily(
                    symbol=symbol, start_date=start_date, end_date=end_date,
                    adjust='')
                if frame is not None and not frame.empty:
                    frame = _standardize_sina(frame)
                    provider = 'sina'
            except Exception:
                frame = None
        if frame is None or frame.empty:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                tx = ak.stock_zh_a_hist_tx(
                    symbol=symbol, start_date=start_date, end_date=end_date,
                    adjust='', timeout=30)
            frame = _standardize_tencent(tx)
            provider = 'tencent'
        if frame.empty:
            raise ValueError('no raw bars')
        frame.to_csv(raw_path, index=False)

    if status == 'delisted' and not signal_path.exists():
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            adjusted = ak.stock_zh_a_hist_tx(
                symbol=symbol, start_date=start_date, end_date=end_date,
                adjust='qfq', timeout=30)
        adjusted = _standardize_tencent(adjusted)
        if adjusted.empty:
            raise ValueError('no adjusted bars for delisted security')
        adjusted.to_csv(signal_path, index=False)
    return symbol, provider


def _metadata_one(symbol, start_date, end_date):
    import akshare as ak

    code = symbol[2:]
    actions = []
    industries = []
    errors = []
    try:
        frame = ak.stock_dividend_cninfo(symbol=code)
        if frame is not None and not frame.empty:
            frame = frame.copy()
            frame.insert(0, 'symbol', symbol)
            actions = frame.to_dict('records')
    except KeyError:
        # CNINFO's empty response has no schema.  Sina still retains many
        # delisted-company action histories, so use it as the fallback.
        try:
            frame = ak.stock_history_dividend_detail(
                symbol=code, indicator='分红')
            if frame is not None and not frame.empty:
                frame = frame.rename(columns={
                    '公告日期': '实施方案公告日期',
                    '送股': '送股比例', '转增': '转增比例',
                    '派息': '派息比例', '除权除息日': '除权日',
                    '红股上市日': '股份到账日',
                })
                frame['symbol'] = symbol
                frame['分红类型'] = '新浪历史分红'
                frame['派息日'] = frame.get('除权日')
                frame['实施方案分红说明'] = ''
                frame['报告时间'] = ''
                wanted = [
                    'symbol', '实施方案公告日期', '分红类型', '送股比例',
                    '转增比例', '派息比例', '股权登记日', '除权日',
                    '派息日', '股份到账日', '实施方案分红说明', '报告时间',
                ]
                actions = frame.reindex(columns=wanted).to_dict('records')
        except Exception as error:
            errors.append('action_fallback:' + type(error).__name__)
    except Exception as error:
        errors.append('action:' + type(error).__name__)
    try:
        frame = ak.stock_industry_change_cninfo(
            symbol=code, start_date=start_date, end_date=end_date)
        if frame is not None and not frame.empty:
            frame = frame.copy()
            frame.insert(0, 'symbol', symbol)
            industries = frame.to_dict('records')
    except KeyError:
        pass
    except Exception as error:
        errors.append('industry:' + type(error).__name__)
    return actions, industries, errors


def download_metadata(master, output_dir, start_date, end_date, workers):
    action_path = output_dir / 'corporate_actions.csv'
    industry_path = output_dir / 'industry_changes.csv'
    errors = {}
    actions = []
    industries = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_metadata_one, symbol, start_date, end_date): symbol
            for symbol in master.symbol
        }
        for position, future in enumerate(as_completed(futures), 1):
            symbol = futures[future]
            try:
                symbol_actions, symbol_industries, symbol_errors = future.result()
                actions.extend(symbol_actions)
                industries.extend(symbol_industries)
                if symbol_errors:
                    errors[symbol] = symbol_errors
            except Exception as error:
                errors[symbol] = [type(error).__name__]
            if position % 250 == 0 or position == len(futures):
                print('metadata {}/{} actions={} industries={} errors={}'.format(
                    position, len(futures), len(actions), len(industries), len(errors)),
                    flush=True)
    pd.DataFrame(actions).to_csv(action_path, index=False)
    pd.DataFrame(industries).to_csv(industry_path, index=False)
    return len(actions), len(industries), errors


def retry_metadata_errors(output_dir, start_date, end_date):
    manifest_path = output_dir / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    symbols = [symbol for symbol in manifest.get('metadata_errors', {})
               if symbol.startswith(('sh', 'sz'))]
    action_path = output_dir / 'corporate_actions.csv'
    industry_path = output_dir / 'industry_changes.csv'
    actions = pd.read_csv(action_path) if action_path.exists() else pd.DataFrame()
    industries = pd.read_csv(industry_path) if industry_path.exists() else pd.DataFrame()
    remaining = {}
    recovered_actions = []
    recovered_industries = []
    for position, symbol in enumerate(symbols, 1):
        symbol_actions, symbol_industries, errors = _metadata_one(
            symbol, start_date, end_date)
        recovered_actions.extend(symbol_actions)
        recovered_industries.extend(symbol_industries)
        if errors:
            remaining[symbol] = errors
        if position % 50 == 0 or position == len(symbols):
            print('metadata retry {}/{} remaining={}'.format(
                position, len(symbols), len(remaining)), flush=True)
        time.sleep(0.05)
    if recovered_actions:
        actions = pd.concat([actions, pd.DataFrame(recovered_actions)], ignore_index=True)
        actions.drop_duplicates(inplace=True)
        actions.to_csv(action_path, index=False)
    if recovered_industries:
        industries = pd.concat(
            [industries, pd.DataFrame(recovered_industries)], ignore_index=True)
        industries.drop_duplicates(inplace=True)
        industries.to_csv(industry_path, index=False)
    manifest['metadata_errors'] = remaining
    manifest['corporate_action_rows'] = len(actions)
    manifest['industry_change_rows'] = len(industries)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n')
    return len(remaining)


def audit_price_files(raw_dir, output_dir):
    quality = {
        'files': 0, 'rows': 0, 'duplicate_dates': 0,
        'unordered_files': 0, 'bad_tradable_ohlc_rows': 0,
        'suspended_zero_ohlc_rows': 0, 'empty_files': 0,
        'first_date': None, 'last_date': None,
    }
    for path in raw_dir.glob('*.csv'):
        frame = pd.read_csv(
            path, usecols=['date', 'open', 'high', 'low', 'close', 'volume'])
        quality['files'] += 1
        quality['rows'] += len(frame)
        if frame.empty:
            quality['empty_files'] += 1
            continue
        quality['duplicate_dates'] += int(frame.date.duplicated().sum())
        quality['unordered_files'] += int(not frame.date.is_monotonic_increasing)
        tradable = ((frame.volume > 0) & (frame.open > 0) &
                    (frame.high > 0) & (frame.low > 0))
        bad = ((frame.high < frame[['open', 'close', 'low']].max(axis=1)) |
               (frame.low > frame[['open', 'close', 'high']].min(axis=1)))
        quality['bad_tradable_ohlc_rows'] += int((tradable & bad).sum())
        quality['suspended_zero_ohlc_rows'] += int((~tradable & bad).sum())
        first, last = int(frame.date.min()), int(frame.date.max())
        quality['first_date'] = first if quality['first_date'] is None else min(
            quality['first_date'], first)
        quality['last_date'] = last if quality['last_date'] is None else max(
            quality['last_date'], last)
    (output_dir / 'quality_report.json').write_text(
        json.dumps(quality, ensure_ascii=False, indent=2) + '\n')
    return quality


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path(
        '/Users/wjy/abu/data/selection_research'))
    parser.add_argument('--start', default='20200101')
    parser.add_argument('--end', default='20261002')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--metadata-workers', type=int, default=6)
    parser.add_argument('--skip-metadata', action='store_true')
    parser.add_argument('--retry-metadata-errors', action='store_true')
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.retry_metadata_errors:
        return 1 if retry_metadata_errors(
            args.output_dir, '19900101', args.end) else 0
    raw_dir = args.output_dir / 'raw'
    signal_extra_dir = args.output_dir / 'signal_extra'
    raw_dir.mkdir(exist_ok=True)
    signal_extra_dir.mkdir(exist_ok=True)

    master = build_security_master(args.output_dir, args.start)
    print('security master: {} listed, {} delisted'.format(
        int((master.status == 'listed').sum()),
        int((master.status == 'delisted').sum())), flush=True)
    jobs = [
        (row.symbol, row.status, args.start, args.end,
         str(raw_dir), str(signal_extra_dir))
        for row in master.itertuples(index=False)
    ]
    providers = {}
    failed = {}
    started = time.time()
    with ProcessPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_download_prices, job): job[0] for job in jobs}
        for position, future in enumerate(as_completed(futures), 1):
            symbol = futures[future]
            try:
                _, provider = future.result()
                providers[provider] = providers.get(provider, 0) + 1
            except Exception as error:
                failed[symbol] = '{}: {}'.format(type(error).__name__, error)
            if position % 100 == 0 or position == len(futures):
                print('prices {}/{} failed={} elapsed={:.1f}s'.format(
                    position, len(futures), len(failed), time.time() - started), flush=True)

    action_rows = industry_rows = 0
    metadata_errors = {}
    if not args.skip_metadata:
        action_rows, industry_rows, metadata_errors = download_metadata(
            master, args.output_dir, '19900101', args.end,
            args.metadata_workers)

    try:
        import akshare as ak
        name_changes = ak.stock_info_sz_change_name(symbol='简称变更')
        name_changes.to_csv(args.output_dir / 'sz_name_changes.csv', index=False)
    except Exception as error:
        metadata_errors['sz_name_changes'] = [type(error).__name__]

    quality = audit_price_files(raw_dir, args.output_dir)

    manifest = {
        'created_at': datetime.now().astimezone().isoformat(),
        'start': args.start, 'end': args.end,
        'security_count': len(master),
        'listed_count': int((master.status == 'listed').sum()),
        'delisted_count': int((master.status == 'delisted').sum()),
        'price_providers': providers,
        'price_failures': failed,
        'corporate_action_rows': action_rows,
        'industry_change_rows': industry_rows,
        'metadata_errors': metadata_errors,
        'quality': quality,
        'limitations': [
            'Shanghai historical ST intervals are not complete.',
            'Tencent fallback does not provide traded amount or shares outstanding.',
            'Corporate action cash is reported gross; investor dividend tax is not modeled.',
        ],
    }
    (args.output_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + '\n')
    print('done; manifest={}'.format(args.output_dir / 'manifest.json'))
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
