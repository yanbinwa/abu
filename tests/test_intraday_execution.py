import unittest
from dataclasses import replace

from abupy.AlphaBu.ABuIntradayExecution import (
    IntradayExecutionConfig, IntradayOrderMachine, apply_intraday_outcome,
    instruction_from_order, simulate_intraday_order,
)
from abupy.AlphaBu.ABuTradeIntent import ApprovedOrder
from tests.test_portfolio_executor import intent, make_executor
from abupy.MarketBu.ABuRealtimeMarket import MinuteBarEvent


DATE = "2025-01-03"


def bar(end, opening=10.0, volume=100_000, available_seconds=2,
        high=None, amount=None, source="fixture", revision=1):
    hour, minute = map(int, end.split(":"))
    end_minute = hour * 60 + minute
    start_minute = end_minute - 1
    start = "{:02d}:{:02d}:00".format(start_minute // 60,
                                      start_minute % 60)
    end_clock = end + ":00"
    available = end + ":{:02d}".format(available_seconds)
    high = high if high is not None else opening + 0.1
    return MinuteBarEvent(
        symbol="sz000001", interval_minutes=1,
        source_timestamp="{}T{}+08:00".format(DATE, end_clock),
        bar_start="{}T{}+08:00".format(DATE, start),
        bar_end="{}T{}+08:00".format(DATE, end_clock),
        request_started_at="{}T{}+08:00".format(DATE, available),
        received_at="{}T{}+08:00".format(DATE, available),
        available_at="{}T{}+08:00".format(DATE, available),
        open_raw=opening, high_raw=high,
        low_raw=min(opening - 0.1, opening), close_raw=opening,
        volume_shares=volume, amount_raw=amount, source=source,
        revision=revision, is_complete=True,
        quality_codes=(("AMOUNT_MISSING",) if amount is None else ()),
    )


def approved(quantity=100, max_price=11.0, stop=8.0):
    return ApprovedOrder(
        order_id="order-1", intent_id="intent-1", strategy_id="test",
        strategy_version="1", symbol="sz000001", side="buy",
        quantity=quantity, created_asof=20250102, valid_session=20250103,
        max_buy_price_raw=max_price, initial_stop_raw=stop,
    )


