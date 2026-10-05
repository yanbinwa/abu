import sqlite3
import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu.ABuDashboard import DashboardQuery, StaticDashboardRenderer
from abupy.ServiceBu.ABuMockPaperScenario import MockPaperTradingScenario


class PaperDashboardTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.result = MockPaperTradingScenario(self.root).run()
        self.database = Path(self.result["database_path"])

    def tearDown(self):
        self.directory.cleanup()

    def test_query_handle_rejects_account_writes(self):
        with DashboardQuery(self.database) as query:
            self.assertEqual(1, len(query.accounts()))
            with self.assertRaises(sqlite3.OperationalError):
                query.connection.execute(
                    "UPDATE accounts SET status='PAUSED'")

    def test_account_view_contains_trade_audit_chain(self):
        with DashboardQuery(self.database) as query:
            detail = query.account("mock-account")
            self.assertEqual(1, len(detail["positions"]))
            self.assertEqual(1, len(detail["fills"]))
            self.assertEqual(1, len(detail["risk_decisions"]))
            self.assertEqual("PASSED", detail["reconciliations"][0]["status"])
            self.assertEqual("SENT", detail["notifications"][0]["status"])

    def test_static_pages_are_rebuildable_and_escape_values(self):
        output = self.root / "dashboard"
        with DashboardQuery(self.database) as query:
            with query.connection:
                pass
            first = StaticDashboardRenderer().build(query, output)
            (output / "index.html").unlink()
            second = StaticDashboardRenderer().build(query, output)
        self.assertEqual(first, second)
        index = (output / "index.html").read_text(encoding="utf-8")
        account = (output / "accounts" /
                   StaticDashboardRenderer._account_page(
                       "mock-account")).read_text(
            encoding="utf-8")
        self.assertIn("只读控制台", index)
        self.assertIn("mock-account", index)
        self.assertIn("risk_decisions", account)
        self.assertNotIn("<script>", account)

    def test_account_page_name_cannot_escape_output_directory(self):
        filename = StaticDashboardRenderer._account_page("../../outside")
        self.assertNotIn("/", filename)
        self.assertTrue(filename.startswith("account-"))

    def test_unknown_account_fails_closed(self):
        with DashboardQuery(self.database) as query:
            with self.assertRaises(KeyError):
                query.account("missing")


if __name__ == "__main__":
    unittest.main()
