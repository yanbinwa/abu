from __future__ import absolute_import

import hashlib
import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1
SCHEMA_NAME = "paper_service_operational_v1"
LATEST_SCHEMA_VERSION = 4


def _schema_path():
    return Path(__file__).resolve().parent / "schemas" / "operational_v1.sql"


def _schema_v2_path():
    return Path(__file__).resolve().parent / "schemas" / "operational_v2.sql"


def _schema_v3_path():
    return Path(__file__).resolve().parent / "schemas" / "operational_v3.sql"


def _schema_v4_path():
    return Path(__file__).resolve().parent / "schemas" / "operational_v4.sql"


def schema_sha256(path=None):
    content = Path(path or _schema_path()).read_bytes()
    return hashlib.sha256(content).hexdigest()


def apply_schema_v1(connection, path=None):
    """Apply the trusted v1 schema atomically after connection pragmas are set."""
    path = Path(path or _schema_path())
    digest = schema_sha256(path)
    existing_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    if existing_table:
        existing = connection.execute(
            "SELECT sha256 FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)
        ).fetchone()
        if existing:
            if existing[0] != digest:
                raise ValueError("schema v1 hash differs from applied migration")
            return digest

    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().upper().startswith("PRAGMA "):
            continue
        lines.append(line)
    safe_name = SCHEMA_NAME.replace("'", "''")
    safe_hash = digest.replace("'", "''")
    script = "BEGIN EXCLUSIVE;\n{}\nINSERT INTO schema_migrations " \
        "(version, name, applied_at, sha256) VALUES " \
        "({}, '{}', strftime('%Y-%m-%dT%H:%M:%fZ','now'), '{}');\nCOMMIT;".format(
            "\n".join(lines), SCHEMA_VERSION, safe_name, safe_hash)
    try:
        connection.executescript(script)
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    return digest


def apply_schema(connection, target_version=1):
    """Apply monotonic migrations up to an explicitly requested version.

    Version 1 remains the default so the active M5 data-only runtime cannot be
    upgraded merely by importing newer research code.  A caller must create a
    verified backup before explicitly requesting version 2 for account work.
    """
    target = int(target_version)
    if target < 1 or target > LATEST_SCHEMA_VERSION:
        raise ValueError("unsupported operational schema target")
    digests = [apply_schema_v1(connection)]
    current = connection.execute(
        "SELECT max(version) FROM schema_migrations").fetchone()[0]
    if int(current) > target:
        raise ValueError(
            "database schema {} is newer than requested runtime schema {}".format(
                current, target))
    if target >= 2:
        path = _schema_v2_path()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = connection.execute(
            "SELECT sha256 FROM schema_migrations WHERE version=2"
        ).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise ValueError("schema v2 hash differs from applied migration")
        else:
            safe_hash = digest.replace("'", "''")
            lines = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip().upper().startswith("PRAGMA "):
                    lines.append(line)
            script = "BEGIN EXCLUSIVE;\n{}\nINSERT INTO schema_migrations " \
                "(version, name, applied_at, sha256) VALUES " \
                "(2, 'paper_service_operational_v2', " \
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'), '{}');\nCOMMIT;".format(
                    "\n".join(lines), safe_hash)
            try:
                connection.executescript(script)
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        digests.append(digest)
    if target >= 3:
        path = _schema_v3_path()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = connection.execute(
            "SELECT sha256 FROM schema_migrations WHERE version=3"
        ).fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise ValueError("schema v3 hash differs from applied migration")
        else:
            safe_hash = digest.replace("'", "''")
            lines = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip().upper().startswith("PRAGMA "):
                    lines.append(line)
            script = "BEGIN EXCLUSIVE;\n{}\nINSERT INTO schema_migrations " \
                "(version, name, applied_at, sha256) VALUES " \
                "(3, 'paper_service_operational_v3', " \
                "strftime('%Y-%m-%dT%H:%M:%fZ','now'), '{}');\nCOMMIT;".format(
                    "\n".join(lines), safe_hash)
            try:
                connection.executescript(script)
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        digests.append(digest)
    if target >= 4:
        path = _schema_v4_path()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        existing = connection.execute(
            "SELECT sha256 FROM schema_migrations WHERE version=4").fetchone()
        if existing is not None:
            if existing[0] != digest:
                raise ValueError("schema v4 hash differs from applied migration")
        else:
            safe_hash = digest.replace("'", "''")
            lines = [line for line in path.read_text(encoding="utf-8").splitlines()
                     if not line.strip().upper().startswith("PRAGMA ")]
            script = "BEGIN EXCLUSIVE;\n{}\nINSERT INTO schema_migrations " \
                "(version,name,applied_at,sha256) VALUES " \
                "(4,'paper_service_operational_v4',"
            script += "strftime('%Y-%m-%dT%H:%M:%fZ','now'),'{}');\nCOMMIT;".format(
                safe_hash)
            script = script.format("\n".join(lines))
            try:
                connection.executescript(script)
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise
        digests.append(digest)
    # Preserve the public v1 digest exactly.  Existing backup manifests and
    # release evidence use the migration file digest as the schema identity.
    if target == 1:
        return digests[0]
    return hashlib.sha256("|".join(digests).encode("utf-8")).hexdigest()


def configure_connection(connection):
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise sqlite3.DatabaseError("failed to enable SQLite foreign keys")
