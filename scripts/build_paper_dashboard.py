#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu.ABuDashboard import (  # noqa: E402
    DashboardQuery, StaticDashboardRenderer,
)


def main():
    parser = argparse.ArgumentParser(description="Build read-only paper dashboard")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with DashboardQuery(args.database) as query:
        result = StaticDashboardRenderer().build(query, args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
