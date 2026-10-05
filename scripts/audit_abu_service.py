#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit an existing ABu service database")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/service/service_v1.json")
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    database = Path(config["database_path"])
    if not database.is_file():
        raise FileNotFoundError("service database does not exist: {}".format(database))
    connection = sqlite3.connect("file:{}?mode=ro".format(database), uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(list(connection.execute("PRAGMA foreign_key_check")))
        version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        running_attempts = connection.execute(
            "SELECT count(*) FROM job_run_attempts WHERE status='RUNNING'").fetchone()[0]
        active_instances = connection.execute(
            "SELECT count(*) FROM service_instances WHERE stopped_at IS NULL").fetchone()[0]
    finally:
        connection.close()
    result = {
        "database": str(database), "integrity_check": integrity,
        "foreign_key_errors": foreign_keys, "schema_version": version,
        "running_attempts": running_attempts, "active_instances": active_instances,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if integrity == "ok" and foreign_keys == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
