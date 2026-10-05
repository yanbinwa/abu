#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu import OperationalStore  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Create a verified SQLite online backup")
    parser.add_argument("--config", type=Path, default=ROOT / "configs/service/service_v1.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    output = args.output or Path(config["backup_root"]) / now.strftime("%Y%m%dT%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    database = output / "operational.sqlite3"
    manifest = output / "backup_manifest.json"
    with OperationalStore(config["database_path"]) as store:
        payload = store.backup(
            database, manifest, service_instance_id="backup-cli",
            created_at=now.isoformat())
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
