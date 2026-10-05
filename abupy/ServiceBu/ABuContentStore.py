from __future__ import absolute_import

import hashlib
import json
import os
import tempfile
from pathlib import Path


def canonical_json_bytes(payload):
    """Return the one canonical JSON representation used by service hashes."""
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(payload):
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class ContentAddressedStore(object):
    """Immutable files published with same-filesystem rename and directory fsync."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def write_json(self, namespace, payload, expected_sha256=None):
        content = canonical_json_bytes(payload) + b"\n"
        return self.write_bytes(namespace, content, suffix=".json",
                                expected_sha256=expected_sha256)

    def write_bytes(self, namespace, content, suffix=".bin", expected_sha256=None):
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        digest = hashlib.sha256(content).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("content hash does not match expected sha256")
        directory = self.root / namespace / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (digest + suffix)
        if target.exists():
            if sha256_file(target) != digest:
                raise ValueError("existing content-addressed file is corrupt")
            return target, digest, False

        descriptor, temporary_name = tempfile.mkstemp(
            prefix="." + digest + ".", suffix=".tmp", dir=str(directory)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(target))
            directory_descriptor = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target, digest, True

    def verify(self, path, expected_sha256):
        path = Path(path)
        if not path.is_file():
            return False
        return sha256_file(path) == expected_sha256

    def iter_files(self, namespace=None):
        base = self.root / namespace if namespace else self.root
        if not base.exists():
            return iter(())
        return (path for path in base.rglob("*") if path.is_file())
