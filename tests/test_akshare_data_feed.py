import unittest
from unittest import mock

import pandas as pd

from abupy.CoreBu.ABuEnv import EMarketSubType, EMarketTargetType
from abupy.MarketBu.ABuDataFeedAkShare import AKShareCNApi
from abupy.MarketBu import ABuMarket
from abupy.MarketBu.ABuSymbol import Symbol


class AKShareCNApiTest(unittest.TestCase):

    def test_normalizes_stock_columns(self):
        raw = pd.DataFrame({
            '日期': ['2024-01-02', '2024-01-03'],
            '开盘': [10.0, 10.5],
            '收盘': [10.5, 10.4],
            '最高': [10.7, 10.6],
            '最低': [9.9, 10.2],
            '成交量': [1000, 1200],
            '成交额': [10000, 12500],
            '涨跌幅': [5.0, -0.95],
        })
        symbol = Symbol(EMarketTargetType.E_MARKET_TARGET_CN, EMarketSubType.SH, '600000')
        api = AKShareCNApi(symbol)

        result = api._normalize_kline(raw, volume_multiplier=100)

        self.assertEqual('sh600000', result.name)
        self.assertEqual([20240102, 20240103], result['date'].tolist())
        self.assertEqual([1, 2], result['date_week'].tolist())
        self.assertEqual(10.5, result.iloc[1]['pre_close'])
        self.assertEqual([0, 1], result['key'].tolist())
        self.assertEqual([100000, 120000], result['volume'].tolist())

    @mock.patch('abupy.MarketBu.ABuDataFeedAkShare._load_akshare')
    def test_uses_index_endpoint_for_index_symbols(self, load_akshare):
        fake_ak = mock.Mock()
        fake_ak.stock_zh_index_daily_em.return_value = pd.DataFrame({
            'date': ['2024-01-02'], 'open': [3000], 'close': [3010],
            'high': [3020], 'low': [2990], 'volume': [100], 'amount': [1000],
        })
        load_akshare.return_value = fake_ak
        symbol = Symbol(EMarketTargetType.E_MARKET_TARGET_CN, EMarketSubType.SH, '000001')
        api = AKShareCNApi(symbol)

        result = api.kline(start='2024-01-01', end='2024-01-03')

        self.assertEqual(1, len(result))
        fake_ak.stock_zh_index_daily_em.assert_called_once_with(
            symbol='sh000001', start_date='20240101', end_date='20240103'
        )
        fake_ak.stock_zh_a_hist.assert_not_called()

    @mock.patch.object(AKShareCNApi, 'all_symbols', return_value=['sh600000', 'sh000001'])
    def test_market_uses_private_source_universe(self, all_symbols):
        previous = ABuMarket.ABuEnv.g_private_data_source
        try:
            ABuMarket.ABuEnv.g_private_data_source = AKShareCNApi
            self.assertEqual(['sh600000', 'sh000001'], ABuMarket._all_cn_symbol(index=True))
            all_symbols.assert_called_once_with(index=True)
        finally:
            ABuMarket.ABuEnv.g_private_data_source = previous


if __name__ == '__main__':
    unittest.main()
