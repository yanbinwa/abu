import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from abupy.ServiceBu import (
    ContentAddressedStore, DailyComponent, DailyRawArchive, DailySnapshotBuilder,
    FactorSnapshotBuilder, FieldDependencyPolicy, OperationalStore,
    ProviderRateLimiter, SnapshotCatalog, compare_selection_panels,
    components_from_source_config, incremental_sessions, version_files,
    normalize_daily_bars, select_pit_records, write_coverage_report,
)


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "configs/service/daily_data_v1.json"
NOW = "2026-10-09T18:05:00+08:00"
CUTOFF = "2026-10-09T18:00:00+08:00"


def component(name, version=None, available_at=CUTOFF, **kwargs):
    return DailyComponent(
        name, version or "sha256:" + (name[0] * 64), "test", available_at, 1,
        **kwargs)


def required_components():
    return [component(name) for name in (
        "universe", "calendar", "security_master", "raw_price",
        "adjusted_price", "corporate_action", "limit_reference")]


class DailyDataCenterTest(unittest.TestCase):

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.store = OperationalStore(root / "state.sqlite3")
        self.catalog = SnapshotCatalog(self.store, root / "content")
        self.policy = FieldDependencyPolicy.from_path(POLICY_PATH)
        self.builder = DailySnapshotBuilder(self.catalog, self.policy)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_raw_capture_is_content_addressed_idempotent_and_redacted(self):
        archive = DailyRawArchive(Path(self.directory.name) / "raw")
        first = archive.capture(
            "provider", "quotes", {"token": "secret", "symbol": "000001"},
            b'{"close": 10}', retrieved_at=NOW, source_version="v1")
        second = archive.capture(
            "provider", "quotes", {"token": "secret", "symbol": "000001"},
            b'{"close": 10}', retrieved_at=NOW, source_version="v1")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual("<REDACTED>", first["metadata"]["request"]["token"])
        self.assertEqual(first["metadata_sha256"], second["metadata_sha256"])

    def test_incremental_plan_and_provider_rate_limit(self):
        self.assertEqual([20261008, 20261009], incremental_sessions(
            [20261007, 20261008, 20261009, 20261010], 20261007, 20261009))
        clock_value = [0.0]
        sleeps = []

        def sleeper(seconds):
            sleeps.append(seconds)
            clock_value[0] += seconds

        limiter = ProviderRateLimiter(
            2, clock=lambda: clock_value[0], sleeper=sleeper)
        limiter.wait()
        limiter.wait()
        self.assertEqual([0.5], sleeps)

    def test_raw_execution_and_adjusted_signal_prices_cannot_be_confused(self):
        frame = pd.DataFrame({
            "date": [20261009], "symbol": ["sz000001"], "open": [10.0],
            "high": [11.0], "low": [9.5], "close": [10.5], "volume": [100],
        })
        raw = normalize_daily_bars(frame, "RAW_EXECUTION")
        adjusted = normalize_daily_bars(frame.assign(close=5.25, open=5.0,
                                                       high=5.5, low=4.75),
                                          "ADJUSTED_SIGNAL")
        self.assertEqual("RAW_EXECUTION", raw.price_space.iloc[0])
        self.assertEqual("ADJUSTED_SIGNAL", adjusted.price_space.iloc[0])
        with self.assertRaisesRegex(ValueError, "unknown price space"):
            normalize_daily_bars(frame, "MIXED")

    def test_pit_selection_excludes_future_revisions(self):
        records = [{
            "symbol": "sz000001", "field": "pe", "observation_session": 20260930,
            "available_at": "2026-10-01T10:00:00+08:00", "revision_id": "r1",
            "value": 10.0,
        }, {
            "symbol": "sz000001", "field": "pe", "observation_session": 20260930,
            "available_at": "2026-10-10T10:00:00+08:00", "revision_id": "r2",
            "value": 11.0,
        }]
        selected = select_pit_records(
            records, 20261009, "2026-10-09T18:00:00+08:00")
        self.assertEqual(10.0, selected[0]["value"])

    def test_required_and_strategy_specific_components_fail_closed(self):
        available = {item.component_id for item in required_components()}
        vcp = self.policy.resolve(available, strategy_id="vcp")
        self.assertEqual("vcp_frozen_v1", vcp["factor_set_version"])
        self.assertIn("valuation", self.policy.resolve(available)["missing_optional"])
        with self.assertRaisesRegex(ValueError, "value_quality_research.*valuation"):
            self.policy.resolve(available, strategy_id="value_quality_research")
        with self.assertRaisesRegex(ValueError, "raw_price"):
            self.policy.resolve(available - {"raw_price"})

    def test_daily_snapshot_is_idempotent_and_component_change_is_new_revision(self):
        first, first_event, created = self.builder.build(
            20261009, CUTOFF, NOW, required_components())
        self.assertTrue(created)
        repeated, repeated_event, created = self.builder.build(
            20261009, CUTOFF, NOW, required_components())
        self.assertFalse(created)
        self.assertEqual(first["snapshot_id"], repeated["snapshot_id"])
        self.assertEqual(first_event["event_id"], repeated_event["event_id"])

        changed = required_components()
        changed[3] = component("raw_price", "sha256:" + "f" * 64)
        second, second_event, created = self.builder.build(
            20261009, CUTOFF, "2026-10-09T18:06:00+08:00", changed,
            sequence_no=2, previous_snapshot_id=first["snapshot_id"])
        self.assertTrue(created)
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first_event["event_id"], second_event["previous_event_id"])

    def test_cutoff_and_pit_coverage_are_enforced(self):
        late = required_components()
        late[3] = component("raw_price", available_at="2026-10-09T18:01:00+08:00")
        with self.assertRaisesRegex(ValueError, "not available at cutoff"):
            self.builder.build(20261009, CUTOFF, NOW, late)
        incomplete = required_components()
        incomplete[3] = component("raw_price", pit_complete_from=20261010)
        with self.assertRaisesRegex(ValueError, "PIT history is incomplete"):
            self.builder.build(20261009, CUTOFF, NOW, incomplete)

    def test_factor_snapshot_is_deterministic_and_keeps_frozen_factor_set(self):
        daily, unused_event, unused_created = self.builder.build(
            20261009, CUTOFF, NOW, required_components())
        factors = FactorSnapshotBuilder(self.catalog, self.policy)
        available = {item.component_id for item in required_components()}
        first, unused_event, created = factors.build(
            "vcp", 20261009, CUTOFF, NOW, daily["snapshot_id"], available,
            {"symbols": ["sz000001"], "score": [1.0]})
        second, unused_event, created_again = factors.build(
            "vcp", 20261009, CUTOFF, NOW, daily["snapshot_id"], available,
            {"symbols": ["sz000001"], "score": [1.0]})
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual("vcp_frozen_v1", manifest["factor_set_version"])
        self.assertTrue(Path(manifest["factor_payload_path"]).is_file())
        with self.assertRaisesRegex(ValueError, "strategy value_quality_research blocked"):
            factors.build(
                "value_quality_research", 20261009, CUTOFF, NOW,
                daily["snapshot_id"], available, {"pe": [10.0]})

    def test_source_adapter_versions_files_and_coverage_is_immutable(self):
        root = Path(self.directory.name)
        first = root / "a.csv"
        second = root / "b.csv"
        first.write_text("x\n1\n", encoding="utf-8")
        second.write_text("x\n2\n", encoding="utf-8")
        version, inventory = version_files([second, first], root=root)
        self.assertTrue(version.startswith("sha256:"))
        self.assertEqual(["a.csv", "b.csv"], [item["path"] for item in inventory])
        source_config = {"components": [{
            "component_id": "raw_price", "source": "test",
            "patterns": [str(root / "*.csv")],
        }, {
            "component_id": "valuation", "source": "test",
            "patterns": [str(root / "missing*.csv")],
        }]}
        components, details = components_from_source_config(source_config, CUTOFF)
        self.assertEqual(["raw_price"], [item.component_id for item in components])
        self.assertEqual("MISSING", details["valuation"]["status"])
        report = write_coverage_report(
            ContentAddressedStore(root / "coverage"), 20261009,
            required_components(), self.policy)
        repeated = write_coverage_report(
            ContentAddressedStore(root / "coverage"), 20261009,
            required_components(), self.policy)
        self.assertTrue(report["created"])
        self.assertFalse(repeated["created"])

    def test_selection_panel_comparison_reports_exact_differences(self):
        values = dict(
            dates=np.array([20261009]), symbols=np.array(["sz000001"]),
            close=np.array([[10.0]]), exec_open=np.array([[9.9]]),
            exec_close=np.array([[10.0]]), amount=np.array([[100.0]]),
            market_cap=np.array([[1000.0]]), turnover=np.array([[0.1]]),
            universe_mask=np.array([[True]]), st_status_known=np.array([[True]]),
            buy_tradable_mask=np.array([[True]]), sell_tradable_mask=np.array([[True]]),
        )
        expected = SimpleNamespace(**values)
        actual = SimpleNamespace(**{key: value.copy() for key, value in values.items()})
        self.assertTrue(compare_selection_panels(expected, actual)["matched"])
        actual.close[0, 0] = 10.1
        result = compare_selection_panels(expected, actual)
        self.assertFalse(result["matched"])
        self.assertEqual("close", result["findings"][0]["field"])


if __name__ == "__main__":
    unittest.main()
