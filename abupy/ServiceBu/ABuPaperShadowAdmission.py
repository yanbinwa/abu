from __future__ import absolute_import

import json
from pathlib import Path

from .ABuContentStore import sha256_json
from .ABuOperationalStore import _atomic_json


class PaperShadowAdmissionGate(object):

    SCHEMA = "transactional_paper_shadow_activation_v1"

    @classmethod
    def create(cls, path, minute_admission_result, *, database_path,
               snapshot_root, source_commit, created_at):
        if (not minute_admission_result.get("passed") or
                minute_admission_result.get("status") !=
                "MINUTE_DATA_ONLY_ACCEPTED"):
            raise ValueError("real minute data admission has not passed")
        observations = minute_admission_result.get("observations", ())
        if not observations or not all(item.get("passed") for item in observations):
            raise ValueError("minute admission observations are incomplete")
        payload = {
            "schema_version": cls.SCHEMA,
            "status": "TRANSACTIONAL_PAPER_SHADOW_ENABLED",
            "database_path": str(Path(database_path).resolve()),
            "snapshot_root": str(Path(snapshot_root).resolve()),
            "source_commit": source_commit,
            "created_at": created_at,
            "accepted_sessions": [
                int(item["trading_session"]) for item in observations],
            "minute_admission_sha256": sha256_json(minute_admission_result),
        }
        core = dict(payload)
        payload["certificate_sha256"] = sha256_json(core)
        _atomic_json(path, payload)
        return payload

    @classmethod
    def verify(cls, path, *, database_path, snapshot_root, source_commit=None):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        digest = payload.pop("certificate_sha256", None)
        if digest != sha256_json(payload):
            raise ValueError("paper shadow activation certificate hash mismatch")
        if (payload.get("schema_version") != cls.SCHEMA or
                payload.get("status") != "TRANSACTIONAL_PAPER_SHADOW_ENABLED"):
            raise ValueError("paper shadow activation certificate is invalid")
        if payload.get("database_path") != str(Path(database_path).resolve()):
            raise ValueError("activation certificate database mismatch")
        if payload.get("snapshot_root") != str(Path(snapshot_root).resolve()):
            raise ValueError("activation certificate snapshot root mismatch")
        if (source_commit is not None and
                payload.get("source_commit") != str(source_commit)):
            raise ValueError("activation certificate source mismatch")
        payload["certificate_sha256"] = digest
        return payload
