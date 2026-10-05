import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from abupy.ServiceBu import DailyShadowSnapshotJob, OperationalStore


ROOT = Path(__file__).resolve().parents[1]


class DailyShadowSnapshotJobTest(unittest.TestCase):

    def _source_config(self, root):
        files = {}
        for name in (
                "universe", "calendar", "security_master", "raw_price",
                "adjusted_price", "corporate_action", "limit_reference"):
            path = root / (name + ".txt")
            path.write_text(name, encoding="utf-8")
            files[name] = str(path)
        config = root / "sources.json"
        config.write_text(json.dumps({"components": [{
            "component_id": name, "source": "test", "patterns": [path]
        } for name, path in files.items()]}), encoding="utf-8")
        return config

    def _benchmark(self, root, session):
        path = root / "sh000300_test"
        path.write_text(
            "date,open,high,low,close,volume\n"
            "{},1,1,1,1,1\n".format(session), encoding="utf-8")
        return str(root / "sh000300_*")

    def test_same_day_source_publishes_once_and_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationalStore(root / "state.sqlite3")
            clock = lambda: datetime(2026, 10, 9, 18, 55,
                                     tzinfo=ZoneInfo("Asia/Shanghai"))
            job = DailyShadowSnapshotJob(
                store, root / "content", ROOT / "configs/service/daily_data_v1.json",
                self._source_config(root), self._benchmark(root, 20261009), clock=clock)
            first = job()
            store.connection.execute(
                "DELETE FROM audit_findings "
                "WHERE category='DAILY_SHADOW_RECONCILIATION'")
            second = job()
            self.assertEqual("COMMITTED", first["status"])
            self.assertEqual("ALREADY_COMMITTED", second["status"])
            self.assertEqual(first["snapshot_id"], second["snapshot_id"])
            self.assertEqual(1, store.connection.execute(
                "SELECT count(*) FROM audit_findings "
                "WHERE category='DAILY_SHADOW_RECONCILIATION'"
            ).fetchone()[0])
            store.close()

    def test_stale_benchmark_is_retryable_and_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OperationalStore(root / "state.sqlite3")
            clock = lambda: datetime(2026, 10, 9, 18, 45,
                                     tzinfo=ZoneInfo("Asia/Shanghai"))
            job = DailyShadowSnapshotJob(
                store, root / "content", ROOT / "configs/service/daily_data_v1.json",
                self._source_config(root), self._benchmark(root, 20261008), clock=clock)
            result = job()
            self.assertEqual("RETRYABLE_NOT_READY", result["status"])
            self.assertEqual(0, store.connection.execute(
                "SELECT count(*) FROM market_snapshots").fetchone()[0])
            store.close()


if __name__ == "__main__":
    unittest.main()
