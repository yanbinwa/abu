import unittest
from unittest import mock

import pandas as pd

from abupy.MarketBu.ABuRealtimeMarket import (
    AKShareRealtimeMarketData, FailoverMinuteMarketData, MarketDataHealth,
    MinuteBarEvent, RealtimeMarketDataError, TencentMinuteMarketData,
    normalize_cn_symbol,
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
        self.assertEqual([True, True], result.bar_complete.tolist())
        self.assertEqual("Asia/Shanghai", str(result.timestamp.dt.tz))
        self.assertEqual(
            pd.Timestamp("2026-10-09 10:01:00", tz="Asia/Shanghai"),
            result.iloc[1].bar_start)
        self.assertEqual(result.iloc[1].received_at,
                         result.iloc[1].available_at)
        self.assertLessEqual(result.iloc[1].request_started_at,
                             result.iloc[1].received_at)
        self.assertEqual(
            ["akshare_eastmoney_minute", "akshare_eastmoney_minute"],
            result.source.tolist())
        self.assertIsNotNone(adapter.health("sh600000"))
        self.assertTrue(adapter.health("sh600000").data_present)
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

    def test_minute_bars_empty_after_range_filter_fails_closed(self):
        fake = mock.Mock()
        fake.stock_zh_a_hist_min_em.return_value = pd.DataFrame({
            "时间": ["2026-10-09 09:31:00"],
            "开盘": [10.0], "收盘": [10.0], "最高": [10.0],
            "最低": [10.0], "成交量": [100], "成交额": [1000],
        })
        adapter = AKShareRealtimeMarketData(
            ak_module=fake, retries=1, fallback=False, now=lambda: NOW)
        with self.assertRaisesRegex(RealtimeMarketDataError, "no rows"):
            adapter.minute_bars(
                "600000", start="2026-10-09 10:00:00",
                end="2026-10-09 10:01:00")
        health = adapter.health()
        self.assertFalse(health.data_present)
        self.assertIn("NO_DATA", health.reason_codes)
        self.assertIn("NO_DATA", adapter.health("sh600000").reason_codes)

    def test_minute_raw_response_archive_runs_before_normalization(self):
        fake = mock.Mock()
        raw = pd.DataFrame({
            "时间": ["2026-10-09 10:00:00"],
            "开盘": [10.0], "收盘": [10.0], "最高": [10.1],
            "最低": [9.9], "成交量": [100], "成交额": [1000],
        })
        fake.stock_zh_a_hist_min_em.return_value = raw
        archived = []
        adapter = AKShareRealtimeMarketData(
            ak_module=fake, retries=1, now=lambda: NOW,
            raw_archive=lambda **payload: archived.append(payload))
        adapter.minute_bars(
            "600000", start="2026-10-09 09:30:00",
            end="2026-10-09 10:01:00")
        self.assertEqual(1, len(archived))
        self.assertIs(archived[0]["raw"], raw)
        self.assertEqual("akshare_eastmoney_minute",
                         archived[0]["provider"])

    def test_minute_event_rejects_invalid_ohlc_and_reversed_clock(self):
        common = dict(
            symbol="sh600000", interval_minutes=1,
            source_timestamp="2026-10-09T09:35:00+08:00",
            bar_start="2026-10-09T09:34:00+08:00",
            bar_end="2026-10-09T09:35:00+08:00",
            request_started_at="2026-10-09T09:35:01+08:00",
            received_at="2026-10-09T09:35:02+08:00",
            available_at="2026-10-09T09:35:02+08:00",
            open_raw=10.0, high_raw=10.2, low_raw=9.9, close_raw=10.1,
            volume_shares=1000, amount_raw=None,
            source="fixture",
        )
        self.assertEqual(("AMOUNT_MISSING",), MinuteBarEvent(
            **common, quality_codes=("AMOUNT_MISSING",)).quality_codes)
        with self.assertRaisesRegex(ValueError, "OHLC"):
            MinuteBarEvent(**{**common, "high_raw": 10.0})
        with self.assertRaisesRegex(ValueError, "reversed"):
            MinuteBarEvent(**{
                **common,
                "received_at": "2026-10-09T09:35:00+08:00",
            })

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


class _Response(object):
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        return None


class TencentMinuteMarketDataTest(unittest.TestCase):

    def test_normalizes_lots_and_archives_before_filtering(self):
        session = mock.Mock()
        session.get.return_value = _Response({
            "code": 0,
            "data": {"sh600000": {"m1": [
                ["202610091000", "10.00", "10.10", "10.20", "9.90", "123"],
                ["202610091002", "10.10", "10.15", "10.20", "10.05", "45"],
            ]}},
        })
        archived = []
        adapter = TencentMinuteMarketData(
            session=session, retries=1, now=lambda: NOW,
            raw_archive=lambda **payload: archived.append(payload))

        frame = adapter.minute_bars(
            "600000", start="2026-10-09 10:00:00",
            end="2026-10-09 10:02:30")

        self.assertEqual([12_300.0, 4_500.0], frame.volume_shares.tolist())
        self.assertEqual("tencent_mkline_minute", frame.iloc[0].source)
        self.assertTrue(pd.isna(frame.iloc[0].amount_raw))
        self.assertIn("AMOUNT_MISSING", frame.iloc[0].quality_codes)
        self.assertEqual(1, len(archived))
        self.assertEqual("tencent_mkline_minute", archived[0]["provider"])
        self.assertTrue(adapter.health("sh600000").data_fresh)
        session.get.assert_called_once()

    def test_failover_uses_whole_fallback_frame_and_opens_on_429(self):
        failure = RealtimeMarketDataError("limited")
        failure.reason_code = "HTTP_429"
        primary = mock.Mock()
        primary.minute_bars.side_effect = failure
        primary.health.return_value = None
        fallback = mock.Mock()
        fallback.raw_archive = None
        frame = pd.DataFrame({"source": ["akshare_sina_minute"]})
        fallback.minute_bars.return_value = frame
        fallback.health.return_value = MarketDataHealth(
            source="sina", connected=True,
            last_provider="akshare_sina_minute",
            last_success_at=NOW.isoformat(), last_error=None,
            last_warning=None, consecutive_failures=0,
            last_latency_ms=1.0, last_record_count=1,
            transport_ok=True, data_present=True, data_fresh=True,
            fields_valid=True)
        router = FailoverMinuteMarketData(
            primary, fallback, failure_threshold=3,
            circuit_breaker_seconds=900, max_fallback_per_cycle=2,
            monotonic=lambda: 100.0)
        router.begin_cycle()

        first = router.minute_bars("sh600000")
        second = router.minute_bars("sh600000")

        self.assertIs(first, frame)
        self.assertIs(second, frame)
        self.assertEqual(1, primary.minute_bars.call_count)
        self.assertEqual(2, fallback.minute_bars.call_count)
        self.assertIn("primary_failed", router.health("sh600000").last_warning)

    def test_failover_quota_fails_closed(self):
        failure = RealtimeMarketDataError("down")
        failure.reason_code = "PROVIDER_ERROR"
        primary = mock.Mock()
        primary.minute_bars.side_effect = failure
        primary.health.return_value = None
        fallback = mock.Mock()
        fallback.raw_archive = None
        fallback.minute_bars.side_effect = failure
        router = FailoverMinuteMarketData(
            primary, fallback, failure_threshold=3,
            max_fallback_per_cycle=0)
        router.begin_cycle()
        with self.assertRaisesRegex(RealtimeMarketDataError, "not admitted"):
            router.minute_bars("sh600000")
        fallback.minute_bars.assert_not_called()


if __name__ == "__main__":
    unittest.main()
