#!/usr/bin/env python3
from __future__ import absolute_import

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.ServiceBu.ABuMockPaperScenario import MockPaperTradingScenario  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="Run one deterministic end-to-end MOCK paper account day")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(
        MockPaperTradingScenario(args.output).run(),
        ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
