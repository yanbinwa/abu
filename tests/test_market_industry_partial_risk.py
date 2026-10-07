from types import SimpleNamespace

import numpy as np

from abupy.AlphaBu.ABuMarketIndustryExit import (
    MarketIndustryExitConfig,
    MarketIndustryPartialRiskConfig,
    MarketIndustryPartialRiskOverlay,
)


class _Panel(object):
    dates = np.asarray([20200102, 20200103, 20200106])


class _Context(object):
    def __init__(self, panel, market=None, industry=None):
        self.panel = panel
        self._market = market or {}
        self._industry = industry or {}

    def market_signal(self, day):
        return self._market.get(int(day))

    def industry_signal(self, day, symbol):
        return self._industry.get((int(day), symbol), (None, 7))


def _overlay(market=None, industry=None, entry=True, partial=True):
    panel = _Panel()
    return MarketIndustryPartialRiskOverlay(
        panel, MarketIndustryExitConfig(), MarketIndustryPartialRiskConfig(),
        context=_Context(panel, market, industry),
        enable_entry_block=entry, enable_partial_reduction=partial)


def test_market_event_blocks_entries_and_has_priority_for_reduction():
    overlay = _overlay(
        market={1: "MARKET_RETREAT_EXIT"},
        industry={(1, "sz000001"): ("INDUSTRY_RETREAT_EXIT", 4)})
    overlay.register_entry("sz000001", 900)
    assert overlay.entry_block_reason(1) == "MARKET_RETREAT_EXIT"
    decision = overlay.evaluate(
        1, "sz000001", 900, SimpleNamespace(trailing_enabled=True))
    assert decision == {
        "reason": "MARKET_RETREAT_REDUCE_50",
        "context_reason": "MARKET_RETREAT_EXIT",
        "quantity": 400,
        "position_effect": "REDUCE",
        "industry_id": -1,
    }


def test_industry_event_reduces_once_and_preserves_board_lot():
    overlay = _overlay(
        industry={(1, "sz000001"): ("INDUSTRY_EXHAUSTION_EXIT", 4)})
    overlay.register_entry("sz000001", 700)
    decision = overlay.evaluate(
        1, "sz000001", 700, SimpleNamespace(trailing_enabled=True))
    assert decision["quantity"] == 300
    overlay.record_fill("sz000001", decision["reason"], 300)
    assert overlay.evaluate(
        2, "sz000001", 400, SimpleNamespace(trailing_enabled=True)) is None


def test_no_reduction_before_one_r_or_when_one_lot_would_not_remain():
    overlay = _overlay(
        market={1: "MARKET_EXHAUSTION_EXIT", 2: "MARKET_RETREAT_EXIT"})
    overlay.register_entry("sz000001", 100)
    assert overlay.evaluate(
        1, "sz000001", 100, SimpleNamespace(trailing_enabled=True)) is None
    assert overlay.evaluate(
        2, "sz000001", 500, SimpleNamespace(trailing_enabled=False)) is None


def test_ablation_switches_are_independent():
    entry_only = _overlay(
        market={1: "MARKET_RETREAT_EXIT"}, entry=True, partial=False)
    entry_only.register_entry("sz000001", 500)
    assert entry_only.entry_block_reason(1) == "MARKET_RETREAT_EXIT"
    assert entry_only.evaluate(
        1, "sz000001", 500, SimpleNamespace(trailing_enabled=True)) is None

    partial_only = _overlay(
        market={1: "MARKET_RETREAT_EXIT"}, entry=False, partial=True)
    partial_only.register_entry("sz000001", 500)
    assert partial_only.entry_block_reason(1) is None
    assert partial_only.evaluate(
        1, "sz000001", 500,
        SimpleNamespace(trailing_enabled=True))["quantity"] == 200
