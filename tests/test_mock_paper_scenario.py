import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu.ABuMockPaperScenario import MockPaperTradingScenario


class MockPaperTradingScenarioTest(unittest.TestCase):

    def test_one_command_runs_fill_close_projection_and_outbox(self):
        with tempfile.TemporaryDirectory() as directory:
            result = MockPaperTradingScenario(Path(directory)).run()
            self.assertEqual(1, result["fills"])
            self.assertEqual(1, result["notifications"])
            self.assertEqual("SENT", result["notification_status"])
            self.assertEqual({"TEXT", "CHART_IMAGE"}, {
                item["part_kind"] for item in result["mock_messages"]})
            self.assertTrue(all(Path(item["path"]).is_file()
                                for item in result["mock_messages"]))
            self.assertEqual("PASSED", result["daily_close"])
            self.assertEqual(9, result["account_version"])
            self.assertTrue(Path(result["projection"]["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
