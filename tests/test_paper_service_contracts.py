import json
import hashlib
import re
import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "abupy" / "ServiceBu" / "schemas"
CONFIGS = ROOT / "configs" / "service"
HASH = "a" * 64


def _is_type(value, expected):
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise AssertionError("unsupported schema type: {}".format(expected))


def validate(schema, value, path="$",
             *, root_schema=None):
    """Validate the deliberately small JSON-Schema subset used by M0 contracts."""
    root_schema = root_schema or schema
    expected = schema.get("type")
    if expected is not None:
        options = expected if isinstance(expected, list) else [expected]
        if not any(_is_type(value, item) for item in options):
            raise AssertionError("{} expected {}, got {!r}".format(path, options, value))
    if "const" in schema and value != schema["const"]:
        raise AssertionError("{} must equal {!r}".format(path, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        raise AssertionError("{} not in enum".format(path))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise AssertionError("{} too short".format(path))
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise AssertionError("{} does not match pattern".format(path))
    if isinstance(value, int) and "minimum" in schema and value < schema["minimum"]:
        raise AssertionError("{} below minimum".format(path))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise AssertionError("{} has too few items".format(path))
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                raise AssertionError("{} contains duplicate items".format(path))
        for index, item in enumerate(value):
            validate(schema.get("items", {}), item, "{}[{}]".format(path, index),
                     root_schema=root_schema)
    if isinstance(value, dict):
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            raise AssertionError("{} missing {}".format(path, sorted(missing)))
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            child = properties.get(key)
            if child is None:
                if additional is False:
                    raise AssertionError("{} unexpected property {}".format(path, key))
                if isinstance(additional, dict):
                    child = additional
            if child is not None:
                validate(child, item, "{}.{}".format(path, key),
                         root_schema=root_schema)


class PaperServiceContractTest(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("PRAGMA foreign_keys = ON")
        sql = (SCHEMAS / "operational_v1.sql").read_text(encoding="utf-8")
        self.connection.executescript(sql)

    def tearDown(self):
        self.connection.close()

    def test_all_json_schema_examples_validate(self):
        schema_paths = sorted(SCHEMAS.glob("*_v1.json"))
        self.assertGreaterEqual(len(schema_paths), 7)
        for path in schema_paths:
            schema = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("https://json-schema.org/draft/2020-12/schema",
                             schema["$schema"])
            self.assertTrue(schema.get("examples"), path.name)
            for example in schema["examples"]:
                validate(schema, example)

    def test_domain_event_requires_stream_sequence(self):
        schema = json.loads((SCHEMAS / "domain_event_v1.json").read_text(
            encoding="utf-8"))
        example = dict(schema["examples"][0])
        example.pop("sequence_no")
        with self.assertRaisesRegex(AssertionError, "sequence_no"):
            validate(schema, example)

    def test_sql_schema_is_idempotent_and_has_expected_tables(self):
        sql = (SCHEMAS / "operational_v1.sql").read_text(encoding="utf-8")
        self.connection.executescript(sql)
        tables = {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {
            "job_runs", "job_run_attempts", "job_ownerships",
            "market_snapshots", "domain_events", "event_consumers",
            "event_consumptions", "stream_watermarks", "accounts",
            "account_sessions", "logical_trades", "position_lots",
            "processed_events", "notification_outbox", "account_cutovers",
        }
        self.assertTrue(expected.issubset(tables), expected - tables)

    def _service_and_job(self):
        self.connection.execute(
            "INSERT INTO service_instances VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("service-1", "localhost", 1, "2026-10-05T00:00:00+08:00",
             None, None, "3030291", HASH))
        self.connection.execute(
            "INSERT INTO job_definitions VALUES (?, ?, ?, ?, ?, ?)",
            ("daily.collect", "v1", "DAILY_DATA", "{}", 1, 1))
        self.connection.execute(
            "INSERT INTO job_runs (job_run_id, job_id, job_version, idempotency_key, "
            "scheduled_for, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("run-1", "daily.collect", "v1", "daily.collect:20261009",
             "2026-10-09T18:00:00+08:00", "RUNNING",
             "2026-10-09T18:00:00+08:00"))

    def test_job_attempts_preserve_each_attempt(self):
        self._service_and_job()
        for attempt in (1, 2):
            self.connection.execute(
                "INSERT INTO job_run_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("attempt-{}".format(attempt), "run-1", attempt, "service-1",
                 "2026-10-09T18:0{}:00+08:00".format(attempt),
                 "2026-10-09T18:0{}:30+08:00".format(attempt),
                 "RETRYABLE_FAILED", "TEST", "failure"))
        rows = list(self.connection.execute(
            "SELECT attempt_no FROM job_run_attempts ORDER BY attempt_no"))
        self.assertEqual([(1,), (2,)], rows)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO job_run_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("attempt-duplicate", "run-1", 2, "service-1",
                 "2026-10-09T18:03:00+08:00", None, "RUNNING", None, None))

    def _snapshot_and_event(self):
        self.connection.execute(
            "INSERT INTO market_snapshots (snapshot_id, snapshot_type, stream_id, "
            "sequence_no, trading_session, decision_cutoff, status, manifest_path, "
            "manifest_sha256, created_at, committed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("minute-1", "MINUTE", "market-minute:20261009:1", 1, 20261009,
             "2026-10-09T09:31:05+08:00", "COMMITTED", "minute-1.json", HASH,
             "2026-10-09T09:31:05+08:00", "2026-10-09T09:31:05+08:00"))
        self.connection.execute(
            "INSERT INTO domain_events (event_id, stream_id, sequence_no, event_type, "
            "schema_version, occurred_at, available_at, trading_session, source_service, "
            "snapshot_id, payload_sha256, payload_json, source_transaction_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("event-1", "market-minute:20261009:1", 1,
             "MinuteSnapshotCommitted", "domain_event_v1",
             "2026-10-09T09:31:05+08:00", "2026-10-09T09:31:05+08:00",
             20261009, "minute_market_hub", "minute-1", HASH, "{}", "tx-1",
             "2026-10-09T09:31:05+08:00"))

    def test_stream_sequence_is_unique_and_stable_across_watchlists(self):
        self._snapshot_and_event()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO domain_events (event_id, stream_id, sequence_no, event_type, "
                "schema_version, occurred_at, available_at, source_service, payload_sha256, "
                "payload_json, source_transaction_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("event-duplicate", "market-minute:20261009:1", 1, "Other",
                 "domain_event_v1", "2026-10-09T09:31:06+08:00",
                 "2026-10-09T09:31:06+08:00", "minute_market_hub", HASH, "{}",
                 "tx-2", "2026-10-09T09:31:06+08:00"))
        schema = json.loads((SCHEMAS / "minute_snapshot_v1.json").read_text(
            encoding="utf-8"))
        self.assertEqual("market-minute:20261009:1",
                         schema["examples"][0]["stream_id"])
        self.assertNotIn("watchlist-a", schema["examples"][0]["stream_id"])

    def test_account_session_phase_enum_rejects_unapproved_phase(self):
        self.connection.execute(
            "INSERT INTO strategy_instances VALUES (?, ?, ?, ?, ?)",
            ("vcp-control", "vcp", "v2", "2026-10-05T00:00:00+08:00", None))
        self.connection.execute(
            "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("account-a", "Account A", 0, "vcp-control", None, "SHADOW",
             "2026-10-05T00:00:00+08:00", "2026-10-05T00:00:00+08:00"))
        self.connection.execute(
            "INSERT INTO account_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("account-a", 20261009, "CREATED", None, None, 0,
             "2026-10-09T08:30:00+08:00"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE account_sessions SET phase='BUY_ANYWAY' "
                "WHERE account_id='account-a' AND trading_session=20261009")

    def test_service_configs_are_safe_by_default(self):
        for path in sorted(CONFIGS.glob("*.json")):
            self.assertIsInstance(json.loads(path.read_text(encoding="utf-8")), dict,
                                  path.name)
        service = json.loads((CONFIGS / "service_v1.json").read_text(encoding="utf-8"))
        self.assertTrue(service["research_only"])
        self.assertFalse(service["broker_connected"])
        self.assertFalse(service["account_writes_enabled"])
        self.assertEqual("ACCEPT_DATA_ONLY", service["minute_execution_admission"])

        retention = json.loads((CONFIGS / "retention_v1.json").read_text(
            encoding="utf-8"))
        self.assertFalse(retention["automatic_authoritative_deletion"])
        self.assertIn("STOP_ACCOUNT_MUTATION", retention["critical_actions"])

    def test_job_ownership_covers_current_legacy_automations(self):
        ownership = json.loads((CONFIGS / "job_ownership_v1.json").read_text(
            encoding="utf-8"))
        owners = {item["old_owner"] for item in ownership["entries"]
                  if item["old_owner"] is not None}
        expected = {
            "codex-automation:vcp", "codex-automation:alpha158",
            "codex-automation:v1", "codex-automation:v1-2",
            "codex-automation:v1-3", "codex-automation:shadow",
        }
        self.assertEqual(expected, owners)
        legacy = [item for item in ownership["entries"]
                  if item["old_owner"] is not None]
        self.assertTrue(all(item["status"] == "OLD_OWNER_ACTIVE"
                            for item in legacy))
        self.assertTrue(all(item["cutover_session"] is None
                            for item in ownership["entries"]))
        shadow = [item for item in ownership["entries"]
                  if item["job_id"] == "daily.snapshot_shadow"]
        self.assertEqual(1, len(shadow))
        self.assertEqual("NEW_OWNER_SHADOW", shadow[0]["status"])
        self.assertFalse(shadow[0]["writes_account"])

    def test_golden_baseline_file_hashes_match(self):
        baseline = json.loads((CONFIGS / "golden_baselines_v1.json").read_text(
            encoding="utf-8"))
        files = {}
        files.update(baseline["strategy_execution_golden"]["files"])
        files.update(baseline["implementation_files"])
        files.update(baseline["frozen_configs"])
        for relative, expected in files.items():
            actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            self.assertEqual(expected, actual, relative)


if __name__ == "__main__":
    unittest.main()
