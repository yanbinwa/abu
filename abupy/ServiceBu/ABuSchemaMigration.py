from __future__ import absolute_import

import hashlib
import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1
SCHEMA_NAME = "paper_service_operational_v1"


def _schema_path():
    return Path(__file__).resolve().parent / "schemas" / "operational_v1.sql"


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


def configure_connection(connection):
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise sqlite3.DatabaseError("failed to enable SQLite foreign keys")
