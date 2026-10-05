from __future__ import absolute_import

import hashlib
import io
import json
from datetime import datetime, timedelta

from .ABuContentStore import ContentAddressedStore


class DeliveryUnknown(RuntimeError):
    pass


class RetryableDeliveryError(RuntimeError):
    pass


class PermanentDeliveryError(RuntimeError):
    pass


class RecordingMockTransport(object):
    """Deterministic no-network transport used by full-chain acceptance."""

    def __init__(self):
        self.messages = []

    def send(self, part_kind, path, idempotency_key):
        record = {
            "part_kind": part_kind, "path": str(path),
            "idempotency_key": idempotency_key,
            "remote_reference": "mock://{}".format(idempotency_key),
        }
        self.messages.append(record)
        return record["remote_reference"]


class TradeNotificationRenderer(object):

    @staticmethod
    def render(part_kind, notification, source_event):
        payload = json.loads(source_event["payload_json"])
        fills = payload.get("fills", [])
        short_id = notification["notification_event_id"][-12:]
        if part_kind == "TEXT":
            lines = [
                "模拟盘交易通知 `{}`".format(short_id),
                "账户：{}".format(notification["account_id"]),
                "事件：{}".format(source_event["event_type"]),
            ]
            for fill in fills:
                lines.append("{} {} {}股 @ {:.3f}".format(
                    fill.get("symbol", ""),
                    "买入" if fill.get("side", "buy") == "buy" else "卖出",
                    int(fill.get("quantity", 0)),
                    int(fill.get("fill_price_micros", 0)) / 1_000_000.0))
            lines.append("本地事件ID：{}；重复消息以此ID识别。".format(short_id))
            return ("\n\n".join(lines) + "\n").encode("utf-8"), ".md"
        if part_kind != "CHART_IMAGE":
            raise ValueError("unsupported notification part")
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        chart_bars = payload.get("chart_bars", {})
        bars = []
        for symbol in sorted(chart_bars):
            bars.extend(chart_bars[symbol])
        figure, axis = plt.subplots(figsize=(6, 3))
        if bars:
            for index, item in enumerate(bars):
                color = "#d62728" if item["close_raw"] >= item["open_raw"] else "#2ca02c"
                axis.vlines(index, item["low_raw"], item["high_raw"], color=color)
                axis.vlines(index, item["open_raw"], item["close_raw"],
                            color=color, linewidth=5)
            axis.set_xticks(range(len(bars)), [
                item["bar_end"][11:16] for item in bars])
        else:
            axis.text(.5, .5, "No minute bars", ha="center", va="center")
        for fill in fills:
            price = int(fill.get("fill_price_micros", 0)) / 1_000_000.0
            axis.axhline(price, linestyle="--", color="#1f77b4", alpha=.7)
        axis.set_title("Paper trade {}".format(short_id))
        axis.set_ylabel("raw price")
        axis.grid(alpha=.25)
        output = io.BytesIO()
        figure.tight_layout()
        figure.savefig(output, format="png", dpi=120)
        plt.close(figure)
        return output.getvalue(), ".png"


