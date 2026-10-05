from __future__ import absolute_import

import hashlib
import json
import os
import time
from pathlib import Path

from .ABuNotificationPipeline import (
    DeliveryUnknown, PermanentDeliveryError, RetryableDeliveryError,
)


def _atomic_bytes(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _atomic_json(path, payload):
    _atomic_bytes(path, (json.dumps(
        payload, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")) + "\n").encode("utf-8"))


class WeComLongConnectionOutboxTransport(object):
    """Queue one deterministic part and wait for the resident bot receipt."""

    def __init__(self, runtime_root, *, timeout_seconds=20,
                 poll_seconds=.2, clock=None, sleeper=None):
        self.runtime_root = Path(runtime_root).resolve()
        self.outbox = self.runtime_root / "outbox"
        self.assets = self.runtime_root / "outbox-assets"
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        if self.timeout_seconds < 0 or self.poll_seconds <= 0:
            raise ValueError("invalid long-connection delivery timing")

    @staticmethod
    def _identifier(idempotency_key):
        if not idempotency_key:
            raise PermanentDeliveryError("notification idempotency key is required")
        return "txn-{}".format(hashlib.sha256(
            str(idempotency_key).encode("utf-8")).hexdigest()[:32])

    def _job(self, part_kind, path, identifier):
        try:
            content = Path(path).read_bytes()
        except OSError as error:
            raise RetryableDeliveryError(
                "notification artifact is unavailable: {}".format(
                    type(error).__name__)) from None
        if part_kind == "TEXT":
            try:
                text = content.decode("utf-8").strip()
            except UnicodeError:
                raise PermanentDeliveryError(
                    "notification text is not UTF-8") from None
            if not text:
                raise PermanentDeliveryError("notification text is empty")
            return {"id": identifier, "deliveryKind": "TEXT",
                    "content": text}
        if part_kind != "CHART_IMAGE":
            raise PermanentDeliveryError("unsupported notification part")
        if not content or len(content) > 20 * 1024 * 1024:
            raise PermanentDeliveryError("notification image size is invalid")
        digest = hashlib.sha256(content).hexdigest()
        asset_name = "{}.png".format(digest)
        asset = self.assets / asset_name
        if asset.exists():
            if hashlib.sha256(asset.read_bytes()).hexdigest() != digest:
                raise PermanentDeliveryError("notification asset hash collision")
        else:
            _atomic_bytes(asset, content)
        return {"id": identifier, "deliveryKind": "CHART_IMAGE",
                "assetName": asset_name, "assetSha256": digest}

    def send(self, part_kind, path, idempotency_key):
        identifier = self._identifier(idempotency_key)
        sent_path = self.runtime_root / "sent-{}.json".format(identifier)
        if sent_path.is_file():
            return "wecom-long://{}".format(identifier)
        job = self._job(part_kind, path, identifier)
        job_path = self.outbox / "{}.json".format(identifier)
        if job_path.exists():
            existing = json.loads(job_path.read_text(encoding="utf-8"))
            comparable = {key: existing.get(key) for key in job}
            if comparable != job:
                raise PermanentDeliveryError("notification queue id collision")
        else:
            _atomic_json(job_path, job)
        deadline = self.clock() + self.timeout_seconds
        while self.clock() <= deadline:
            if sent_path.is_file():
                return "wecom-long://{}".format(identifier)
            if not job_path.exists():
                raise DeliveryUnknown("WeCom queue receipt is missing")
            if self.timeout_seconds == 0:
                break
            self.sleeper(self.poll_seconds)
        raise DeliveryUnknown("WeCom long-connection delivery timed out")


def wecom_transport_from_environment(environment):
    webhook = environment.get("WECOM_WEBHOOK_URL", "")
    if webhook:
        from .ABuWeComTransport import WeComWebhookTransport
        return WeComWebhookTransport(webhook)
    runtime = environment.get("WECOM_RUNTIME_DIR", "")
    if (environment.get("WECOM_BOT_ID") and
            environment.get("WECOM_BOT_SECRET") and runtime):
        return WeComLongConnectionOutboxTransport(runtime)
    raise PermanentDeliveryError("no supported WeCom delivery channel configured")
