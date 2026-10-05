import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import OperationalStore, StrategyAccountStore
from abupy.ServiceBu.ABuAccountProjection import AccountProjectionExporter


NOW = "2026-10-12T18:30:00+08:00"


class AccountProjectionExporterTest(unittest.TestCase):

    def test_build_and_export_are_read_only_deterministic_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationalStore(
                root / "state.sqlite3", target_schema_version=3)
            registry = StrategyAccountStore(store)
            registry.register_strategy_instance("strategy-a", "fixture", "1", NOW)
            registry.register_account(
                "account-a", "A", "strategy-a", NOW,
                initial_cash_micros=1_000_000)
            registry.activate_strategy(
                "activation-a", "account-a", {"v": 1}, 20261012,
                change_reason="test", approved_at=NOW)
            exporter = AccountProjectionExporter(store, root / "exports")
            before = store.connection.total_changes
            first = exporter.build("account-a", NOW)
            second = exporter.build("account-a", NOW)
            self.assertEqual(first, second)
            self.assertEqual(before, store.connection.total_changes)
            self.assertEqual([], first["orders"])
            self.assertEqual(1_000_000, first["balance"]["cash_micros"])
            artifact = exporter.export("account-a", NOW)
            repeated = exporter.export("account-a", NOW)
            self.assertTrue(artifact["created"])
            self.assertFalse(repeated["created"])
            self.assertEqual(artifact["path"], repeated["path"])
            self.assertTrue(Path(artifact["path"]).is_file())
            self.assertEqual(0, store.connection.execute(
                "SELECT count(*) FROM account_events").fetchone()[0])
            store.close()

    def test_unknown_account_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationalStore(
                Path(directory) / "state.sqlite3", target_schema_version=3)
            with self.assertRaises(KeyError):
                AccountProjectionExporter(store).build("missing", NOW)
            store.close()


if __name__ == "__main__":
    unittest.main()
