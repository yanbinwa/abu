# coding=utf-8
"""AKShare A-share daily market data adapter.

The adapter deliberately imports AKShare lazily.  Importing :mod:`abupy` should
therefore continue to work when the optional data dependency is not installed.
"""
from __future__ import absolute_import, division, print_function

import logging
import os
import io
import threading
import time
from contextlib import redirect_stderr, redirect_stdout
from functools import lru_cache

import numpy as np
import pandas as pd
import requests

from ..CoreBu import ABuEnv
from ..CoreBu.ABuEnv import EMarketTargetType
from ..UtilBu import ABuDateUtil
from .ABuDataBase import StockBaseMarket, SupportMixin


# AKShare's Sina adjustment path initializes an embedded JavaScript runtime
# which is not safe to initialize from multiple macOS threads.
_SINA_REQUEST_LOCK = threading.Lock()


def _load_akshare():
    """Import AKShare with an actionable error message."""
    try:
        import akshare as ak
    except ImportError as error:
        raise ImportError(
            'AKShare is required for AKShareCNApi; install project requirements first'
        ) from error
    return ak


def _request_with_retry(func, retries, retry_wait):
    """Run one AKShare request with bounded exponential backoff."""
    last_error = None
    for attempt in range(retries):
        try:
            return func()
        except Exception as error:  # Upstream endpoints raise several exception types.
            last_error = error
            if attempt + 1 < retries:
                time.sleep(retry_wait * (2 ** attempt))
    raise last_error


@lru_cache(maxsize=2)
def akshare_cn_stock_info(refresh=False):
    """Return the current Shanghai and Shenzhen A-share universe.

    Shanghai main-board and STAR-market lists are obtained from the exchange
    endpoints exposed by AKShare.  Shenzhen's A-share list already contains its
    main board, ChiNext and other A-share boards.  Beijing stocks are excluded
    because this version of ABU has no Beijing exchange market subtype.
    """
    cache_path = os.path.join(ABuEnv.g_project_cache_dir, 'akshare_cn_stock_info.csv')
    if not refresh and os.path.exists(cache_path):
        return pd.read_csv(cache_path, dtype={'code': str, 'symbol': str})

    ak = _load_akshare()
    sh_main = _request_with_retry(
        lambda: ak.stock_info_sh_name_code(symbol='主板A股'), 3, 1.0
    )
    sh_star = _request_with_retry(
        lambda: ak.stock_info_sh_name_code(symbol='科创板'), 3, 1.0
    )
    sz = _request_with_retry(
        lambda: ak.stock_info_sz_name_code(symbol='A股列表'), 3, 1.0
    )

    sh = pd.concat([sh_main, sh_star], ignore_index=True)
    sh = sh.rename(columns={
        '证券代码': 'code', '证券简称': 'name', '上市日期': 'list_date'
    })[['code', 'name', 'list_date']]
    sh['exchange'] = 'sh'

    sz = sz.rename(columns={
        'A股代码': 'code', 'A股简称': 'name', 'A股上市日期': 'list_date'
    })[['code', 'name', 'list_date']]
    sz['exchange'] = 'sz'

    stock_info = pd.concat([sh, sz], ignore_index=True)
    stock_info['code'] = stock_info['code'].astype(str).str.zfill(6)
    stock_info = stock_info[stock_info['code'].str.match(r'^\d{6}$')]
    stock_info['symbol'] = stock_info['exchange'] + stock_info['code']
    stock_info.drop_duplicates(subset=['symbol'], keep='last', inplace=True)
    stock_info.sort_values(['exchange', 'code'], inplace=True)
    stock_info.reset_index(drop=True, inplace=True)
    cache_dir = os.path.dirname(cache_path)
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir)
    stock_info.to_csv(cache_path, index=False, encoding='utf-8')
    return stock_info


def akshare_cn_symbols(include_index=False, refresh=False):
    """Return current ABU-formatted Shanghai and Shenzhen stock symbols."""
    symbols = akshare_cn_stock_info(refresh=refresh)['symbol'].tolist()
    if include_index:
        symbols.extend(['sh000001', 'sh000300', 'sz399001', 'sz399006'])
    return symbols


