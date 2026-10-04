import unittest
from unittest import mock

import pandas as pd

from abupy.MarketBu.ABuRealtimeMarket import (
    AKShareRealtimeMarketData, RealtimeMarketDataError, normalize_cn_symbol,
)


NOW = pd.Timestamp("2026-10-09 10:02:30", tz="Asia/Shanghai")


def eastmoney_snapshot():
    return pd.DataFrame({
        "代码": ["600000", "000001"], "名称": ["浦发银行", "平安银行"],
        "最新价": [10.2, 12.3], "今开": [10.0, 12.1],
        "最高": [10.3, 12.5], "最低": [9.9, 12.0], "昨收": [9.95, 12.0],
        "成交量": [1000, 2000], "成交额": [1_020_000, 2_460_000],
        "涨跌幅": [2.51, 2.5], "换手率": [0.5, 0.8],
    })


class AKShareRealtimeMarketDataTest(unittest.TestCase):

    def test_symbol_normalization(self):
        self.assertEqual("sh600000", normalize_cn_symbol("600000.SH"))
        self.assertEqual("sz000001", normalize_cn_symbol("000001"))
        self.assertEqual("bj920001", normalize_cn_symbol("920001"))

    def test_snapshot_normalizes_filters_and_tracks_health(self):
        fake = mock.Mock()
        fake.stock_zh_a_spot_em.return_value = eastmoney_snapshot()
        adapter = AKShareRealtimeMarketData(
            ak_module=fake, retries=1, now=lambda: NOW)

        result = adapter.snapshot(["sh600000"])

        self.assertEqual(["sh600000"], result.symbol.tolist())
        self.assertEqual(100_000, result.iloc[0].volume)
        self.assertEqual(0.005, result.iloc[0].turnover_rate)
        self.assertEqual("received_at_proxy", result.iloc[0].timestamp_quality)
        self.assertTrue(result.iloc[0].quote_valid)
        health = adapter.health()
        self.assertTrue(health.connected)
        self.assertEqual("akshare_eastmoney", health.last_provider)
        self.assertEqual(1, health.last_record_count)

    def test_snapshot_falls_back_to_sina_and_preserves_provider_time(self):
        fake = mock.Mock()
        fake.stock_zh_a_spot_em.side_effect = RuntimeError("primary unavailable")
        raw = eastmoney_snapshot().iloc[[0]].copy()
        raw["买入"] = [10.19]
        raw["卖出"] = [10.20]
        raw["时间戳"] = ["2026-10-09 10:02:00"]
        fake.stock_zh_a_spot.return_value = raw
        adapter = AKShareRealtimeMarketData(
            ak_module=fake, retries=1, now=lambda: NOW)

        result = adapter.snapshot()

        self.assertEqual("akshare_sina", result.iloc[0].source)
        self.assertEqual("provider", result.iloc[0].timestamp_quality)
        self.assertEqual(10.19, result.iloc[0].bid1)
        self.assertEqual(1000, result.iloc[0].volume)
        self.assertIn("eastmoney_failed", adapter.health().last_warning)

    def test_snapshot_requires_requested_symbols(self):
        fake = mock.Mock()
        fake.stock_zh_a_spot_em.return_value = eastmoney_snapshot()
        adapter = AKShareRealtimeMarketData(
            ak_module=fake, retries=1, now=lambda: NOW)

        with self.assertRaises(RealtimeMarketDataError):
            adapter.snapshot(["600519"])

        self.assertFalse(adapter.health().connected)
        self.assertEqual(1, adapter.health().consecutive_failures)

    def test_minute_bars_are_normalized_and_marked_complete(self):
        fake = mock.Mock()
        fake.stock_zh_a_hist_min_em.return_value = pd.DataFrame({
            "时间": ["2026-10-09 10:00:00", "2026-10-09 10:02:00"],
            "开盘": [10.0, 10.2], "收盘": [10.2, 10.25],
            "最高": [10.2, 10.3], "最低": [9.99, 10.15],
            "成交量": [100, 80], "成交额": [101_000, 82_000],
            "均价": [10.1, 10.25],
        })
        adapter = AKShareRealtimeMarketData(
            ak_module=fake, retries=1, now=lambda: NOW)

        result = adapter.minute_bars(
            "sh600000", period="1", start="2026-10-09 09:30:00",
            end="2026-10-09 10:02:30")

        self.assertEqual(["sh600000", "sh600000"], result.symbol.tolist())
        self.assertEqual([10_000, 8_000], result.volume.tolist())
        self.assertEqual([True, False], result.bar_complete.tolist())
        self.assertEqual("Asia/Shanghai", str(result.timestamp.dt.tz))
        self.assertEqual(
            ["akshare_eastmoney_minute", "akshare_eastmoney_minute"],
            result.source.tolist())
        fake.stock_zh_a_hist_min_em.assert_called_once_with(
            symbol="600000", start_date="2026-10-09 09:30:00",
            end_date="2026-10-09 10:02:30", period="1", adjust="")

    def test_minute_bars_fall_back_to_sina_and_filter_requested_range(self):
        fake = mock.Mock()
        fake.stock_zh_a_hist_min_em.side_effect = RuntimeError(
            "primary unavailable")
        fake.stock_zh_a_minute.return_value = pd.DataFrame({
            "day": ["2026-10-09 09:55:00", "2026-10-09 10:00:00",
                    "2026-10-09 10:05:00"],
            "open": [10.0, 10.1, 10.2], "close": [10.1, 10.2, 10.3],
            "high": [10.1, 10.2, 10.3], "low": [9.9, 10.0, 10.1],
            "volume": [1000, 2000, 3000],
        })
        adapter = AKShareRealtimeMarketData(
            ak_module=fake, retries=1, now=lambda: NOW)

        result = adapter.minute_bars(
            "600000", period="5", start="2026-10-09 10:00:00",
            end="2026-10-09 10:02:30")

        self.assertEqual(1, len(result))
        self.assertEqual("akshare_sina_minute", result.iloc[0].source)
        self.assertEqual(2000, result.iloc[0].volume)
        self.assertTrue(pd.isna(result.iloc[0].amount))
        self.assertIn("eastmoney_failed", adapter.health().last_warning)
        self.assertEqual("akshare_sina_minute", adapter.health().last_provider)
        fake.stock_zh_a_minute.assert_called_once_with(
            symbol="sh600000", period="5", adjust="")

    def test_poll_snapshots_is_bounded_for_jobs_and_tests(self):
        fake = mock.Mock()
        fake.stock_zh_a_spot_em.return_value = eastmoney_snapshot()
        adapter = AKShareRealtimeMarketData(
            ak_module=fake, retries=1, now=lambda: NOW)
        received = []

        polls = adapter.poll_snapshots(
            received.append, symbols=["600000"], interval_seconds=0.01,
            max_polls=2)

        self.assertEqual(2, polls)
        self.assertEqual(2, len(received))


if __name__ == "__main__":
    unittest.main()
