import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.run_abu_service import build_handlers


class _Service(object):

    def __init__(self, config):
        self.config = config
        self.store = None


def _args(**overrides):
    values = {
        "enable_daily_shadow": False,
        "enable_minute_shadow": False,
        "enable_paper_shadow": False,
        "paper_account_id": None,
        "execution_policy_id": "M1",
        "enable_notifications": False,
        "enable_dashboard": False,
        "wecom_env": Path("/does/not/exist"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ServiceEntrypointTest(unittest.TestCase):

    def test_default_registers_no_mutating_or_external_handler(self):
        self.assertEqual({}, build_handlers(_Service({}), _args()))

    def test_paper_shadow_requires_admitted_account_writes(self):
        with self.assertRaisesRegex(ValueError, "account_writes_enabled"):
            build_handlers(_Service({"account_writes_enabled": False}),
                           _args(enable_paper_shadow=True,
                                 paper_account_id="account-a"))

    def test_paper_shadow_handler_is_explicitly_registered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            minute_config = root / "minute.json"
            minute_config.write_text(
                '{"minute_store_root":"/tmp/minutes",'
                '"trading_calendar_path":"/tmp/calendar.json"}',
                encoding="utf-8")
            service = _Service({
                "account_writes_enabled": True,
                "minute_shadow_config_path": str(minute_config),
                "snapshot_root": str(root / "snapshots"),
            })
            handlers = build_handlers(
                service, _args(enable_paper_shadow=True,
                               paper_account_id="account-a"))
            self.assertEqual(
                {"minute.execute_paper_shadow"}, set(handlers))

    def test_notification_handler_requires_explicit_flag_and_local_secret(self):
        previous = os.environ.get("WECOM_WEBHOOK_URL")
        try:
            os.environ["WECOM_WEBHOOK_URL"] = (
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")
            handlers = build_handlers(
                _Service({"notification_artifact_root": "/tmp/notifications"}),
                _args(enable_notifications=True))
            self.assertEqual({"notification.deliver"}, set(handlers))
        finally:
            if previous is None:
                os.environ.pop("WECOM_WEBHOOK_URL", None)
            else:
                os.environ["WECOM_WEBHOOK_URL"] = previous

    def test_dashboard_handler_is_read_only_and_explicit(self):
        handlers = build_handlers(
            _Service({"database_path": "/tmp/read-only.sqlite3",
                      "dashboard_root": "/tmp/dashboard"}),
            _args(enable_dashboard=True))
        self.assertEqual({"dashboard.build"}, set(handlers))


if __name__ == "__main__":
    unittest.main()
