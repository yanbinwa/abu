import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import OperationalStore, StrategyAccountStore, config_sha256


NOW = "2026-10-09T08:45:00+08:00"


class StrategyAccountStoreTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.operational = OperationalStore(
            Path(self.directory.name) / "operational.sqlite3")
        self.registry = StrategyAccountStore(self.operational)

    def tearDown(self):
        self.operational.close()
        self.directory.cleanup()

    def register(self, instance="vcp-a", account="account-a", name="A"):
        self.registry.register_strategy_instance(instance, "vcp", "v2", NOW)
        self.registry.register_account(
            account, name, instance, NOW, initial_cash_micros=100_000_000_000)
        activation, _ = self.registry.activate_strategy(
            "activation-{}-1".format(account), account, {"variant": "residual"},
            20261009, change_reason="INITIAL_SHADOW_ACTIVATION", approved_at=NOW)
        return activation

    def test_equal_configs_hash_identically_and_account_identity_is_stable(self):
        self.assertEqual(config_sha256({"b": 2, "a": 1}),
                         config_sha256({"a": 1, "b": 2}))
        first = self.register()
        self.assertEqual("account-a", first["account_id"])
        second, created = self.registry.register_account(
            "account-a", "A", "vcp-a", NOW,
            initial_cash_micros=100_000_000_000)
        self.assertFalse(created)
        self.assertEqual("account-a", second["account_id"])
        with self.assertRaisesRegex(ValueError, "already bound"):
            self.registry.register_account("account-a", "changed", "vcp-a", NOW)
        with self.assertRaisesRegex(ValueError, "initial cash is immutable"):
            self.registry.register_account(
                "account-a", "A", "vcp-a", NOW, initial_cash_micros=1)

    def test_same_strategy_and_config_have_isolated_accounts(self):
        first = self.register("vcp-a", "account-a", "A")
        second = self.register("vcp-b", "account-b", "B")
        self.assertNotEqual(first["strategy_instance_id"],
                            second["strategy_instance_id"])
        self.assertNotEqual(first["account_id"], second["account_id"])
        self.assertEqual(first["config_sha256"], second["config_sha256"])
        self.assertEqual(100_000_000_000,
                         self.registry.account_view("account-a").cash_micros)
        self.assertEqual(100_000_000_000,
                         self.registry.account_view("account-b").cash_micros)

    def test_activation_upgrade_keeps_account_and_old_trade_owner(self):
        initial = self.register()
        trade, created = self.registry.open_logical_trade(
            "account-a", "trade-1", "sz000001", initial["activation_id"],
            entry_policy_id="vcp-entry", entry_policy_version="2",
            exit_policy_id="vcp-exit", exit_policy_version="2",
            risk_policy_id="risk", risk_policy_version="1", opened_at=NOW)
        self.assertTrue(created)
        self.registry.add_position_lot(
            "lot-1", "account-a", "trade-1", "sz000001", 100,
            20261012, 10_000_000, NOW)

        upgraded, _ = self.registry.activate_strategy(
            "activation-account-a-2", "account-a", {"variant": "residual-v2"},
            20261012, change_reason="CONFIG_UPGRADE", approved_at=NOW)
        view = self.registry.account_view("account-a")
        self.assertEqual("account-a", view.account_id)
        self.assertEqual(upgraded["activation_id"], view.active_activation_id)
        self.assertEqual(initial["activation_id"],
                         view.logical_trades[0]["management_activation_id"])
        self.assertEqual(initial["activation_id"],
                         view.logical_trades[0]["opened_under_activation_id"])
        self.assertEqual(1, len(view.position_lots))
        with self.assertRaises(TypeError):
            view.logical_trades[0]["status"] = "CLOSED"
        self.assertEqual(initial["activation_id"],
                         self.registry.activation_for_session(
                             "account-a", 20261009)["activation_id"])
        self.assertEqual(upgraded["activation_id"],
                         self.registry.activation_for_session(
                             "account-a", 20261012)["activation_id"])

    def test_takeover_is_per_trade_atomic_and_audited(self):
        initial = self.register()
        for trade_id, symbol in (("trade-1", "sz000001"),
                                 ("trade-2", "sz000002")):
            self.registry.open_logical_trade(
                "account-a", trade_id, symbol, initial["activation_id"],
                entry_policy_id="entry", entry_policy_version="1",
                exit_policy_id="old-exit", exit_policy_version="1",
                risk_policy_id="risk", risk_policy_version="1", opened_at=NOW)
        upgraded, _ = self.registry.activate_strategy(
            "activation-account-a-2", "account-a", {"variant": "new"},
            20261012, change_reason="CONFIG_UPGRADE", approved_at=NOW)

        assignment, created = self.registry.takeover_trade_management(
            "account-a", "trade-1", upgraded["activation_id"],
            exit_policy_id="new-exit", exit_policy_version="2",
            effective_from="2026-10-12T08:45:00+08:00",
            reason="EXPLICIT_OPERATOR_APPROVAL", occurred_at=NOW,
            trading_session=20261012)
        self.assertTrue(created)
        self.assertEqual(upgraded["activation_id"], assignment["to_activation_id"])
        view = self.registry.account_view("account-a")
        trades = {row["trade_id"]: row for row in view.logical_trades}
        self.assertEqual(upgraded["activation_id"],
                         trades["trade-1"]["management_activation_id"])
        self.assertEqual(initial["activation_id"],
                         trades["trade-2"]["management_activation_id"])
        event = self.operational.connection.execute(
            "SELECT * FROM domain_events WHERE event_type="
            "'PositionManagementTakenOver'").fetchone()
        self.assertIsNotNone(event)
        self.assertEqual("account-a", event["account_id"])

        duplicate, created = self.registry.takeover_trade_management(
            "account-a", "trade-1", upgraded["activation_id"],
            exit_policy_id="new-exit", exit_policy_version="2",
            effective_from="2026-10-12T08:45:00+08:00",
            reason="EXPLICIT_OPERATOR_APPROVAL", occurred_at=NOW,
            trading_session=20261012)
        self.assertFalse(created)
        self.assertEqual(assignment["assignment_id"], duplicate["assignment_id"])

    def test_activation_boundary_and_cross_account_ownership_fail_closed(self):
        first = self.register("vcp-a", "account-a", "A")
        second = self.register("vcp-b", "account-b", "B")
        with self.assertRaisesRegex(ValueError, "effective session boundary"):
            self.registry.activate_strategy(
                "future", "account-a", {}, 20261012,
                activation_session=20261009, change_reason="BAD",
                approved_at=NOW)
        with self.assertRaisesRegex(ValueError, "does not belong"):
            self.registry.open_logical_trade(
                "account-a", "bad", "sz000001", second["activation_id"],
                entry_policy_id="entry", entry_policy_version="1",
                exit_policy_id="exit", exit_policy_version="1",
                risk_policy_id="risk", risk_policy_version="1", opened_at=NOW)
        self.assertEqual(first["activation_id"],
                         self.registry.account_view(
                             "account-a").active_activation_id)


if __name__ == "__main__":
    unittest.main()
