import unittest

from abupy.AlphaBu.ABuIntradayExecution import IntradayExecutionConfig
from abupy.AlphaBu.ABuVCPStrategy import run_vcp_backtest
from abupy.MarketBu.ABuRealtimeMarket import MinuteBarEvent
from tests.test_vcp_strategy import make_vcp_panel


def minute_bar(trade_date, symbol, end, opening=10.0, volume=1_000_000):
    text = str(trade_date)
    date = "{}-{}-{}".format(text[:4], text[4:6], text[6:])
    hour, minute = map(int, end.split(":"))
    start_total = hour * 60 + minute - 1
    start = "{:02d}:{:02d}:00".format(start_total // 60,
                                      start_total % 60)
    end_clock = end + ":00"
    available = end + ":02"
    return MinuteBarEvent(
        symbol=symbol, interval_minutes=1,
        source_timestamp="{}T{}+08:00".format(date, end_clock),
        bar_start="{}T{}+08:00".format(date, start),
        bar_end="{}T{}+08:00".format(date, end_clock),
        request_started_at="{}T{}+08:00".format(date, available),
        received_at="{}T{}+08:00".format(date, available),
        available_at="{}T{}+08:00".format(date, available),
        open_raw=opening, high_raw=opening + 0.1,
        low_raw=opening - 0.1, close_raw=opening,
        volume_shares=volume, amount_raw=None, source="fixture",
        is_complete=True, quality_codes=("AMOUNT_MISSING",),
    )


def minute_loader(trade_date, symbol):
    return [
        minute_bar(trade_date, symbol, "09:35", volume=1_000_000),
        minute_bar(trade_date, symbol, "09:37", opening=10.5,
                   volume=1_000_000),
    ]


class IntradayBacktestTest(unittest.TestCase):

    def test_d0_remains_default_and_m1_uses_independent_ledger(self):
        panel, _ = make_vcp_panel()
        default = run_vcp_backtest(
            panel, 2024, "c_core_fixed20", slippage_bps=0)
        explicit = run_vcp_backtest(
            panel, 2024, "c_core_fixed20", slippage_bps=0,
            execution_policy_id="D0")
        self.assertEqual(default[0], explicit[0])
        self.assertTrue(default[1].equals(explicit[1]))
        self.assertTrue(default[2].equals(explicit[2]))

        m1 = run_vcp_backtest(
            panel, 2024, "c_core_fixed20", slippage_bps=0,
            execution_policy_id="M1", minute_bars=minute_loader,
            intraday_execution_config=IntradayExecutionConfig(
                slippage_bps=0))
        self.assertEqual("M1", m1[0]["execution_policy_id"])
        buys = m1[2][(m1[2].side == "buy") & (m1[2].status == "filled")]
        self.assertGreaterEqual(len(buys), 1)
        self.assertTrue((buys.execution_policy_id == "M1").all())

    def test_missing_minute_data_fails_closed_without_position(self):
        panel, _ = make_vcp_panel()
        result, _, fills, _, _ = run_vcp_backtest(
            panel, 2024, "c_core_fixed20", slippage_bps=0,
            execution_policy_id="M2",
            minute_bars=lambda date, symbol: [])
        self.assertEqual(0, result["filled_buys"])
        rejected = fills[(fills.side == "buy") &
                         (fills.reason_code == "NO_MINUTE_DATA")]
        self.assertGreaterEqual(len(rejected), 1)
        self.assertTrue((rejected.execution_policy_id == "M2").all())

    def test_audit_contains_intraday_order_events(self):
        panel, _ = make_vcp_panel()
        audit = {}
        run_vcp_backtest(
            panel, 2024, "c_core_fixed20", slippage_bps=0,
            execution_policy_id="M1", minute_bars=minute_loader,
            audit=audit,
            intraday_execution_config=IntradayExecutionConfig(
                slippage_bps=0))
        self.assertIn("intraday_order_events", audit)
        self.assertTrue(audit["intraday_order_events"])


if __name__ == "__main__":
    unittest.main()
