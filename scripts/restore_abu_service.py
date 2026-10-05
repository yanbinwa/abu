#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu import OperationalStore  # noqa: E402

def main(argv=None):
    parser = argparse.ArgumentParser(description="Restore an ABu service backup to an empty path")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    restored = OperationalStore.restore(args.backup, args.manifest, args.destination)
    print(str(restored))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
