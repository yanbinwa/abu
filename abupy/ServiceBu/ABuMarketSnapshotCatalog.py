from __future__ import absolute_import

import json
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator

from .ABuContentStore import ContentAddressedStore, sha256_json
from .ABuDomainEventStore import DomainEventStore, build_domain_event


class SnapshotCatalog(object):

    _TYPE_TO_SCHEMA = {
        "DAILY": "daily_snapshot_v1",
        "PREOPEN": "preopen_snapshot_v1",
        "MINUTE": "minute_snapshot_v1",
        "FACTOR": "factor_snapshot_v1",
        "WATCHLIST": "watchlist_v1",
    }

    def __init__(self, operational_store, content_root):
        self.store = operational_store
        self.content = ContentAddressedStore(content_root)
        self.events = DomainEventStore(operational_store)

    @staticmethod
    def _validate_manifest(snapshot_type, document):
        schema_path = (Path(__file__).resolve().parent / "schemas" /
                       (SnapshotCatalog._TYPE_TO_SCHEMA[snapshot_type] + ".json"))
        if not schema_path.exists():
            return
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(document)

    def publish(self, snapshot_type, manifest, *, source_service,
                event_type=None, fail_after_file=False,
                partition_rows=(), selection_rows=(),
                additional_events=(), audit_findings=()):
        snapshot_type = snapshot_type.upper()
        if snapshot_type not in self._TYPE_TO_SCHEMA:
            raise ValueError("unsupported snapshot type: {}".format(snapshot_type))
        core = dict(manifest)
        core.pop("manifest_sha256", None)
        core.pop("snapshot_id", None)
        required = ("stream_id", "sequence_no", "trading_session",
                    "decision_cutoff", "created_at")
        missing = [name for name in required if name not in core]
        if missing:
            raise ValueError("snapshot manifest missing: {}".format(", ".join(missing)))

        snapshot_id = "{}-{}".format(snapshot_type.lower(), sha256_json(core)[:24])
        document = dict(core)
        document["snapshot_id"] = snapshot_id
        document.setdefault("schema_version", self._TYPE_TO_SCHEMA[snapshot_type])
        document["manifest_sha256"] = sha256_json(document)
        self._validate_manifest(snapshot_type, document)
        path, file_sha256, unused_created = self.content.write_json(
            "manifests", document)
        if fail_after_file:
            raise RuntimeError("fault injected after manifest publication")

        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM market_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
            if existing is not None:
                if (existing["manifest_sha256"] != file_sha256 or
                        existing["stream_id"] != document["stream_id"] or
                        existing["sequence_no"] != int(document["sequence_no"])):
                    raise ValueError("snapshot id collision")
                event = connection.execute(
                    "SELECT * FROM domain_events WHERE snapshot_id=?", (snapshot_id,)
                ).fetchone()
                return dict(existing), dict(event), False

            previous = connection.execute(
                "SELECT snapshot_id, sequence_no FROM market_snapshots "
                "WHERE stream_id=? AND status='COMMITTED' "
                "ORDER BY sequence_no DESC LIMIT 1", (document["stream_id"],)
            ).fetchone()
            expected_sequence = 1 if previous is None else previous["sequence_no"] + 1
            expected_previous = None if previous is None else previous["snapshot_id"]
            if int(document["sequence_no"]) != expected_sequence:
                raise ValueError("snapshot sequence is not contiguous")
            if document.get("previous_snapshot_id") != expected_previous:
                raise ValueError("previous_snapshot_id does not match catalog head")

            connection.execute(
                "INSERT INTO market_snapshots "
                "(snapshot_id, snapshot_type, stream_id, sequence_no, previous_snapshot_id, "
                "trading_session, decision_cutoff, status, manifest_path, manifest_sha256, "
                "created_at, committed_at, quality_codes_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'COMMITTED', ?, ?, ?, ?, ?)",
                (snapshot_id, snapshot_type, document["stream_id"],
                 int(document["sequence_no"]), document.get("previous_snapshot_id"),
                 int(document["trading_session"]), document["decision_cutoff"],
                 str(path), file_sha256, document["created_at"], document["created_at"],
                 json.dumps(document.get("quality_codes", []), sort_keys=True)))
            for item in partition_rows:
                connection.execute(
                    "INSERT INTO market_snapshot_partitions "
                    "(snapshot_id, partition_key, partition_manifest_path, "
                    "partition_manifest_sha256, terminal_status, latest_available_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (snapshot_id, item["partition_key"], item["manifest_path"],
                     item["manifest_sha256"], item["terminal_status"],
                     item.get("latest_available_at")))
            for item in selection_rows:
                connection.execute(
                    "INSERT INTO minute_snapshot_selections "
                    "(snapshot_id, symbol, business_bar_key, selected_event_id, "
                    "selected_revision, available_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (snapshot_id, item["symbol"], item["business_bar_key"],
                     item["event_id"], int(item["revision"]),
                     item["available_at"]))
            event_payload = {
                "snapshot_id": snapshot_id,
                "manifest_sha256": file_sha256,
                "decision_cutoff": document["decision_cutoff"],
            }
            last_event = connection.execute(
                "SELECT event_id FROM domain_events WHERE stream_id=? AND sequence_no=?",
                (document["stream_id"], int(document["sequence_no"]) - 1)).fetchone()
            event = build_domain_event(
                event_type or "{}SnapshotCommitted".format(snapshot_type.title()),
                document["stream_id"], document["sequence_no"], event_payload,
                previous_event_id=None if last_event is None else last_event["event_id"],
                occurred_at=document["created_at"], available_at=document["created_at"],
                trading_session=document["trading_session"],
                source_service=source_service, snapshot_id=snapshot_id,
                correlation_id="snapshot:{}".format(document["trading_session"]),
                source_transaction_id="snapshot-tx-{}".format(uuid.uuid4().hex))
            event_row, unused_new = self.events.append(event, connection=connection)
            for extra_event in additional_events:
                self.events.append(extra_event, connection=connection)
            for finding in audit_findings:
                connection.execute(
                    "INSERT INTO audit_findings "
                    "(finding_id, severity, category, account_id, snapshot_id, "
                    "event_id, detail_json, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (finding["finding_id"], finding["severity"], finding["category"],
                     finding.get("account_id"), snapshot_id,
                     finding.get("event_id"),
                     json.dumps(finding.get("detail", {}), sort_keys=True),
                     finding["created_at"]))
            snapshot_row = dict(connection.execute(
                "SELECT * FROM market_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone())
            return snapshot_row, event_row, True

    def manifest(self, snapshot_id, verify=True):
        row = self.get(snapshot_id, verify=verify)
        if row is None:
            return None
        return json.loads(Path(row["manifest_path"]).read_text(encoding="utf-8"))

    def get(self, snapshot_id, verify=True):
        row = self.store.connection.execute(
            "SELECT * FROM market_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if row is None:
            return None
        value = dict(row)
        if verify and not self.content.verify(
                value["manifest_path"], value["manifest_sha256"]):
            raise ValueError("snapshot manifest missing or corrupt")
        return value

    def list_committed(self, snapshot_type=None, trading_session=None):
        clauses = ["status='COMMITTED'"]
        values = []
        if snapshot_type is not None:
            clauses.append("snapshot_type=?")
            values.append(snapshot_type.upper())
        if trading_session is not None:
            clauses.append("trading_session=?")
            values.append(int(trading_session))
        rows = self.store.connection.execute(
            "SELECT * FROM market_snapshots WHERE {} "
            "ORDER BY trading_session, stream_id, sequence_no".format(" AND ".join(clauses)),
            tuple(values)).fetchall()
        return [dict(row) for row in rows]

    def audit(self, now, mark_corrupt=True):
        referenced = set()
        corrupt = []
        for row in self.store.connection.execute("SELECT * FROM market_snapshots"):
            path = str(Path(row["manifest_path"]).resolve())
            referenced.add(path)
            if not self.content.verify(path, row["manifest_sha256"]):
                corrupt.append(row["snapshot_id"])
        files = {str(path.resolve()) for path in self.content.iter_files("manifests")}
        orphans = sorted(files - referenced)
        if corrupt and mark_corrupt:
            with self.store.transaction() as connection:
                for snapshot_id in corrupt:
                    connection.execute(
                        "UPDATE market_snapshots SET status='CORRUPT' WHERE snapshot_id=?",
                        (snapshot_id,))
                    connection.execute(
                        "INSERT INTO audit_findings "
                        "(finding_id, severity, category, snapshot_id, detail_json, created_at) "
                        "VALUES (?, 'CRITICAL', 'SNAPSHOT_MANIFEST_CORRUPT', ?, ?, ?)",
                        ("finding-{}".format(uuid.uuid4().hex), snapshot_id,
                         json.dumps({"snapshot_id": snapshot_id}, sort_keys=True), now))
        return {"corrupt_snapshot_ids": sorted(corrupt), "orphan_paths": orphans}
