from __future__ import absolute_import

import html
import hashlib
import json
import os
import sqlite3
from pathlib import Path


class DashboardQuery(object):
    """Read-only operational views backed by a query-only SQLite handle."""

    def __init__(self, database_path):
        path = Path(database_path).resolve()
        if not path.is_file():
            raise FileNotFoundError("dashboard database is missing: {}".format(path))
        self.path = path
        self.connection = sqlite3.connect(
            "file:{}?mode=ro".format(path.as_posix()), uri=True,
            isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only = ON")
        self.connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _rows(cursor):
        return [dict(row) for row in cursor.fetchall()]

    @staticmethod
    def _row(cursor):
        row = cursor.fetchone()
        return None if row is None else dict(row)

    def system_health(self):
        service = self._row(self.connection.execute(
            "SELECT *,CASE WHEN stopped_at IS NULL THEN 'RUNNING' ELSE 'STOPPED' "
            "END AS status FROM service_instances ORDER BY started_at DESC LIMIT 1"))
        jobs = self._rows(self.connection.execute(
            "SELECT d.job_id,d.category,d.critical,r.status,r.scheduled_for,"
            "r.completed_at FROM job_definitions d LEFT JOIN job_runs r "
            "ON r.job_run_id=(SELECT r2.job_run_id FROM job_runs r2 "
            "WHERE r2.job_id=d.job_id ORDER BY r2.scheduled_for DESC LIMIT 1) "
            "ORDER BY d.job_id"))
        findings = self._rows(self.connection.execute(
            "SELECT severity,category,account_id,snapshot_id,event_id,created_at "
            "FROM audit_findings WHERE resolved_at IS NULL "
            "ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 WHEN 'ERROR' THEN 1 "
            "WHEN 'WARNING' THEN 2 ELSE 3 END,created_at DESC LIMIT 100"))
        watermarks = self._rows(self.connection.execute(
            "SELECT consumer_id,stream_id,last_consumed_sequence,state,updated_at "
            "FROM stream_watermarks ORDER BY updated_at DESC LIMIT 100"))
        return {"service": service, "jobs": jobs,
                "unresolved_findings": findings, "watermarks": watermarks}

    def snapshots(self, limit=100):
        return self._rows(self.connection.execute(
            "SELECT snapshot_id,snapshot_type,stream_id,sequence_no,"
            "trading_session,decision_cutoff,status,quality_codes_json,committed_at "
            "FROM market_snapshots ORDER BY trading_session DESC,committed_at DESC "
            "LIMIT ?", (int(limit),)))

    def accounts(self):
        return self._rows(self.connection.execute(
            "SELECT a.account_id,a.account_name,a.status,a.strategy_instance_id,"
            "a.active_activation_id,a.account_version,a.updated_at,"
            "b.cash_micros,b.reserved_cash_micros,"
            "(SELECT c.equity_micros FROM account_daily_closes c "
            "WHERE c.account_id=a.account_id ORDER BY c.trading_session DESC LIMIT 1) "
            "AS latest_equity_micros,"
            "(SELECT c.trading_session FROM account_daily_closes c "
            "WHERE c.account_id=a.account_id ORDER BY c.trading_session DESC LIMIT 1) "
            "AS latest_close_session "
            "FROM accounts a LEFT JOIN account_balances b "
            "ON b.account_id=a.account_id ORDER BY a.account_id"))

    def account(self, account_id):
        values = (str(account_id),)
        account = self._row(self.connection.execute(
            "SELECT a.*,b.cash_micros,b.reserved_cash_micros,b.updated_at AS balance_updated_at "
            "FROM accounts a LEFT JOIN account_balances b ON b.account_id=a.account_id "
            "WHERE a.account_id=?", values))
        if account is None:
            raise KeyError("unknown account_id: {}".format(account_id))
        queries = {
            "sessions": "SELECT * FROM account_sessions WHERE account_id=? ORDER BY trading_session DESC",
            "positions": "SELECT * FROM positions WHERE account_id=? ORDER BY symbol",
            "lots": "SELECT * FROM position_lots WHERE account_id=? ORDER BY symbol,opened_at",
            "trades": "SELECT * FROM logical_trades WHERE account_id=? ORDER BY opened_at DESC",
            "orders": "SELECT * FROM orders WHERE account_id=? ORDER BY created_at DESC",
            "fills": "SELECT * FROM fills WHERE account_id=? ORDER BY occurred_at DESC",
            "risk_decisions": "SELECT * FROM risk_decisions WHERE account_id=? ORDER BY created_at DESC",
            "daily_closes": "SELECT * FROM account_daily_closes WHERE account_id=? ORDER BY trading_session DESC",
            "reconciliations": "SELECT * FROM reconciliation_runs WHERE account_id=? ORDER BY trading_session DESC",
            "events": "SELECT * FROM account_events WHERE account_id=? ORDER BY account_version DESC LIMIT 200",
            "notifications": "SELECT * FROM notification_outbox WHERE account_id=? ORDER BY created_at DESC",
        }
        result = {"account": account}
        for name, query in queries.items():
            result[name] = self._rows(self.connection.execute(query, values))
        return result

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _atomic_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / ("." + path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class StaticDashboardRenderer(object):

    STYLE = """
body{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;margin:2rem;color:#20242a}
table{border-collapse:collapse;width:100%;margin:1rem 0;font-size:13px}th,td{border:1px solid #ddd;padding:.45rem;text-align:left;vertical-align:top}th{background:#f2f4f7}code{font-size:12px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1rem}.card{border:1px solid #ddd;border-radius:8px;padding:1rem}a{color:#0759b5}.muted{color:#667085}
"""

    @staticmethod
    def _account_page(account_id):
        digest = hashlib.sha256(str(account_id).encode("utf-8")).hexdigest()[:20]
        return "account-{}.html".format(digest)

    @staticmethod
    def _value(value):
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return html.escape(str(value))

    @classmethod
    def _table(cls, rows, empty="暂无数据"):
        if not rows:
            return '<p class="muted">{}</p>'.format(html.escape(empty))
        columns = list(rows[0])
        head = "".join("<th>{}</th>".format(html.escape(name))
                       for name in columns)
        body = []
        for row in rows:
            body.append("<tr>{}</tr>".format("".join(
                "<td>{}</td>".format(cls._value(row.get(name)))
                for name in columns)))
        return "<table><thead><tr>{}</tr></thead><tbody>{}</tbody></table>".format(
            head, "".join(body))

    @classmethod
    def _page(cls, title, body):
        return "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>" \
            "<meta name='viewport' content='width=device-width,initial-scale=1'>" \
            "<title>{}</title><style>{}</style></head><body>{}</body></html>".format(
                html.escape(title), cls.STYLE, body)

    def build(self, query, output_root):
        output_root = Path(output_root)
        health = query.system_health()
        accounts = query.accounts()
        snapshots = query.snapshots()
        service = health["service"] or {}
        account_rows = []
        for item in accounts:
            row = dict(item)
            row["account_id"] = '<a href="accounts/{}">{}</a>'.format(
                self._account_page(item["account_id"]),
                html.escape(str(item["account_id"])))
            account_rows.append(row)

        def trusted_table(rows):
            if not rows:
                return self._table(rows)
            columns = list(rows[0])
            head = "".join("<th>{}</th>".format(html.escape(name))
                           for name in columns)
            body = "".join("<tr>{}</tr>".format("".join(
                "<td>{}</td>".format(
                    row[name] if name == "account_id" else self._value(row[name]))
                for name in columns)) for row in rows)
            return "<table><thead><tr>{}</tr></thead><tbody>{}</tbody></table>".format(
                head, body)

        index = "<h1>ABu 模拟盘只读控制台</h1>"
        index += "<p class='muted'>生成式静态页面，不包含账户写入接口。</p>"
        index += "<div class='cards'><div class='card'><h3>服务</h3><p>{}</p>" \
            "<p>{}</p></div><div class='card'><h3>未解决审计项</h3><p>{}</p>" \
            "</div><div class='card'><h3>行情快照</h3><p>{}</p></div></div>".format(
                self._value(service.get("status", "UNKNOWN")),
                self._value(service.get("service_instance_id", "")),
                len(health["unresolved_findings"]), len(snapshots))
        index += "<h2>账户</h2>" + trusted_table(account_rows)
        index += "<h2>作业</h2>" + self._table(health["jobs"])
        index += "<h2>最新快照</h2>" + self._table(snapshots)
        index += "<h2>未解决审计项</h2>" + self._table(
            health["unresolved_findings"])
        _atomic_text(output_root / "index.html", self._page("ABu 模拟盘", index))

        for summary in accounts:
            detail = query.account(summary["account_id"])
            body = "<p><a href='../index.html'>返回首页</a></p>"
            body += "<h1>账户 {}</h1>".format(
                html.escape(str(summary["account_id"])))
            body += "<h2>账户状态</h2>" + self._table([detail["account"]])
            for name in ("sessions", "positions", "lots", "trades", "orders",
                         "fills", "risk_decisions", "daily_closes",
                         "reconciliations", "notifications", "events"):
                body += "<h2>{}</h2>{}".format(
                    html.escape(name), self._table(detail[name]))
            _atomic_text(
                output_root / "accounts" /
                self._account_page(summary["account_id"]),
                self._page("账户 {}".format(summary["account_id"]), body))
        return {"index_path": str(output_root / "index.html"),
                "account_pages": len(accounts), "snapshot_rows": len(snapshots)}