class IntradayExecutionTest(unittest.TestCase):

    def test_m1_uses_bar_ending_0935_and_next_eligible_open(self):
        order = approved()
        config = IntradayExecutionConfig(slippage_bps=0)
        instruction = instruction_from_order(order, 20250103, "M1", config)
        outcome = simulate_intraday_order(
            instruction, order, [bar("09:35"), bar("09:37", opening=10.2)],
            config=config, upper_limit_raw=11.0)
        self.assertEqual("FILLED", outcome.state)
        self.assertEqual(10.2, outcome.fill_price_raw)
        self.assertTrue(outcome.trigger_bar_end.endswith("09:35:00+08:00"))
        self.assertTrue(outcome.candidate_bar_start.endswith("09:36:00+08:00"))

    def test_m2_capacity_uses_completed_reference_not_candidate_volume(self):
        order = approved(quantity=1000)
        config = IntradayExecutionConfig(
            slippage_bps=0, max_volume_participation=0.05)
        instruction = instruction_from_order(order, 20250103, "M2", config)
        reference = bar("09:35", volume=25_000)
        candidate = bar("09:37", opening=10.2, volume=1)
        first = simulate_intraday_order(
            instruction, order, [reference, candidate], config=config)
        second = simulate_intraday_order(
            instruction, order, [reference, replace(candidate,
                volume_shares=999_999)], config=config)
        self.assertEqual("FILLED", first.state)
        self.assertEqual(first, second)
        self.assertTrue(first.capacity_reference_bar_end.endswith(
            "09:35:00+08:00"))

    def test_m2_waits_for_later_completed_reference(self):
        order = approved(quantity=1000)
        config = IntradayExecutionConfig(
            slippage_bps=0, max_volume_participation=0.05)
        instruction = instruction_from_order(order, 20250103, "M2", config)
        outcome = simulate_intraday_order(
            instruction, order, [
                bar("09:35", volume=10_000),
                bar("09:36", volume=25_000),
                bar("09:38", opening=10.3, volume=1),
            ], config=config)
        self.assertEqual("FILLED", outcome.state)
        self.assertTrue(outcome.capacity_reference_bar_end.endswith(
            "09:36:00+08:00"))
        self.assertTrue(outcome.candidate_bar_start.endswith(
            "09:37:00+08:00"))

    def test_candidate_after_1030_is_never_filled(self):
        order = approved()
        config = IntradayExecutionConfig(slippage_bps=0)
        instruction = instruction_from_order(order, 20250103, "M2", config)
        outcome = simulate_intraday_order(
            instruction, order, [bar("10:29", volume=100_000),
                                 bar("10:32", volume=100_000)],
            config=config)
        self.assertEqual("EXPIRED", outcome.state)
        self.assertEqual("EXECUTION_WINDOW_EXPIRED", outcome.reason_code)

    def test_candidate_starting_at_1030_is_allowed(self):
        order = approved()
        config = IntradayExecutionConfig(slippage_bps=0)
        instruction = instruction_from_order(order, 20250103, "M2", config)
        outcome = simulate_intraday_order(
            instruction, order, [bar("10:29", volume=100_000),
                                 bar("10:31", opening=10.2)],
            config=config)
        self.assertEqual("FILLED", outcome.state)
        self.assertTrue(outcome.candidate_bar_start.endswith(
            "10:30:00+08:00"))

    def test_limit_base_and_pessimistic_are_distinct(self):
        order = approved()
        bars = [bar("09:35"), bar("09:37", opening=10.2, high=11.0)]
        base = IntradayExecutionConfig(slippage_bps=0, limit_up_mode="base")
        pessimistic = replace(base, limit_up_mode="pessimistic")
        base_result = simulate_intraday_order(
            instruction_from_order(order, 20250103, "M1", base), order,
            bars, config=base, upper_limit_raw=11.0)
        pessimistic_result = simulate_intraday_order(
            instruction_from_order(order, 20250103, "M1", pessimistic), order,
            bars, config=pessimistic, upper_limit_raw=11.0)
        self.assertEqual("FILLED", base_result.state)
        self.assertEqual("EXPIRED", pessimistic_result.state)
        self.assertEqual("CANDIDATE_TOUCHED_LIMIT_UP",
                         pessimistic_result.reason_code)

    def test_corporate_action_cancels_without_reading_bars(self):
        order = approved()
        instruction = instruction_from_order(order, 20250103, "M1")
        outcome = simulate_intraday_order(
            instruction, order, [bar("09:35"), bar("09:36")],
            corporate_action=True)
        self.assertEqual("CANCELLED", outcome.state)
        self.assertEqual("CORPORATE_ACTION_REAPPROVAL_REQUIRED",
                         outcome.reason_code)

    def test_outcome_commits_through_shared_executor(self):
        executor = make_executor(slippage=0)
        order, _ = executor.approve_order(
            intent(suffix="minute"), 100, 20250103, 11.0, 2.0)
        executor.process_open_sells(1)
        config = IntradayExecutionConfig(slippage_bps=0)
        instruction = instruction_from_order(order, 20250103, "M1", config)
        outcome = simulate_intraday_order(
            instruction, order, [bar("09:35"), bar("09:37", opening=10.2)],
            config=config)
        fill = apply_intraday_outcome(executor, order, 1, outcome)
        self.assertEqual("filled", fill.status)
        self.assertEqual("M1", fill.execution_policy_id)
        self.assertEqual(0, executor.reserved_cash)

    def test_machine_state_restores_without_replaying_consumed_bars(self):
        order = approved()
        config = IntradayExecutionConfig(slippage_bps=0)
        instruction = instruction_from_order(order, 20250103, "M1", config)
        reference = bar("09:35")
        candidate = bar("09:37", opening=10.2)

        machine = IntradayOrderMachine(instruction, order, config=config)
        machine.on_bar(reference)
        self.assertEqual("CANDIDATE", machine.state)
        restored = IntradayOrderMachine.restore(
            instruction, order, machine.export_state(), config=config)
        restored.on_bar(candidate)

        uninterrupted = IntradayOrderMachine(
            instruction, order, config=config)
        uninterrupted.on_bar(reference)
        uninterrupted.on_bar(candidate)
        self.assertEqual(uninterrupted.outcome(), restored.outcome())
        self.assertEqual("FILLED", restored.state)

    def test_machine_restore_rejects_corrupt_state(self):
        order = approved()
        instruction = instruction_from_order(order, 20250103, "M1")
        machine = IntradayOrderMachine(instruction, order)
        state = machine.export_state()
        state["events"][0]["sequence"] = 2
        with self.assertRaisesRegex(ValueError, "sequence is not contiguous"):
            IntradayOrderMachine.restore(instruction, order, state)


if __name__ == "__main__":
    unittest.main()
