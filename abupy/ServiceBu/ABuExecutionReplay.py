from __future__ import absolute_import

from dataclasses import asdict, dataclass

from ..AlphaBu.ABuIntradayExecution import (
    IntradayExecutionConfig, IntradayOrderMachine, instruction_from_order,
)
from ..AlphaBu.ABuTradeIntent import ApprovedOrder
from .ABuContentStore import sha256_json


@dataclass(frozen=True)
class D0OpenInput:
    opening_raw: float
    buy_tradable: bool = True
    upper_limit_raw: float | None = None


class ExecutionReplayHarness(object):
    """Pure fixed-input D0/M1/M2 paired replay with no operational writes."""

    def __init__(self, config=None):
        self.config = config or IntradayExecutionConfig()

    @staticmethod
    def _d0(order, opening):
        if not opening.buy_tradable:
            return {"state": "REJECTED", "reason_code": "NOT_BUY_TRADABLE"}
        if opening.opening_raw <= 0:
            return {"state": "REJECTED", "reason_code": "INVALID_OPEN"}
        if (opening.upper_limit_raw is not None and
                opening.opening_raw >= opening.upper_limit_raw - 1e-12):
            return {"state": "REJECTED", "reason_code": "OPEN_AT_LIMIT_UP"}
        if opening.opening_raw > float(order.max_buy_price_raw):
            return {"state": "REJECTED", "reason_code": "ABOVE_MAX_BUY_PRICE"}
        return {
            "state": "FILLED", "reason_code": "D0_OPEN_FILLED",
            "reference_price_raw": float(opening.opening_raw),
            "fill_price_raw": float(opening.opening_raw),
            "quantity": int(order.quantity),
        }

    def _intraday(self, order, trading_session, batches, policy_id,
                  upper_limit_raw):
        instruction = instruction_from_order(
            order, trading_session, policy_id, self.config,
            created_at="replay", reservation_id="replay")
        machine = IntradayOrderMachine(
            instruction, order, config=self.config,
            upper_limit_raw=upper_limit_raw)
        previous = None
        expected_sequence = 1
        consumed = []
        for batch in batches:
            if int(batch.sequence_no) != expected_sequence:
                raise ValueError("replay snapshot sequence gap")
            if batch.previous_snapshot_id != previous:
                raise ValueError("replay snapshot predecessor mismatch")
            for event in batch.events_by_symbol.get(order.symbol, ()):
                machine.on_bar(event)
            consumed.append(batch.snapshot_id)
            previous = batch.snapshot_id
            expected_sequence += 1
            if machine.state in ("FILLED", "CANCELLED", "EXPIRED"):
                break
        if machine.state not in ("FILLED", "CANCELLED", "EXPIRED"):
            machine.finalize()
        outcome = machine.outcome()
        return {
            "state": outcome.state,
            "reason_code": outcome.reason_code,
            "reference_price_raw": outcome.reference_price,
            "fill_price_raw": outcome.fill_price_raw,
            "quantity": int(order.quantity) if outcome.state == "FILLED" else 0,
            "consumed_snapshot_ids": consumed,
            "terminal_state": machine.export_state(),
        }

    def replay_order(self, order, trading_session, batches, d0_open):
        if not isinstance(order, ApprovedOrder) or order.side != "buy":
            raise TypeError("replay requires one approved buy order")
        frozen_batches = tuple(batches)
        upper = d0_open.upper_limit_raw
        result = {
            "order_id": order.order_id,
            "symbol": order.symbol,
            "D0": self._d0(order, d0_open),
            "M1": self._intraday(
                order, trading_session, frozen_batches, "M1", upper),
            "M2": self._intraday(
                order, trading_session, frozen_batches, "M2", upper),
        }
        result["result_sha256"] = sha256_json(result)
        return result

    def replay_portfolio(self, cases):
        results = []
        for case in sorted(cases, key=lambda item: item["order"].order_id):
            results.append(self.replay_order(
                case["order"], case["trading_session"], case["batches"],
                case["d0_open"]))
        report = {
            "schema_version": "execution_replay_report_v1",
            "config": asdict(self.config),
            "orders": results,
        }
        report["report_sha256"] = sha256_json(report)
        return report