class NotificationWorker(object):

    def __init__(self, operational_store, artifact_root, transport, *,
                 renderer=None, max_attempts=5, base_backoff_seconds=5,
                 max_backoff_seconds=300):
        self.store = operational_store
        self.artifacts = ContentAddressedStore(artifact_root)
        self.transport = transport
        self.renderer = renderer or TradeNotificationRenderer()
        self.max_attempts = int(max_attempts)
        self.base_backoff_seconds = int(base_backoff_seconds)
        self.max_backoff_seconds = int(max_backoff_seconds)

    @staticmethod
    def _parse(value):
        return datetime.fromisoformat(value)

    def _next_attempt(self, notification_id, attempt, now):
        delay = min(
            self.max_backoff_seconds,
            self.base_backoff_seconds * (2 ** max(0, attempt - 1)))
        jitter = int(hashlib.sha256(
            "{}:{}".format(notification_id, attempt).encode()).hexdigest()[:4], 16)
        delay += jitter % max(1, self.base_backoff_seconds)
        return (self._parse(now) + timedelta(seconds=delay)).isoformat()

    def _refresh_outbox(self, connection, account_id, notification_id, now):
        statuses = {row[0] for row in connection.execute(
            "SELECT status FROM notification_parts "
            "WHERE account_id=? AND notification_event_id=?",
            (account_id, notification_id))}
        if statuses == {"SENT"}:
            status = "SENT"
        elif statuses & {"REQUIRES_ATTENTION"}:
            status = "REQUIRES_ATTENTION"
        elif statuses == {"ABANDONED_BY_OPERATOR"}:
            status = "ABANDONED_BY_OPERATOR"
        else:
            status = "IN_PROGRESS"
        connection.execute(
            "UPDATE notification_outbox SET status=?, updated_at=? "
            "WHERE account_id=? AND notification_event_id=?",
            (status, now, account_id, notification_id))

    def render_pending(self, now):
        rows = self.store.connection.execute(
            "SELECT p.*, o.source_event_id FROM notification_parts p "
            "JOIN notification_outbox o ON o.account_id=p.account_id "
            "AND o.notification_event_id=p.notification_event_id "
            "WHERE p.status='PENDING' ORDER BY p.notification_event_id,p.part_kind"
        ).fetchall()
        rendered = 0
        for row in rows:
            source = self.store.connection.execute(
                "SELECT * FROM domain_events WHERE event_id=?",
                (row["source_event_id"],)).fetchone()
            content, suffix = self.renderer.render(
                row["part_kind"], dict(row), dict(source))
            path, digest, unused_created = self.artifacts.write_bytes(
                "notifications", content, suffix=suffix)
            artifact_id = "artifact-{}-{}".format(
                row["notification_event_id"], row["part_kind"].lower())
            with self.store.transaction() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO render_artifacts "
                    "(artifact_id,notification_event_id,artifact_kind,path,sha256,created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (artifact_id, row["notification_event_id"],
                     row["part_kind"], str(path), digest, now))
                connection.execute(
                    "UPDATE notification_parts SET status='RENDERED',updated_at=? "
                    "WHERE account_id=? AND notification_event_id=? AND part_kind=? "
                    "AND status='PENDING'",
                    (now, row["account_id"], row["notification_event_id"],
                     row["part_kind"]))
                self._refresh_outbox(
                    connection, row["account_id"],
                    row["notification_event_id"], now)
            rendered += 1
        return rendered

    def recover_unknown(self, now):
        with self.store.transaction() as connection:
            rows = connection.execute(
                "SELECT account_id,notification_event_id FROM notification_parts "
                "WHERE status='UNKNOWN'").fetchall()
            connection.execute(
                "UPDATE notification_parts SET status='RETRY_PENDING',"
                "next_attempt_at=?,updated_at=? WHERE status='UNKNOWN'",
                (now, now))
            for row in rows:
                self._refresh_outbox(
                    connection, row["account_id"],
                    row["notification_event_id"], now)
            return len(rows)

    def _eligible(self, now):
        return self.store.connection.execute(
            "SELECT p.*,a.path FROM notification_parts p "
            "JOIN render_artifacts a ON a.notification_event_id=p.notification_event_id "
            "AND a.artifact_kind=p.part_kind "
            "WHERE p.status IN ('RENDERED','RETRY_PENDING','RETRYABLE_FAILED') "
            "AND (p.next_attempt_at IS NULL OR p.next_attempt_at<=?) "
            "ORDER BY p.notification_event_id,p.part_kind", (now,)).fetchall()

    def drain_once(self, now):
        self.render_pending(now)
        outcomes = []
        for row in self._eligible(now):
            attempt = int(row["attempt_count"]) + 1
            attempt_id = "delivery-{}-{}-{}".format(
                row["notification_event_id"], row["part_kind"].lower(), attempt)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE notification_parts SET status='SENDING',attempt_count=?,"
                    "next_attempt_at=NULL,updated_at=? WHERE account_id=? "
                    "AND notification_event_id=? AND part_kind=?",
                    (attempt, now, row["account_id"],
                     row["notification_event_id"], row["part_kind"]))
                connection.execute(
                    "INSERT INTO notification_delivery_attempts "
                    "(attempt_id,notification_event_id,account_id,part_kind,"
                    "attempt_no,status,started_at) VALUES (?,?,?,?,?,'SENDING',?)",
                    (attempt_id, row["notification_event_id"], row["account_id"],
                     row["part_kind"], attempt, now))
            try:
                remote = self.transport.send(
                    row["part_kind"], row["path"],
                    "{}:{}".format(row["notification_event_id"], row["part_kind"]))
                status, error, next_at = "SENT", None, None
            except DeliveryUnknown as exc:
                status, error, next_at, remote = "UNKNOWN", str(exc), None, None
            except RetryableDeliveryError as exc:
                status = ("REQUIRES_ATTENTION" if attempt >= self.max_attempts
                          else "RETRY_PENDING")
                error, remote = str(exc), None
                next_at = (None if status == "REQUIRES_ATTENTION" else
                           self._next_attempt(row["notification_event_id"], attempt, now))
            except PermanentDeliveryError as exc:
                status, error, next_at, remote = (
                    "REQUIRES_ATTENTION", str(exc), None, None)
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE notification_parts SET status=?,next_attempt_at=?,"
                    "last_error=?,remote_reference=?,updated_at=? WHERE account_id=? "
                    "AND notification_event_id=? AND part_kind=?",
                    (status, next_at, error, remote, now, row["account_id"],
                     row["notification_event_id"], row["part_kind"]))
                connection.execute(
                    "UPDATE notification_delivery_attempts SET status=?,"
                    "completed_at=?,error_detail=?,remote_reference=? "
                    "WHERE attempt_id=?",
                    (status, now, error, remote, attempt_id))
                self._refresh_outbox(
                    connection, row["account_id"],
                    row["notification_event_id"], now)
            outcomes.append({"notification_event_id": row["notification_event_id"],
                             "part_kind": row["part_kind"], "status": status})
        return outcomes

    def operator_action(self, notification_id, part_kind, action, now, reason,
                        operator):
        target = {"retry": "RETRY_PENDING", "abandon": "ABANDONED_BY_OPERATOR"}.get(action)
        if target is None or not reason or not operator:
            raise ValueError("operator action, identity and reason are required")
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT account_id FROM notification_parts "
                "WHERE notification_event_id=? AND part_kind=?",
                (notification_id, part_kind)).fetchone()
            if row is None:
                raise KeyError("notification part not found")
            connection.execute(
                "UPDATE notification_parts SET status=?,next_attempt_at=?,"
                "last_error=?,updated_at=? WHERE notification_event_id=? AND part_kind=?",
                (target, now if action == "retry" else None,
                 "operator {}: {}".format(action, reason), now,
                 notification_id, part_kind))
            action_id = "operator-{}".format(hashlib.sha256(
                "{}|{}|{}|{}|{}".format(
                    notification_id, part_kind, action, operator, now
                ).encode()).hexdigest()[:24])
            connection.execute(
                "INSERT INTO notification_operator_actions "
                "(action_id,notification_event_id,account_id,part_kind,action,"
                "operator,reason,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (action_id, notification_id, row["account_id"], part_kind,
                 action, operator, reason, now))
            self._refresh_outbox(
                connection, row["account_id"], notification_id, now)
