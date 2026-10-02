# -*- encoding: utf-8 -*-
"""Close-to-next-open selection rules and a raw-price long-only ledger.

Signals use adjusted prices available at the close of day t.  Orders and
portfolio valuation use a separate unadjusted price panel.  Optional corporate
actions credit cash and shares from holdings recorded on the record date.
"""

from __future__ import division

from pathlib import Path

import numpy as np
import pandas as pd


STRATEGIES = ('residual_momentum', 'trend_reversal', 'trend_breakout')
INITIAL_CASH = 1_000_000.0
MAX_GROSS = 0.80
MAX_SYMBOL = 0.08
BROKER_RATE = 0.00025
TRANSFER_RATE = 0.00001
SELL_STAMP_RATE = 0.0005


def _rolling(values, window, operation):
    frame = pd.DataFrame(values)
    result = getattr(frame.rolling(window, min_periods=window), operation)()
    return result.to_numpy(dtype=np.float32)


class SelectionPanel(object):
    """Trading-calendar-aligned data; absent stock bars remain NaN."""

    def __init__(self, dates, symbols, opening, high, low, close, volume,
                 benchmark_close, benchmark_open=None, execution=None,
                 amount=None, market_cap=None, industry=None,
                 corporate_actions=None, st_status=None):
        self.dates = np.asarray(dates, dtype=np.int32)
        self.symbols = tuple(symbols)
        self.open = np.asarray(opening, dtype=np.float32)
        self.high = np.asarray(high, dtype=np.float32)
        self.low = np.asarray(low, dtype=np.float32)
        self.close = np.asarray(close, dtype=np.float32)
        self.volume = np.asarray(volume, dtype=np.float32)
        self.benchmark_close = np.asarray(benchmark_close, dtype=np.float64)
        self.benchmark_open = np.asarray(
            benchmark_close if benchmark_open is None else benchmark_open,
            dtype=np.float64)
        expected = (len(self.dates), len(self.symbols))
        for name in ('open', 'high', 'low', 'close', 'volume'):
            if getattr(self, name).shape != expected:
                raise ValueError('{} has wrong shape'.format(name))
        if len(self.benchmark_close) != len(self.dates) or \
                len(self.benchmark_open) != len(self.dates):
            raise ValueError('benchmark has wrong length')

        execution = execution or {}
        self.exec_open = np.asarray(execution.get('open', self.open), dtype=np.float32)
        self.exec_high = np.asarray(execution.get('high', self.high), dtype=np.float32)
        self.exec_low = np.asarray(execution.get('low', self.low), dtype=np.float32)
        self.exec_close = np.asarray(execution.get('close', self.close), dtype=np.float32)
        self.exec_volume = np.asarray(execution.get('volume', self.volume), dtype=np.float32)
        for name in ('exec_open', 'exec_high', 'exec_low', 'exec_close', 'exec_volume'):
            if getattr(self, name).shape != expected:
                raise ValueError('{} has wrong shape'.format(name))
        self.amount = (np.full(expected, np.nan, dtype=np.float32) if amount is None
                       else np.asarray(amount, dtype=np.float32))
        self.market_cap = (np.full(expected, np.nan, dtype=np.float64)
                           if market_cap is None else np.asarray(market_cap, dtype=np.float64))
        self.industry = (np.full(expected, -1, dtype=np.int16) if industry is None
                         else np.asarray(industry, dtype=np.int16))
        self.st_status = (np.zeros(expected, dtype=bool) if st_status is None
                          else np.asarray(st_status, dtype=bool))
        self.corporate_actions = corporate_actions or {}
        self.uses_raw_execution = execution is not None

        with np.errstate(divide='ignore', invalid='ignore'):
            self.returns = self.close / np.roll(self.close, 1, axis=0) - 1
            self.benchmark_returns = self.benchmark_close / np.roll(
                self.benchmark_close, 1) - 1
        self.returns[0] = np.nan
        self.benchmark_returns[0] = np.nan
        self.returns[~np.isfinite(self.returns)] = np.nan
        self.returns[np.abs(self.returns) > 0.50] = np.nan

        self.ma10 = _rolling(self.close, 10, 'mean')
        self.ma60 = _rolling(self.close, 60, 'mean')
        self.ma120 = _rolling(self.close, 120, 'mean')
        self.market_ma120 = pd.Series(self.benchmark_close).rolling(
            120, min_periods=120).mean().to_numpy()
        self.market_ma200 = pd.Series(self.benchmark_close).rolling(
            200, min_periods=200).mean().to_numpy()
        previous = np.roll(self.close, 1, axis=0)
        previous[0] = np.nan
        true_range = np.maximum.reduce((self.high - self.low,
                                        np.abs(self.high - previous),
                                        np.abs(self.low - previous)))
        self.atr21 = _rolling(true_range, 21, 'mean')
        self.prior60_high = _rolling(self.high, 60, 'max')
        self.prior60_high = np.roll(self.prior60_high, 1, axis=0)
        self.prior60_high[0] = np.nan

    @classmethod
    def from_snapshot(cls, snapshot_dir, stock_info_file):
        snapshot_dir = Path(snapshot_dir)
        benchmark_file = next(snapshot_dir.glob('sh000300_*'))
        benchmark = pd.read_csv(benchmark_file, usecols=['date', 'open', 'close'])
        dates = benchmark.date.to_numpy(dtype=np.int32)
        calendar_index = pd.Index(dates)
        pool = pd.read_csv(stock_info_file, dtype={'symbol': str})
        symbols = sorted(set(pool.symbol).intersection(
            item.name.split('_')[0] for item in snapshot_dir.iterdir()))
        matrices = {key: np.full((len(dates), len(symbols)), np.nan,
                                 dtype=np.float32)
                    for key in ('open', 'high', 'low', 'close', 'volume')}
        for column, symbol in enumerate(symbols):
            path = next(snapshot_dir.glob(symbol + '_*'))
            bars = pd.read_csv(path, usecols=['date', 'open', 'high', 'low',
                                             'close', 'volume'])
            row = calendar_index.get_indexer(bars.date.to_numpy(dtype=np.int32))
            valid = row >= 0
            for key, matrix in matrices.items():
                matrix[row[valid], column] = bars[key].to_numpy(
                    dtype=np.float32)[valid]
        return cls(dates, symbols, matrices['open'], matrices['high'],
                   matrices['low'], matrices['close'], matrices['volume'],
                   benchmark.close.to_numpy(), benchmark.open.to_numpy())

    @classmethod
    def from_research_data(cls, signal_dir, research_dir, start_date=20200101,
                           end_date=20261002):
        """Load adjusted signals plus raw fills and point-in-time metadata."""
        signal_dir = Path(signal_dir)
        research_dir = Path(research_dir)
        extra_dir = research_dir / 'signal_extra'
        raw_dir = research_dir / 'raw'
        master = pd.read_csv(research_dir / 'security_master.csv', dtype={'code': str})

        signal_paths = {}
        for directory in (signal_dir, extra_dir):
            if not directory.exists():
                continue
            for path in directory.iterdir():
                symbol = path.name.split('_')[0].split('.')[0]
                if symbol.startswith(('sh', 'sz')):
                    signal_paths[symbol] = path
        raw_paths = {path.stem: path for path in raw_dir.glob('*.csv')}
        symbols = sorted(set(master.symbol).intersection(signal_paths).intersection(raw_paths))

        benchmark_file = next(signal_dir.glob('sh000300_*'))
        benchmark = pd.read_csv(benchmark_file, usecols=['date', 'open', 'close'])
        benchmark = benchmark[(benchmark.date >= int(start_date)) &
                              (benchmark.date <= int(end_date))].copy()
        dates = benchmark.date.to_numpy(dtype=np.int32)
        calendar_index = pd.Index(dates)
        shape = (len(dates), len(symbols))
        signal = {key: np.full(shape, np.nan, dtype=np.float32)
                  for key in ('open', 'high', 'low', 'close', 'volume')}
        execution = {key: np.full(shape, np.nan, dtype=np.float32)
                     for key in ('open', 'high', 'low', 'close', 'volume')}
        amount = np.full(shape, np.nan, dtype=np.float32)
        market_cap = np.full(shape, np.nan, dtype=np.float64)

        def load_matrix(path, matrices, column, extra_columns=()):
            requested = ['date', 'open', 'high', 'low', 'close', 'volume']
            available = pd.read_csv(path, nrows=0).columns
            requested.extend(item for item in extra_columns if item in available)
            bars = pd.read_csv(path, usecols=requested)
            rows = calendar_index.get_indexer(bars.date.to_numpy(dtype=np.int32))
            valid = rows >= 0
            for key in ('open', 'high', 'low', 'close', 'volume'):
                matrices[key][rows[valid], column] = pd.to_numeric(
                    bars[key], errors='coerce').to_numpy(dtype=np.float32)[valid]
            return bars, rows, valid

        for column, symbol in enumerate(symbols):
            load_matrix(signal_paths[symbol], signal, column)
            bars, rows, valid = load_matrix(
                raw_paths[symbol], execution, column,
                ('amount', 'outstanding_share', 'turnover'))
            if 'amount' in bars:
                amount[rows[valid], column] = pd.to_numeric(
                    bars.amount, errors='coerce').to_numpy(dtype=np.float32)[valid]
            if 'outstanding_share' in bars:
                shares = pd.to_numeric(bars.outstanding_share, errors='coerce').to_numpy()
                close = pd.to_numeric(bars.close, errors='coerce').to_numpy()
                market_cap[rows[valid], column] = (shares * close)[valid]

        industry = cls._load_industry_matrix(
            research_dir / 'industry_changes.csv', dates, symbols)
        st_status = cls._load_sz_st_matrix(
            research_dir / 'sz_name_changes.csv', dates, symbols)
        actions = cls._load_corporate_actions(
            research_dir / 'corporate_actions.csv', dates, symbols)
        return cls(dates, symbols, signal['open'], signal['high'], signal['low'],
                   signal['close'], signal['volume'], benchmark.close.to_numpy(),
                   benchmark.open.to_numpy(), execution=execution, amount=amount,
                   market_cap=market_cap, industry=industry,
                   corporate_actions=actions, st_status=st_status)

    @staticmethod
    def _load_sz_st_matrix(path, dates, symbols):
        matrix = np.zeros((len(dates), len(symbols)), dtype=bool)
        if not path.exists() or path.stat().st_size == 0:
            return matrix
        frame = pd.read_csv(path, dtype={'证券代码': str})
        required = {'变更日期', '证券代码', '变更后简称'}
        if frame.empty or not required.issubset(frame.columns):
            return matrix
        symbol_index = {name: position for position, name in enumerate(symbols)}
        frame['symbol'] = 'sz' + frame['证券代码'].str.zfill(6)
        frame['change'] = pd.to_datetime(frame['变更日期'], errors='coerce').dt.strftime(
            '%Y%m%d')
        for symbol, group in frame.groupby('symbol'):
            column = symbol_index.get(symbol)
            if column is None:
                continue
            for row in group.dropna(subset=['change']).sort_values('change').to_dict('records'):
                start = int(np.searchsorted(dates, int(row['change']), side='left'))
                name = str(row['变更后简称']).upper().replace(' ', '')
                matrix[start:, column] = name.startswith(('ST', '*ST', 'S*ST', 'SST'))
        return matrix

    @staticmethod
    def _load_industry_matrix(path, dates, symbols):
        matrix = np.full((len(dates), len(symbols)), -1, dtype=np.int16)
        if not path.exists() or path.stat().st_size == 0:
            return matrix
        frame = pd.read_csv(path, dtype={'证券代码': str})
        if frame.empty or '变更日期' not in frame:
            return matrix
        if '分类标准' in frame:
            preferred = frame[frame['分类标准'].astype(str).str.contains('申银万国')]
            if not preferred.empty:
                frame = preferred
        industry_column = next((name for name in ('行业门类', '行业大类', '行业次类')
                                if name in frame), None)
        if industry_column is None:
            return matrix
        frame['symbol'] = frame.get('symbol', '').astype(str)
        frame['change'] = pd.to_datetime(frame['变更日期'], errors='coerce').dt.strftime(
            '%Y%m%d')
        labels = sorted(frame[industry_column].dropna().astype(str).unique())
        label_codes = {name: position for position, name in enumerate(labels)}
        symbol_index = {name: position for position, name in enumerate(symbols)}
        for symbol, group in frame.groupby('symbol'):
            column = symbol_index.get(symbol)
            if column is None:
                continue
            group = group.dropna(subset=['change']).sort_values('change')
            for row in group.to_dict('records'):
                change = int(row['change'])
                start = int(np.searchsorted(dates, change, side='left'))
                label = str(row[industry_column])
                matrix[start:, column] = label_codes[label]
        return matrix

    @staticmethod
    def _load_corporate_actions(path, dates, symbols):
        """Return record-day events with mapped cash/share credit dates."""
        if not path.exists() or path.stat().st_size == 0:
            return {}
        frame = pd.read_csv(path, dtype={'symbol': str})
        if frame.empty or '股权登记日' not in frame:
            return {}
        symbol_index = {name: position for position, name in enumerate(symbols)}
        date_index = {int(value): position for position, value in enumerate(dates)}

        def mapped_day(value, fallback):
            parsed = pd.to_datetime(value, errors='coerce')
            if pd.isna(parsed):
                parsed = pd.to_datetime(fallback, errors='coerce')
            if pd.isna(parsed):
                return None
            number = int(parsed.strftime('%Y%m%d'))
            position = int(np.searchsorted(dates, number, side='left'))
            return position if position < len(dates) else None

        actions = {}
        for row in frame.to_dict('records'):
            symbol = symbol_index.get(str(row.get('symbol')))
            record = pd.to_datetime(row.get('股权登记日'), errors='coerce')
            if symbol is None or pd.isna(record):
                continue
            record_day = date_index.get(int(record.strftime('%Y%m%d')))
            if record_day is None:
                continue
            cash_per_share = pd.to_numeric(row.get('派息比例'), errors='coerce')
            stock_per_share = (pd.to_numeric(row.get('送股比例'), errors='coerce') +
                               pd.to_numeric(row.get('转增比例'), errors='coerce'))
            cash_per_share = 0.0 if pd.isna(cash_per_share) else float(cash_per_share) / 10.0
            stock_per_share = 0.0 if pd.isna(stock_per_share) else float(stock_per_share) / 10.0
            ex_date = row.get('除权日')
            event = {
                'symbol': symbol,
                'cash_per_share': cash_per_share,
                'stock_per_share': stock_per_share,
                'cash_day': mapped_day(row.get('派息日'), ex_date),
                'stock_day': mapped_day(row.get('股份到账日'), ex_date),
                'description': row.get('实施方案分红说明', ''),
            }
            if cash_per_share or stock_per_share:
                actions.setdefault(record_day, []).append(event)
        return actions

    @staticmethod
    def _ordered(scores, eligible, limit):
        candidates = np.flatnonzero(eligible & np.isfinite(scores))
        # Stable symbol order is the explicit tie break.
        ranked = candidates[np.lexsort((candidates, -scores[candidates]))]
        return ranked[:limit].tolist()

    def momentum_picks(self, day, limit=20):
        """Six-to-one month market residual momentum, beta from prior year."""
        if day < 252:
            return []
        regression = self.returns[day - 251:day - 20].astype(np.float64)
        market = self.benchmark_returns[day - 251:day - 20]
        valid = np.isfinite(regression) & np.isfinite(market[:, None])
        market_matrix = market[:, None]
        count = valid.sum(axis=0)
        market_mean = np.divide(np.where(valid, market_matrix, 0).sum(axis=0),
                                count, out=np.zeros(len(count)), where=count > 0)
        stock_mean = np.divide(np.where(valid, regression, 0).sum(axis=0),
                               count, out=np.zeros(len(count)), where=count > 0)
        market_centered = market_matrix - market_mean
        stock_centered = regression - stock_mean
        numerator = np.where(valid, market_centered * stock_centered, 0).sum(axis=0)
        denominator = np.where(valid, market_centered ** 2, 0).sum(axis=0)
        beta = np.divide(numerator, denominator,
                         out=np.full(len(count), np.nan), where=denominator > 0)
        formation = self.returns[day - 125:day - 20].astype(np.float64)
        formation_market = self.benchmark_returns[day - 125:day - 20, None]
        formation_valid = np.isfinite(formation)
        residual = formation - beta[None, :] * formation_market
        score = np.nansum(residual, axis=0)
        price_history = self.close[day - 251:day + 1]
        minimum_price = np.where(np.isfinite(price_history),
                                 price_history, np.inf).min(axis=0)
        eligible = ((count >= 200) & (formation_valid.sum(axis=0) >= 95) &
                    np.isfinite(self.close[day]) & (self.close[day] > 1) &
                    (self.volume[day] > 0) &
                    ~self.st_status[day] &
                    (minimum_price > 1))
        return self._ordered(score, eligible, limit)

    def reversal_picks(self, day, held, limit=10):
        if day < 120 or not self.benchmark_close[day] > self.market_ma120[day]:
            return []
        with np.errstate(divide='ignore', invalid='ignore'):
            change = self.close[day] / self.close[day - 5] - 1
            score = (self.ma10[day] - self.close[day]) / self.atr21[day]
        complete = np.isfinite(self.close[day - 5:day + 1]).all(axis=0)
        eligible = (complete & (change <= -0.05) & (score >= 1.0) &
                    (self.close[day] > self.ma120[day]) &
                    (self.close[day] > 1) & (self.volume[day] > 0) &
                    ~self.st_status[day] &
                    np.isfinite(self.atr21[day]) & (self.atr21[day] > 0))
        if held:
            eligible[list(held)] = False
        return self._ordered(score, eligible, limit)

    def breakout_picks(self, day, held, limit=10):
        if day < 200 or not self.benchmark_close[day] > self.market_ma200[day]:
            return []
        with np.errstate(divide='ignore', invalid='ignore'):
            score = (self.close[day] - self.prior60_high[day]) / self.atr21[day]
        eligible = ((score > 0) & (self.close[day] > self.ma120[day]) &
                    (self.close[day] > 1) & (self.volume[day] > 0) &
                    ~self.st_status[day] &
                    np.isfinite(self.atr21[day]) & (self.atr21[day] > 0))
        if held:
            eligible[list(held)] = False
        return self._ordered(score, eligible, limit)


