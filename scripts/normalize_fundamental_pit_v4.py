#!/usr/bin/env python3
"""Normalize provenance-rich extracted fundamental JSONL into PIT facts."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuFundamentalPIT import (  # noqa: E402
    normalize_extracted_facts, stable_json,
)


DEFAULT_MAPPING = ROOT / "configs/selection/fundamental_field_mapping_v4.json"


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(
        encoding="utf-8").splitlines() if line.strip()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--trading-sessions", type=Path, required=True,
                        help="JSON array of YYYY-MM-DD sessions")
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("refusing to overwrite normalized PIT facts")
    mapping = json.loads(args.mapping.read_text(encoding="utf-8"))
    mapping_hash = hashlib.sha256(stable_json(mapping).encode("utf-8")).hexdigest()
    sessions = json.loads(args.trading_sessions.read_text(encoding="utf-8"))
    facts = normalize_extracted_facts(
        read_jsonl(args.input), mapping, sessions,
        mapping["mapping_version"] + ":" + mapping_hash,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(
        stable_json(fact.record()) + "\n" for fact in facts
    ), encoding="utf-8")
    print(json.dumps({
        "input_rows": len(read_jsonl(args.input)),
        "normalized_rows": len(facts), "mapping_sha256": mapping_hash,
        "output": str(args.output), "status": "NORMALIZED_RESEARCH_ONLY",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
