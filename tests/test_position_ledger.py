# -*- encoding: utf-8 -*-
import unittest

from abupy.AlphaBu.ABuPositionLedger import (
    FillAllocation, LogicalTrade, PhysicalPosition, PositionLot,
    evaluation_id, logical_add_order_id, migrate_legacy_position, proposal_id,
)
from abupy.AlphaBu.ABuTradeIntent import ApprovedOrder, Position, TradeIntent


class PositionLedgerSchemaTest(unittest.TestCase):

    def test_explicit_side_effect_combinations(self):
        for side, effect in (("buy", "OPEN"), ("buy", "INCREASE"),
                             ("sell", "REDUCE"), ("sell", "CLOSE")):
            TradeIntent("i-" + effect, "s", "1", 20250101, "sz000001",
                        side=side, position_effect=effect)
        with self.assertRaises(ValueError):
            TradeIntent("bad", "s", "1", 20250101, "sz000001",
                        side="sell", position_effect="INCREASE")

    def test_layered_ids_are_stable_and_add_order_is_policy_independent(self):
        first = evaluation_id("t", 20250101, "p1", "1")
        self.assertEqual(first, evaluation_id("t", 20250101, "p1", "1"))
        self.assertNotEqual(first, evaluation_id("t", 20250101, "p2", "1"))
        self.assertNotEqual(proposal_id(first, 1), proposal_id(first, 2))
        self.assertEqual(logical_add_order_id("t", 20250101, 1),
                         logical_add_order_id("t", 20250101, 1))

    def test_legacy_position_migration_preserves_quantity_and_book_cost(self):
        legacy = Position(
            symbol="sz000001", quantity=300, entry_date=20250102,
            entry_price_raw=10.0, total_cost=3006.0, strategy_id="vcp",
            strategy_version="1", initial_stop_raw=9.0,
            current_stop_raw=9.5, initial_r_per_share_raw=1.0,
            initial_r_cash_frozen=300.0,
        )
        migrated = migrate_legacy_position(legacy)
        self.assertEqual(migrated.physical_position.total_quantity, 300)
        self.assertEqual(migrated.position_lot.quantity_remaining, 300)
        self.assertEqual(migrated.physical_position.remaining_book_cost_cash,
                         3006.0)
        self.assertEqual(migrated.position_lot.remaining_book_cost_cash, 3006.0)
        self.assertEqual(migrated.logical_trade.current_stop_raw, 9.5)

    def test_schema_invariants_fail_closed(self):
        with self.assertRaises(ValueError):
            PhysicalPosition("x", 10, 9, 2, 100.0)
        with self.assertRaises(ValueError):
            LogicalTrade("t", "GLOBAL", "x", "s", "1", "i", "UNKNOWN")
        with self.assertRaises(TypeError):
            PhysicalPosition(symbol="x", total_quantity=1, sellable_quantity=1,
                             reserved_sell_quantity=0,
                             remaining_book_cost_cash=1.0, unknown=True)


if __name__ == "__main__":
    unittest.main()
