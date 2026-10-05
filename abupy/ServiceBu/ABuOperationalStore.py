from __future__ import absolute_import

import hashlib
import json
import os
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from .ABuSchemaMigration import apply_schema_v1, configure_connection


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class OperationalStore(object):

    def __init__(self, path, initialize=True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._transaction_lock = threading.RLock()
        self.connection = sqlite3.connect(
            str(self.path), timeout=5.0, isolation_level=None,
            check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        configure_connection(self.connection)
        self.schema_sha256 = apply_schema_v1(self.connection) if initialize else None

    @contextmanager
    def transaction(self, immediate=True):
        with self._transaction_lock:
            self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.connection
            except Exception:
                self.connection.rollback()
                raise
            else:
                self.connection.commit()

    def record_service_start(self, service_instance_id, host_name, process_id,
                             started_at, repository_commit, config_sha256):
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO service_instances "
                "(service_instance_id, host_name, process_id, started_at, "
                "repository_commit, config_sha256) VALUES (?, ?, ?, ?, ?, ?)",
                (service_instance_id, host_name, int(process_id), started_at,
                 repository_commit, config_sha256))

    def recover_stale_service_instances(self, recovered_at):
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE service_instances SET stopped_at=?, "
                "stop_reason='SERVICE_RESTART_DETECTED' WHERE stopped_at IS NULL",
                (recovered_at,))
            return cursor.rowcount

    def record_service_stop(self, service_instance_id, stopped_at, reason):
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE service_instances SET stopped_at=?, stop_reason=? "
                "WHERE service_instance_id=? AND stopped_at IS NULL",
                (stopped_at, reason, service_instance_id))
            if cursor.rowcount != 1:
                raise ValueError("service instance is missing or already stopped")

    def integrity_check(self):
        with self._transaction_lock:
            result = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys = list(self.connection.execute("PRAGMA foreign_key_check"))
        return {"integrity_check": result, "foreign_key_errors": len(foreign_keys)}

    def backup(self, backup_path, manifest_path, *, service_instance_id,
               immutable_files=(), created_at):
        backup_path = Path(backup_path)
        manifest_path = Path(manifest_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = backup_path.parent / ("." + backup_path.name + ".tmp")
        if temporary.exists():
            temporary.unlink()
        destination = sqlite3.connect(str(temporary))
        try:
            with self._transaction_lock:
                self.connection.backup(destination)
        finally:
            destination.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, backup_path)
        directory = os.open(str(backup_path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        check = sqlite3.connect(str(backup_path))
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            version = check.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        finally:
            check.close()
        if integrity != "ok":
            raise sqlite3.DatabaseError("backup integrity check failed: {}".format(integrity))
        files = []
        for item in immutable_files:
            item = Path(item)
            files.append({
                "path": str(item), "sha256": _sha256(item),
                "retention_class": "PERMANENT_AUDIT",
            })
        payload = {
            "schema_version": "backup_manifest_v1",
            "backup_id": backup_path.stem,
            "database_backup_sha256": _sha256(backup_path),
            "database_schema_version": int(version),
            "immutable_files": files,
            "created_at": created_at,
            "source_service_instance_id": service_instance_id,
        }
        _atomic_json(manifest_path, payload)
        return payload

    @staticmethod
    def restore(backup_path, manifest_path, destination_path):
        backup_path = Path(backup_path)
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        if _sha256(backup_path) != payload["database_backup_sha256"]:
            raise ValueError("database backup hash mismatch")
        for item in payload["immutable_files"]:
            if _sha256(item["path"]) != item["sha256"]:
                raise ValueError("immutable backup dependency hash mismatch")
        destination_path = Path(destination_path)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        if destination_path.exists():
            raise FileExistsError("restore destination already exists: {}".format(destination_path))
        temporary = destination_path.parent / ("." + destination_path.name + ".tmp")
        shutil.copyfile(backup_path, temporary)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination_path)
        connection = sqlite3.connect(str(destination_path))
        try:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("restored database failed integrity check")
        finally:
            connection.close()
        return destination_path

    def close(self):
        with self._transaction_lock:
            self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