def _fee_components(quantity, price, side):
    gross = quantity * price
    commission = max(gross * BROKER_RATE, 5.0)
    transfer = gross * TRANSFER_RATE
    stamp = gross * SELL_STAMP_RATE if side == 'sell' else 0.0
    return commission, transfer, stamp


def _fee(quantity, price, side):
    return sum(_fee_components(quantity, price, side))


def _can_trade(panel, day, symbol):
    opening = float(panel.exec_open[day, symbol])
    return (np.isfinite(opening) and opening > 0 and
            np.isfinite(panel.exec_high[day, symbol]) and
            np.isfinite(panel.exec_low[day, symbol]) and
            panel.exec_high[day, symbol] > panel.exec_low[day, symbol] and
            panel.exec_volume[day, symbol] > 0)


def run_selection_backtest(panel, strategy, year, slippage_bps=25):
    """Run one strategy/year from cash, using the previous session as signal."""
    if strategy not in STRATEGIES:
        raise ValueError('unknown strategy: {}'.format(strategy))
    if slippage_bps < 0:
        raise ValueError('slippage_bps must be nonnegative')
    dates = panel.dates
    indices = np.flatnonzero(dates // 10000 == int(year))
    if len(indices) == 0 or indices[0] == 0:
        raise ValueError('no backtest dates or missing prior signal date')
    first, last = int(indices[0]), int(indices[-1])
    target_holdings = 20 if strategy == 'residual_momentum' else 10
    cash = INITIAL_CASH
    positions = {}  # symbol index -> quantity, cost, entry bar, entry ATR, peak
    pending_sells = set()
    pending_buys = []
    records = []
    trades = []
    buy_count = sell_count = 0
    total_fees = traded_notional = total_slippage = 0.0
    total_commission = total_transfer = total_stamp = 0.0
    dividends_received = 0.0
    realized_wins = []
    last_close = np.full(len(panel.symbols), np.nan)
    if first > 0:
        history = pd.DataFrame(panel.exec_close[:first]).ffill().iloc[-1]
        last_close = history.to_numpy(dtype=np.float64)
    cash_receivables = {}
    share_receivables = {}

    def market_value(day, at_open):
        total = 0.0
        for symbol, position in positions.items():
            raw = panel.exec_open[day, symbol] if at_open else panel.exec_close[day, symbol]
            price = float(raw) if np.isfinite(raw) and raw > 0 else last_close[symbol]
            if not np.isfinite(price) or price <= 0:
                price = position['entry_price']
            total += position['quantity'] * price
        return total

    def signals(day):
        if strategy == 'residual_momentum':
            if day + 1 >= len(dates) or dates[day] // 100 == dates[day + 1] // 100:
                return [], []
            desired = panel.momentum_picks(day)
            return [s for s in positions if s not in desired], [s for s in desired if s not in positions]
        if strategy == 'trend_reversal':
            sell = []
            for symbol, position in positions.items():
                price = panel.close[day, symbol]
                if (not panel.benchmark_close[day] > panel.market_ma120[day] or
                        (np.isfinite(price) and (price >= panel.ma10[day, symbol] or
                         price < position['entry_signal_price'] - 2 * position['entry_atr'])) or
                        day - position['entry_day'] >= 10):
                    sell.append(symbol)
            buy = panel.reversal_picks(day, positions)
            return sell, buy
        sell = []
        for symbol, position in positions.items():
            price = panel.close[day, symbol]
            if np.isfinite(price):
                position['peak'] = max(position['peak'], float(price))
            atr = panel.atr21[day, symbol]
            trail = position['peak'] - 3 * atr if np.isfinite(atr) else -np.inf
            if (not panel.benchmark_close[day] > panel.market_ma200[day] or
                    (np.isfinite(price) and price < max(trail, panel.ma60[day, symbol]))):
                sell.append(symbol)
        buy = panel.breakout_picks(day, positions)
        return sell, buy

    sell, buy = signals(first - 1)
    pending_sells.update(sell)
    pending_buys = buy
    for day in range(first, last + 1):
        # Rights captured on an earlier record date are credited independently
        # of whether the original shares have since been sold.
        for item in cash_receivables.pop(day, []):
            cash += item['cash']
            dividends_received += item['cash']
            trades.append((int(dates[day]), panel.symbols[item['symbol']],
                           'cash_dividend', 0, 0.0, 0.0, 0.0, 0.0, 0.0,
                           0.0, 0.0, item['cash'], item['description']))
        for item in share_receivables.pop(day, []):
            symbol = item['symbol']
            quantity = item['quantity']
            if quantity <= 0:
                continue
            if symbol in positions:
                positions[symbol]['quantity'] += quantity
            else:
                signal_price = panel.close[max(first - 1, day - 1), symbol]
                raw_price = panel.exec_close[max(first - 1, day - 1), symbol]
                positions[symbol] = {
                    'quantity': quantity, 'cost': 0.0,
                    'entry_price': float(raw_price) if np.isfinite(raw_price) else 0.0,
                    'entry_signal_price': (float(signal_price)
                                           if np.isfinite(signal_price) else 0.0),
                    'entry_day': day, 'entry_atr': 0.0,
                    'peak': float(signal_price) if np.isfinite(signal_price) else 0.0,
                }
            trades.append((int(dates[day]), panel.symbols[symbol],
                           'stock_dividend', quantity, 0.0, 0.0, 0.0, 0.0,
                           0.0, 0.0, 0.0, 0.0, item['description']))

        # Pending exits persist through suspensions/one-price sessions. Buys expire.
        for symbol in sorted(pending_sells):
            if symbol not in positions or not _can_trade(panel, day, symbol):
                continue
            position = positions.pop(symbol)
            reference = float(panel.exec_open[day, symbol])
            price = reference * (1 - slippage_bps / 10000.0)
            quantity = position['quantity']
            commission, transfer, stamp = _fee_components(quantity, price, 'sell')
            fee = commission + transfer + stamp
            proceeds = quantity * price - fee
            cash += proceeds
            total_fees += fee
            total_commission += commission
            total_transfer += transfer
            total_stamp += stamp
            slippage_cost = quantity * (reference - price)
            total_slippage += slippage_cost
            traded_notional += quantity * price
            sell_count += 1
            realized_wins.append(proceeds > position['cost'])
            trades.append((int(dates[day]), panel.symbols[symbol], 'sell',
                           quantity, reference, price, commission, transfer,
                           stamp, fee, slippage_cost, 0.0, ''))
        pending_sells.intersection_update(positions)

        for symbol in pending_buys:
            if len(positions) >= target_holdings:
                break
            if symbol in positions or symbol in pending_sells or not _can_trade(panel, day, symbol):
                continue
            held_value = market_value(day, at_open=True)
            equity = cash + held_value
            budget = min(MAX_GROSS / target_holdings * equity,
                         MAX_SYMBOL * equity, MAX_GROSS * equity - held_value,
                         cash / (1 + BROKER_RATE + TRANSFER_RATE))
            reference = float(panel.exec_open[day, symbol])
            price = reference * (1 + slippage_bps / 10000.0)
            quantity = int(budget / price / 100) * 100
            while quantity > 0 and quantity * price + _fee(quantity, price, 'buy') > cash:
                quantity -= 100
            if quantity < 100:
                continue
            commission, transfer, stamp = _fee_components(quantity, price, 'buy')
            fee = commission + transfer + stamp
            cost = quantity * price + fee
            cash -= cost
            total_fees += fee
            total_commission += commission
            total_transfer += transfer
            total_stamp += stamp
            slippage_cost = quantity * (price - reference)
            total_slippage += slippage_cost
            traded_notional += quantity * price
            buy_count += 1
            atr = panel.atr21[day - 1, symbol]
            positions[symbol] = {'quantity': quantity, 'cost': cost,
                                 'entry_price': price, 'entry_day': day,
                                 'entry_signal_price': float(panel.close[day - 1, symbol]),
                                 'entry_atr': float(atr) if np.isfinite(atr) else 0.0,
                                 'peak': float(panel.close[day - 1, symbol])}
            trades.append((int(dates[day]), panel.symbols[symbol], 'buy',
                           quantity, reference, price, commission, transfer,
                           stamp, fee, slippage_cost, 0.0, ''))

        observed = panel.exec_close[day]
        fresh = np.isfinite(observed) & (observed > 0)
        last_close[fresh] = observed[fresh]
        stocks = market_value(day, at_open=False)
        capital = cash + stocks
        records.append((int(dates[day]), cash, stocks, capital,
                        stocks / capital if capital > 0 else np.nan,
                        len(positions)))

        # Capture entitlements after the record-date close.
        for event in panel.corporate_actions.get(day, []):
            position = positions.get(event['symbol'])
            if position is None:
                continue
            if event['cash_per_share'] and event['cash_day'] is not None:
                cash_receivables.setdefault(event['cash_day'], []).append({
                    'symbol': event['symbol'],
                    'cash': position['quantity'] * event['cash_per_share'],
                    'description': event['description'],
                })
            if event['stock_per_share'] and event['stock_day'] is not None:
                share_receivables.setdefault(event['stock_day'], []).append({
                    'symbol': event['symbol'],
                    'quantity': int(np.floor(
                        position['quantity'] * event['stock_per_share'])),
                    'description': event['description'],
                })
        if day < last:
            sell, buy = signals(day)
            pending_sells.update(sell)
            pending_buys = buy

    curve = pd.DataFrame(records, columns=['date', 'cash', 'stocks', 'capital',
                                           'exposure', 'holdings'])
    trades = pd.DataFrame(trades, columns=[
        'date', 'symbol', 'side', 'quantity', 'reference_price', 'price',
        'commission', 'transfer_fee', 'stamp_tax', 'fee', 'slippage_cost',
        'cash_flow', 'description'])
    drawdown_curve = np.r_[INITIAL_CASH, curve.capital.to_numpy(dtype=np.float64)]
    gross_fixed_path = curve.capital.iloc[-1] + total_fees + total_slippage
    return {
        'strategy': strategy, 'year': int(year), 'start': int(dates[first]),
        'end': int(dates[last]), 'slippage_bps_per_side': slippage_bps,
        'return_pct': (curve.capital.iloc[-1] / INITIAL_CASH - 1) * 100,
        'max_drawdown_pct': (drawdown_curve /
                             np.maximum.accumulate(drawdown_curve) - 1).min() * 100,
        'benchmark_return_pct': (panel.benchmark_close[last] /
                                 panel.benchmark_open[first] - 1) * 100,
        'average_exposure_pct': curve.exposure.mean() * 100,
        'average_holdings': curve.holdings.mean(),
        'buys': buy_count, 'sells': sell_count,
        'closed_win_rate_pct': np.mean(realized_wins) * 100 if realized_wins else np.nan,
        'gross_fixed_path_return_pct': (gross_fixed_path / INITIAL_CASH - 1) * 100,
        'commission_cny': total_commission,
        'transfer_fee_cny': total_transfer,
        'stamp_tax_cny': total_stamp,
        'fees_cny': total_fees,
        'slippage_cost_cny': total_slippage,
        'dividends_received_cny': dividends_received,
        'turnover_initial_cash_multiple': traded_notional / INITIAL_CASH,
    }, curve, trades


def _trade_templates(panel, trades, year):
    """Convert executed round trips into fixed-date placebo templates."""
    date_index = {int(value): position for position, value in enumerate(panel.dates)}
    symbol_index = {value: position for position, value in enumerate(panel.symbols)}
    final_day = int(np.flatnonzero(panel.dates // 10000 == int(year))[-1])
    active = {}
    templates = []
    for trade in trades[trades.side.isin(['buy', 'sell'])].itertuples(index=False):
        symbol = symbol_index[trade.symbol]
        day = date_index[int(trade.date)]
        if trade.side == 'buy':
            active[symbol] = {
                'entry_day': day, 'symbol': symbol,
                'notional': float(trade.quantity * trade.reference_price),
            }
        elif symbol in active:
            item = active.pop(symbol)
            item['exit_day'] = day
            templates.append(item)
    for item in active.values():
        item['exit_day'] = final_day
        templates.append(item)
    return sorted(templates, key=lambda item: (item['entry_day'], item['symbol']))


def _matching_pool(panel, template, pool_size=50):
    day = template['entry_day']
    exit_day = template['exit_day']
    target = template['symbol']
    signal_day = max(0, day - 1)
    start60 = max(0, signal_day - 59)
    start120 = max(0, signal_day - 119)
    with np.errstate(divide='ignore', invalid='ignore'):
        price = panel.close[signal_day].astype(np.float64)
        traded_value = panel.amount[start60:signal_day + 1].astype(np.float64)
        fallback_value = (panel.close[start60:signal_day + 1].astype(np.float64) *
                          panel.volume[start60:signal_day + 1].astype(np.float64))
        liquidity = np.nanmean(np.where(np.isfinite(traded_value), traded_value,
                                        fallback_value), axis=0)
        volatility = np.nanstd(panel.returns[start60:signal_day + 1], axis=0)
        stock_returns = panel.returns[start120:signal_day + 1].astype(np.float64)
        market_returns = panel.benchmark_returns[start120:signal_day + 1]
        market_centered = market_returns - np.nanmean(market_returns)
        denominator = np.nansum(market_centered ** 2)
        beta = np.nansum(
            (stock_returns - np.nanmean(stock_returns, axis=0)) *
            market_centered[:, None], axis=0) / denominator
    cap = panel.market_cap[signal_day]
    industry = panel.industry[signal_day]
    eligible = (np.isfinite(panel.open[day]) & (panel.open[day] > 0) &
                np.isfinite(panel.open[exit_day]) & (panel.open[exit_day] > 0) &
                np.isfinite(panel.exec_open[day]) & (panel.exec_open[day] > 0) &
                np.isfinite(panel.exec_open[exit_day]) &
                (panel.exec_open[exit_day] > 0) &
                (panel.exec_high[day] > panel.exec_low[day]) &
                (panel.exec_high[exit_day] > panel.exec_low[exit_day]) &
                (panel.exec_volume[day] > 0) &
                (panel.exec_volume[exit_day] > 0) &
                np.isfinite(price) & (price > 1) & np.isfinite(liquidity) &
                (liquidity > 0) & np.isfinite(volatility) &
                ~panel.st_status[signal_day])
    eligible[target] = False
    candidates = np.flatnonzero(eligible)
    target_industry = industry[target]
    if target_industry >= 0:
        same_industry = candidates[industry[candidates] == target_industry]
        if len(same_industry) >= min(10, pool_size):
            candidates = same_industry
    if len(candidates) == 0:
        return np.array([], dtype=np.int32)

    distance = np.zeros(len(candidates), dtype=np.float64)
    used = np.zeros(len(candidates), dtype=np.float64)
    for values, logarithmic in ((price, True), (liquidity, True),
                                (cap, True), (volatility, False), (beta, False)):
        target_value = values[target]
        candidate_values = values[candidates]
        valid = np.isfinite(target_value) & np.isfinite(candidate_values)
        if not np.isfinite(target_value) or valid.sum() == 0:
            continue
        if logarithmic:
            valid &= candidate_values > 0
            if target_value <= 0 or valid.sum() == 0:
                continue
            delta = np.abs(np.log(candidate_values[valid] / target_value))
        else:
            scale = np.nanstd(candidate_values[valid])
            scale = scale if np.isfinite(scale) and scale > 1e-12 else 1.0
            delta = np.abs(candidate_values[valid] - target_value) / scale
        distance[valid] += delta
        used[valid] += 1
    distance = np.divide(distance, used, out=np.full_like(distance, np.inf),
                         where=used > 0)
    order = np.lexsort((candidates, distance))
    return candidates[order[:pool_size]].astype(np.int32)


def run_matched_placebos(panel, trades, year, actual_return_pct,
                         replicates=500, seed=20261002):
    """Fixed-path matched placebo distribution.

    Entry/exit dates, holding periods, trade notionals and explicit costs are
    copied from the strategy.  Symbols are replaced with candidates matched on
    industry when available, price, liquidity, market cap, beta and volatility.
    """
    templates = _trade_templates(panel, trades, year)
    if not templates:
        return {}, pd.DataFrame()
    pools = [_matching_pool(panel, item) for item in templates]
    valid = [position for position, pool in enumerate(pools) if len(pool)]
    if not valid:
        return {}, pd.DataFrame()
    templates = [templates[position] for position in valid]
    pools = [pools[position] for position in valid]

    year_days = np.flatnonzero(panel.dates // 10000 == int(year))
    first, last = int(year_days[0]), int(year_days[-1])
    length = last - first + 1
    costs = np.zeros(length, dtype=np.float64)
    date_index = {int(value): position for position, value in enumerate(panel.dates)}
    cost_trades = trades[trades.side.isin(['buy', 'sell'])]
    for trade in cost_trades.itertuples(index=False):
        day = date_index[int(trade.date)] - first
        if 0 <= day < length:
            costs[day] += float(trade.fee + trade.slippage_cost)
    cumulative_costs = np.cumsum(costs)

    rng = np.random.default_rng(seed)
    rows = []
    for replicate in range(int(replicates)):
        curve = np.full(length, INITIAL_CASH, dtype=np.float64) - cumulative_costs
        occupied = {}
        for template, pool in zip(templates, pools):
            candidates = pool.copy()
            rng.shuffle(candidates)
            chosen = int(candidates[0])
            for candidate in candidates:
                intervals = occupied.get(int(candidate), [])
                overlap = any(not (template['exit_day'] <= entry or
                                   template['entry_day'] >= exit_day)
                              for entry, exit_day in intervals)
                if not overlap:
                    chosen = int(candidate)
                    break
            occupied.setdefault(chosen, []).append(
                (template['entry_day'], template['exit_day']))
            entry = template['entry_day']
            exit_day = template['exit_day']
            entry_price = float(panel.open[entry, chosen])
            exit_price = float(panel.open[exit_day, chosen])
            notional = template['notional']
            local_start = max(entry, first) - first
            local_exit = min(exit_day, last) - first
            closes = pd.Series(panel.close[max(entry, first):min(exit_day, last) + 1,
                                            chosen]).ffill().to_numpy(dtype=np.float64)
            if len(closes):
                curve[local_start:local_exit + 1] += notional * (
                    closes / entry_price - 1)
            if exit_day <= last:
                curve[local_exit:] += notional * (exit_price / entry_price - 1)
                # Remove the close-based mark on the exit day and replace it
                # with the opening exit value.
                if len(closes):
                    curve[local_exit] -= notional * (closes[-1] / entry_price - 1)
        drawdown = np.r_[INITIAL_CASH, curve]
        rows.append({
            'replicate': replicate,
            'return_pct': (curve[-1] / INITIAL_CASH - 1) * 100,
            'max_drawdown_pct': (drawdown /
                                 np.maximum.accumulate(drawdown) - 1).min() * 100,
        })
    distribution = pd.DataFrame(rows)
    percentile = (distribution.return_pct < actual_return_pct).mean() * 100
    summary = {
        'placebo_replicates': len(distribution),
        'matched_trade_templates': len(templates),
        'actual_return_percentile': percentile,
        'placebo_return_mean_pct': distribution.return_pct.mean(),
        'placebo_return_p05_pct': distribution.return_pct.quantile(0.05),
        'placebo_return_p50_pct': distribution.return_pct.quantile(0.50),
        'placebo_return_p95_pct': distribution.return_pct.quantile(0.95),
        'placebo_drawdown_p50_pct': distribution.max_drawdown_pct.quantile(0.50),
    }
    return summary, distribution
