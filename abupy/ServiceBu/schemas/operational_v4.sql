CREATE TABLE IF NOT EXISTS notification_delivery_attempts (
    attempt_id TEXT PRIMARY KEY,
    notification_event_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    part_kind TEXT NOT NULL,
    attempt_no INTEGER NOT NULL CHECK (attempt_no > 0),
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_detail TEXT,
    remote_reference TEXT,
    FOREIGN KEY (account_id, notification_event_id)
        REFERENCES notification_outbox(account_id, notification_event_id),
    UNIQUE (notification_event_id, part_kind, attempt_no)
);

CREATE TABLE IF NOT EXISTS notification_operator_actions (
    action_id TEXT PRIMARY KEY,
    notification_event_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    part_kind TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('retry', 'abandon')),
    operator TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (account_id, notification_event_id)
        REFERENCES notification_outbox(account_id, notification_event_id)
);
