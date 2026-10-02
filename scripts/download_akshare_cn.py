#!/usr/bin/env python3
"""Download recent Shanghai and Shenzhen daily data into ABU's CSV cache."""
from __future__ import print_function

import argparse
import json
import os
import sys
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from abupy.CoreBu import ABuEnv  # noqa: E402
from abupy.MarketBu import ABuSymbolPd  # noqa: E402
from abupy.MarketBu.ABuDataFeedAkShare import (  # noqa: E402
    akshare_cn_symbols,
    use_akshare,
)


def parse_date(value):
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError as error:
        raise argparse.ArgumentTypeError('date must use YYYY-MM-DD') from error


def parse_args():
    today = date.today()
    parser = argparse.ArgumentParser(
        description='Download AKShare A-share daily bars into ABU CSV cache.'
    )
    parser.add_argument('--start', type=parse_date, default=today - timedelta(days=365))
    parser.add_argument('--end', type=parse_date, default=today)
    parser.add_argument('--adjust', choices=['none', 'qfq', 'hfq'], default='qfq')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--executor', choices=['process', 'thread'], default='process')
    parser.add_argument('--timeout', type=float, default=30)
    parser.add_argument('--retries', type=int, default=3)
    parser.add_argument(
        '--symbols',
        help='Comma-separated ABU symbols, for example sh600000,sz000001.',
    )
    parser.add_argument('--limit', type=int, help='Only download the first N symbols.')
    parser.add_argument('--no-index', action='store_true', help='Do not download four benchmark indices.')
    parser.add_argument('--refresh', action='store_true', help='Ignore matching local cache files.')
    parser.add_argument(
        '--refresh-universe', action='store_true', help='Refresh the cached exchange stock list.'
    )
    return parser.parse_args()


def download_one(symbol, start, end):
    frame = ABuSymbolPd.make_kl_df(symbol, start=start, end=end, show_progress=False)
    return symbol, 0 if frame is None else len(frame)


def configure_worker(adjust, timeout, retries, refresh):
    """Initialize ABU globals once in every worker process or thread pool."""
    use_akshare(adjust=adjust, timeout=timeout, retries=retries)
    ABuEnv.g_data_fetch_mode = (
        ABuEnv.EMarketDataFetchMode.E_DATA_FETCH_FORCE_NET
        if refresh
        else ABuEnv.EMarketDataFetchMode.E_DATA_FETCH_NORMAL
    )
    os.makedirs(ABuEnv.g_project_kl_df_data_csv, exist_ok=True)


def main():
    args = parse_args()
    if args.start > args.end:
        raise SystemExit('--start must be earlier than or equal to --end')
    if args.workers < 1:
        raise SystemExit('--workers must be at least 1')

    adjust = '' if args.adjust == 'none' else args.adjust
    configure_worker(adjust, args.timeout, args.retries, args.refresh)

    if args.symbols:
        symbols = [symbol.strip() for symbol in args.symbols.split(',') if symbol.strip()]
        if not args.no_index:
            symbols.extend(['sh000001', 'sh000300', 'sz399001', 'sz399006'])
    else:
        symbols = akshare_cn_symbols(
            include_index=not args.no_index, refresh=args.refresh_universe
        )
    symbols = list(dict.fromkeys(symbols))
    if args.limit is not None:
        symbols = symbols[:args.limit]

    start = args.start.isoformat()
    end = args.end.isoformat()
    print('AKShare download: {} symbols, {} to {}, adjust={}, cache={}'.format(
        len(symbols), start, end, args.adjust, ABuEnv.g_project_kl_df_data_csv
    ))

    succeeded = {}
    failed = []
    executor_class = ProcessPoolExecutor if args.executor == 'process' else ThreadPoolExecutor
    with executor_class(
        max_workers=args.workers,
        initializer=configure_worker,
        initargs=(adjust, args.timeout, args.retries, args.refresh),
    ) as executor:
        futures = {
            executor.submit(download_one, symbol, start, end): symbol
            for symbol in symbols
        }
        for position, future in enumerate(as_completed(futures), 1):
            symbol = futures[future]
            try:
                _, rows = future.result()
                if rows > 0:
                    succeeded[symbol] = rows
                else:
                    failed.append(symbol)
            except Exception as error:
                failed.append(symbol)
                print('failed {}: {}'.format(symbol, error), file=sys.stderr)
            if position == len(symbols) or position % 25 == 0:
                print('progress {}/{}: succeeded={}, failed={}'.format(
                    position, len(symbols), len(succeeded), len(failed)
                ))

    print('completed: succeeded={}, failed={}'.format(len(succeeded), len(failed)))
    manifest = {
        'created_at': datetime.now().astimezone().isoformat(),
        'provider': 'AKShare',
        'provider_fallback_order': ['eastmoney', 'tencent', 'sina'],
        'adjust': args.adjust,
        'adjust_exceptions': {
            'sh689009': 'AKShare CDR endpoint does not expose an adjustment option'
        },
        'volume_unit': 'share',
        'requested_start': start,
        'requested_end': end,
        'symbols_requested': len(symbols),
        'symbols_succeeded': len(succeeded),
        'symbols_failed': failed,
        'cache_directory': ABuEnv.g_project_kl_df_data_csv,
        'beijing_exchange_included': False,
    }
    manifest_path = Path(ABuEnv.g_project_cache_dir) / 'akshare_download_manifest.json'
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    print('manifest written to {}'.format(manifest_path))
    if failed:
        failed_path = Path(ABuEnv.g_project_data_dir) / 'akshare_failed_symbols.txt'
        failed_path.write_text('\n'.join(failed) + '\n', encoding='utf-8')
        print('failed symbols written to {}'.format(failed_path), file=sys.stderr)
        return 1
    failed_path = Path(ABuEnv.g_project_data_dir) / 'akshare_failed_symbols.txt'
    if failed_path.exists():
        failed_path.unlink()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