class AKShareCNApi(StockBaseMarket, SupportMixin):
    """AKShare daily K-line source for Shanghai and Shenzhen A shares."""

    adjust = 'qfq'
    timeout = 30
    retries = 3
    retry_wait = 1.0
    _primary_enabled = True
    _primary_state_lock = threading.Lock()

    @classmethod
    def configure(cls, adjust='qfq', timeout=30, retries=3, retry_wait=1.0):
        """Configure requests before registering this class as the private source."""
        if adjust not in ('', 'qfq', 'hfq'):
            raise ValueError("adjust must be one of '', 'qfq' or 'hfq'")
        if retries < 1:
            raise ValueError('retries must be at least 1')
        cls.adjust = adjust
        cls.timeout = timeout
        cls.retries = retries
        cls.retry_wait = retry_wait
        cls._primary_enabled = True

    def _support_market(self):
        return [EMarketTargetType.E_MARKET_TARGET_CN]

    @classmethod
    def all_symbols(cls, index=False):
        """Return the cached current universe for ABU full-market workflows."""
        return akshare_cn_symbols(include_index=index)

    @staticmethod
    def _request_dates(n_folds, start, end):
        query_end = ABuDateUtil.fix_date(end) if end else ABuDateUtil.current_str_date()
        query_start = ABuDateUtil.fix_date(start) if start else ABuDateUtil.begin_date(
            365 * n_folds, date_str=query_end
        )
        return query_start.replace('-', ''), query_end.replace('-', '')

    def _request_primary_kline(self, start_date, end_date):
        ak = _load_akshare()
        if self._symbol.is_a_index():
            return ak.stock_zh_index_daily_em(
                symbol=self._symbol.value,
                start_date=start_date,
                end_date=end_date,
            ), 1
        return ak.stock_zh_a_hist(
            symbol=self._symbol.symbol_code,
            period='daily',
            start_date=start_date,
            end_date=end_date,
            adjust=self.adjust,
            timeout=self.timeout,
        ), 100

    def _request_fallback_kline(self, start_date, end_date):
        """Use AKShare's Sina-backed endpoints when Eastmoney is unavailable."""
        ak = _load_akshare()
        with _SINA_REQUEST_LOCK:
            # These AKShare functions do not expose a timeout argument.  Patch
            # requests.get only for the duration of this serialized call so a
            # stalled upstream response cannot occupy a worker indefinitely.
            original_get = requests.get

            def get_with_timeout(*args, **kwargs):
                kwargs.setdefault('timeout', self.timeout)
                return original_get(*args, **kwargs)

            requests.get = get_with_timeout
            try:
                if self._symbol.is_a_index():
                    return ak.stock_zh_index_daily(symbol=self._symbol.value), 1
                return ak.stock_zh_a_daily(
                    symbol=self._symbol.value,
                    start_date=start_date,
                    end_date=end_date,
                    adjust=self.adjust,
                ), 1
            finally:
                requests.get = original_get

    def _request_tencent_kline(self, start_date, end_date):
        """Use AKShare's Tencent endpoint with an explicit request timeout."""
        ak = _load_akshare()
        with _SINA_REQUEST_LOCK:
            # AKShare's initial Tencent metadata request does not expose its
            # timeout argument, so apply the same scoped requests protection.
            original_get = requests.get

            def get_with_timeout(*args, **kwargs):
                kwargs.setdefault('timeout', self.timeout)
                return original_get(*args, **kwargs)

            requests.get = get_with_timeout
            try:
                # AKShare displays a per-year progress bar for this endpoint.
                # The downloader already reports aggregate progress.
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    frame = ak.stock_zh_a_hist_tx(
                        symbol=self._symbol.value,
                        start_date=start_date,
                        end_date=end_date,
                        adjust=self.adjust,
                        timeout=self.timeout,
                    )
            finally:
                requests.get = original_get
        # AKShare calls Tencent's sixth K-line field ``amount`` although it is
        # traded volume in lots.  Normalize it before the generic parser.
        frame = frame.rename(columns={'amount': 'volume'})
        return frame, 100

    def _request_cdr_kline(self, start_date, end_date):
        """Use AKShare's dedicated endpoint for the Shanghai CDR instrument."""
        ak = _load_akshare()
        return ak.stock_zh_a_cdr_daily(
            symbol=self._symbol.value,
            start_date=start_date,
            end_date=end_date,
        ), 1

    def kline(self, n_folds=2, start=None, end=None):
        """Return one normalized daily K-line DataFrame."""
        start_date, end_date = self._request_dates(n_folds, start, end)
        if self._symbol.value == 'sh689009':
            try:
                raw, volume_multiplier = _request_with_retry(
                    lambda: self._request_cdr_kline(start_date, end_date),
                    self.retries,
                    self.retry_wait,
                )
                kline_df = self._normalize_kline(raw, volume_multiplier=volume_multiplier)
                return StockBaseMarket._fix_kline_pd(kline_df, n_folds, start, end)
            except Exception as cdr_error:
                logging.error('AKShare CDR request failed for sh689009: %s', cdr_error)
                return None

        primary_error = None
        if self._primary_enabled:
            try:
                raw, volume_multiplier = _request_with_retry(
                    lambda: self._request_primary_kline(start_date, end_date),
                    self.retries,
                    self.retry_wait,
                )
            except Exception as error:
                primary_error = error
                with self._primary_state_lock:
                    type(self)._primary_enabled = False

        if primary_error is not None:
            logging.warning(
                'AKShare primary request failed for %s (%s); '
                'using fallback for the rest of this process',
                self._symbol.value,
                primary_error,
            )
        if not self._primary_enabled or primary_error is not None:
            try:
                raw, volume_multiplier = _request_with_retry(
                    lambda: self._request_tencent_kline(start_date, end_date),
                    self.retries,
                    self.retry_wait,
                )
            except Exception as tencent_error:
                logging.warning(
                    'AKShare Tencent request failed for %s (%s); using Sina fallback',
                    self._symbol.value,
                    tencent_error,
                )
                try:
                    raw, volume_multiplier = _request_with_retry(
                        lambda: self._request_fallback_kline(start_date, end_date),
                        self.retries,
                        self.retry_wait,
                    )
                except Exception as fallback_error:
                    logging.error(
                        'AKShare request failed for %s between %s and %s: %s',
                        self._symbol.value,
                        start_date,
                        end_date,
                        fallback_error,
                    )
                    return None

        kline_df = self._normalize_kline(raw, volume_multiplier=volume_multiplier)
        return StockBaseMarket._fix_kline_pd(kline_df, n_folds, start, end)

    def minute(self, *args, **kwargs):
        """Minute bars are outside the initial daily-backtest integration."""
        raise NotImplementedError('AKShareCNApi currently supports daily K-lines only')

    def _normalize_kline(self, raw, volume_multiplier=1):
        if raw is None or raw.empty:
            return None

        rename = {
            '日期': 'date_time',
            'date': 'date_time',
            '开盘': 'open',
            'open': 'open',
            '收盘': 'close',
            'close': 'close',
            '最高': 'high',
            'high': 'high',
            '最低': 'low',
            'low': 'low',
            '成交量': 'volume',
            'volume': 'volume',
            '成交额': 'amount',
            'amount': 'amount',
            '涨跌幅': 'p_change',
            '涨跌额': 'change',
            '振幅': 'amplitude',
            '换手率': 'turnover',
        }
        frame = raw.rename(columns=rename).copy()
        required = ['date_time', 'open', 'close', 'high', 'low', 'volume']
        missing = [column for column in required if column not in frame.columns]
        if missing:
            raise ValueError('AKShare response missing columns: {}'.format(', '.join(missing)))

        frame['date_time'] = pd.to_datetime(frame['date_time'], errors='coerce')
        numeric = ['open', 'close', 'high', 'low', 'volume', 'amount',
                   'p_change', 'change', 'amplitude', 'turnover']
        for column in numeric:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors='coerce')
        frame.dropna(subset=['date_time', 'open', 'close', 'high', 'low'], inplace=True)
        frame.sort_values('date_time', inplace=True)
        frame.drop_duplicates(subset=['date_time'], keep='last', inplace=True)
        frame.set_index('date_time', drop=True, inplace=True)

        # Eastmoney's stock endpoint uses lots (100 shares); the index endpoint
        # and Sina fallback use shares.  Persist one consistent unit: shares.
        frame['volume'] = (frame['volume'].fillna(0) * volume_multiplier).astype(np.int64)
        previous_close = frame['close'].shift(1)
        if 'p_change' not in frame.columns:
            frame['p_change'] = frame['close'].pct_change().mul(100)
        frame['p_change'] = frame['p_change'].fillna(0).round(3)

        first_implied_close = frame['close'] / (1.0 + frame['p_change'] / 100.0)
        frame['pre_close'] = previous_close.fillna(first_implied_close).fillna(frame['open'])
        frame['date'] = frame.index.strftime('%Y%m%d').astype(int)
        frame['date_week'] = frame.index.weekday
        frame['key'] = np.arange(len(frame), dtype=np.int64)

        core = ['open', 'high', 'low', 'close', 'pre_close', 'volume',
                'p_change', 'date', 'date_week', 'key']
        optional = [column for column in ['amount', 'change', 'amplitude', 'turnover']
                    if column in frame.columns]
        frame = frame[core + optional]
        frame.name = self._symbol.value
        return frame


def use_akshare(adjust='qfq', timeout=30, retries=3, retry_wait=1.0):
    """Configure and register AKShare as ABU's private A-share data source."""
    AKShareCNApi.configure(
        adjust=adjust,
        timeout=timeout,
        retries=retries,
        retry_wait=retry_wait,
    )
    ABuEnv.g_private_data_source = AKShareCNApi
    ABuEnv.g_market_target = EMarketTargetType.E_MARKET_TARGET_CN
    return AKShareCNApi
