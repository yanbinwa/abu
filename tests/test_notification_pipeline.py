import tempfile
import unittest
from pathlib import Path

from abupy.ServiceBu import OperationalStore
from abupy.ServiceBu.ABuMockPaperScenario import MockPaperTradingScenario
from abupy.ServiceBu.ABuNotificationPipeline import (
    DeliveryUnknown, NotificationWorker, RetryableDeliveryError,
)


NOW = "2026-10-09T15:20:00+08:00"


class MockTransport(object):
    def __init__(self, failures=None):
        self.failures = dict(failures or {})
        self.sent = []

    def send(self, kind, path, key):
        self.sent.append((kind, key, Path(path).suffix))
        failure = self.failures.pop(kind, None)
        if failure:
            raise failure
        return "mock://" + key


class NotificationPipelineTest(unittest.TestCase):

    def setup_scenario(self, directory):
        result = MockPaperTradingScenario(
            Path(directory), deliver_notifications=False).run()
        return result, OperationalStore(
            result["database_path"], target_schema_version=4)

    def test_text_and_chart_render_and_send_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            unused_result, store = self.setup_scenario(directory)
            transport = MockTransport()
            worker = NotificationWorker(
                store, Path(directory) / "artifacts", transport)
            outcomes = worker.drain_once(NOW)
            self.assertEqual({"SENT"}, {item["status"] for item in outcomes})
            self.assertEqual(2, len(transport.sent))
            self.assertEqual("SENT", store.connection.execute(
                "SELECT status FROM notification_outbox").fetchone()[0])
            self.assertEqual(2, store.connection.execute(
                "SELECT count(*) FROM render_artifacts").fetchone()[0])
            store.close()

    def test_unknown_is_retried_without_resending_sent_text(self):
        with tempfile.TemporaryDirectory() as directory:
            unused_result, store = self.setup_scenario(directory)
            transport = MockTransport({
                "CHART_IMAGE": DeliveryUnknown("remote outcome unknown")})
            worker = NotificationWorker(
                store, Path(directory) / "artifacts", transport)
            first = worker.drain_once(NOW)
            self.assertEqual({"SENT", "UNKNOWN"}, {
                item["status"] for item in first})
            worker.recover_unknown("2026-10-09T15:21:00+08:00")
            second = worker.drain_once("2026-10-09T15:21:00+08:00")
            self.assertEqual(["CHART_IMAGE"], [item["part_kind"] for item in second])
            self.assertEqual("SENT", store.connection.execute(
                "SELECT status FROM notification_outbox").fetchone()[0])
            store.close()

    def test_retry_threshold_and_operator_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            unused_result, store = self.setup_scenario(directory)
            transport = MockTransport({
                "TEXT": RetryableDeliveryError("temporary")})
            worker = NotificationWorker(
                store, Path(directory) / "artifacts", transport,
                max_attempts=1)
            worker.drain_once(NOW)
            row = store.connection.execute(
                "SELECT notification_event_id,status FROM notification_parts "
                "WHERE part_kind='TEXT'").fetchone()
            self.assertEqual("REQUIRES_ATTENTION", row["status"])
            worker.operator_action(
                row["notification_event_id"], "TEXT", "retry",
                "2026-10-09T15:22:00+08:00", "operator verified transport",
                "tester")
            worker.drain_once("2026-10-09T15:22:00+08:00")
            self.assertEqual("SENT", store.connection.execute(
                "SELECT status FROM notification_parts WHERE part_kind='TEXT'"
            ).fetchone()[0])
            self.assertEqual(1, store.connection.execute(
                "SELECT count(*) FROM notification_operator_actions"
            ).fetchone()[0])
            store.close()


if __name__ == "__main__":
    unittest.main()
