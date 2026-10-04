#!/usr/bin/env python3
"""Verify a v4 artifact manifest and all files bound to it."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from abupy.AlphaBu.ABuArtifactManifest import (  # noqa: E402
    verify_artifact_manifest,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--file", action="append", default=[], metavar="ROLE=PATH",
        help="relocate a manifest-bound file without changing its content",
    )
    args = parser.parse_args()
    overrides = {}
    for value in args.file:
        if "=" not in value:
            parser.error("--file requires ROLE=PATH")
        role, path = value.split("=", 1)
        overrides[role] = Path(path)
    manifest = verify_artifact_manifest(args.manifest, overrides)
    print(json.dumps({
        "artifact_id": manifest["artifact_id"],
        "manifest_sha256": manifest["manifest_sha256"],
        "status": "VERIFIED_RESEARCH_ONLY",
    }, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
