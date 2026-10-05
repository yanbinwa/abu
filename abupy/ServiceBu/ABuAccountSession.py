from __future__ import absolute_import

from dataclasses import dataclass

from .ABuPreopenSnapshot import validate_preopen_snapshot_row


class InvalidAccountSessionTransition(ValueError):
    """The requested account-day phase transition is not legal in v1."""


class AccountSessionVersionConflict(RuntimeError):
    """The caller based its transition on a stale phase version."""


@dataclass(frozen=True)
class AccountSessionTransition:
    account_id: str
    trading_session: int
    previous_phase: str
    phase: str
    previous_phase_version: int
    phase_version: int
    preopen_snapshot_id: str | None
    blocked_reason: str | None
    changed: bool
    idempotency_key: str


class AccountSessionStore(object):
    """Transactional account-day phase barrier.

    Every mutating method receives the caller's existing SQLite transaction.
    It never commits independently, so account projections, account events and
    the phase barrier can share the authoritative commit boundary.
    """

    PHASES = (
        "CREATED",
        "PREOPEN_INPUTS_READY",
        "RECEIVABLES_APPLIED",
        "OPEN_SELLS_PROCESSED",
        "INTRADAY_BUYS_ENABLED",
        "DAILY_CLOSE_COMPLETED",
    )
    NEXT_PHASE = dict(zip(PHASES[:-1], PHASES[1:]))

    @staticmethod
    def _session(value):
        session = int(value)
        if len(str(session)) != 8:
            raise ValueError("trading_session must use YYYYMMDD")
        return session

    @staticmethod
    def phase_idempotency_key(account_id, trading_session, target_phase,
                              input_snapshot_id=None):
        return "account-phase:{}:{}:{}:{}".format(
            account_id, int(trading_session), target_phase,
            input_snapshot_id or "none")

    @staticmethod
    def _row(connection, account_id, trading_session):
        return connection.execute(
            "SELECT * FROM account_sessions "
            "WHERE account_id=? AND trading_session=?",
            (account_id, int(trading_session))).fetchone()

    @staticmethod
    def _validate_activation(connection, context, trading_session):
        if context.active_activation_id is None:
            raise ValueError("account has no active strategy activation")
        activation = connection.execute(
            "SELECT * FROM strategy_activations "
            "WHERE activation_id=? AND account_id=?",
            (context.active_activation_id, context.account_id)).fetchone()
        if activation is None:
            raise ValueError("account active activation is invalid")
        session = int(trading_session)
        if (session < int(activation["effective_from_session"]) or
                (activation["effective_until_session"] is not None and
                 session > int(activation["effective_until_session"]))):
            raise ValueError("active activation does not cover trading session")

    @classmethod
    def create(cls, connection, context, trading_session, updated_at):
        session = cls._session(trading_session)
        cls._validate_activation(connection, context, session)
        existing = cls._row(connection, context.account_id, session)
        if existing is not None:
            return dict(existing), False
        connection.execute(
            "INSERT INTO account_sessions "
            "(account_id, trading_session, phase, preopen_snapshot_id, "
            "blocked_reason, phase_version, updated_at) "
            "VALUES (?, ?, 'CREATED', NULL, NULL, 0, ?)",
            (context.account_id, session, updated_at))
        return dict(cls._row(connection, context.account_id, session)), True

    @staticmethod
    def _validate_preopen_snapshot(connection, trading_session, snapshot_id):
        snapshot = connection.execute(
            "SELECT * FROM market_snapshots WHERE snapshot_id=?",
            (snapshot_id,)).fetchone()
        if snapshot is None:
            raise ValueError("preopen snapshot is required")
        validate_preopen_snapshot_row(snapshot, trading_session)

    @classmethod
    def transition(cls, connection, context, trading_session, target_phase,
                   expected_phase_version, updated_at, *,
                   preopen_snapshot_id=None):
        session = cls._session(trading_session)
        if target_phase not in cls.PHASES or target_phase == "CREATED":
            raise InvalidAccountSessionTransition(
                "invalid target account session phase")
        row = cls._row(connection, context.account_id, session)
        if row is None:
            raise KeyError("account session has not been created")
        current_version = int(row["phase_version"])
        if row["phase"] == target_phase:
            same_snapshot = (
                preopen_snapshot_id is None or
                row["preopen_snapshot_id"] == preopen_snapshot_id)
            if not same_snapshot:
                raise InvalidAccountSessionTransition(
                    "completed phase cannot change its input snapshot")
            return AccountSessionTransition(
                account_id=context.account_id, trading_session=session,
                previous_phase=target_phase, phase=target_phase,
                previous_phase_version=current_version,
                phase_version=current_version,
                preopen_snapshot_id=row["preopen_snapshot_id"],
                blocked_reason=row["blocked_reason"], changed=False,
                idempotency_key=cls.phase_idempotency_key(
                    context.account_id, session, target_phase,
                    row["preopen_snapshot_id"]))
        if current_version != int(expected_phase_version):
            raise AccountSessionVersionConflict(
                "account session expected phase version {} but is {}".format(
                    expected_phase_version, current_version))
        if row["blocked_reason"] is not None:
            raise InvalidAccountSessionTransition(
                "blocked account session must be explicitly unblocked")
        if cls.NEXT_PHASE.get(row["phase"]) != target_phase:
            raise InvalidAccountSessionTransition(
                "account session cannot transition from {} to {}".format(
                    row["phase"], target_phase))

        snapshot_id = row["preopen_snapshot_id"]
        if target_phase == "PREOPEN_INPUTS_READY":
            if not preopen_snapshot_id:
                raise ValueError("preopen_snapshot_id is required")
            cls._validate_preopen_snapshot(
                connection, session, preopen_snapshot_id)
            snapshot_id = preopen_snapshot_id
        elif (preopen_snapshot_id is not None and
              preopen_snapshot_id != snapshot_id):
            raise InvalidAccountSessionTransition(
                "preopen snapshot is immutable after readiness")

        next_version = current_version + 1
        updated = connection.execute(
            "UPDATE account_sessions SET phase=?, preopen_snapshot_id=?, "
            "phase_version=?, updated_at=? "
            "WHERE account_id=? AND trading_session=? AND phase_version=?",
            (target_phase, snapshot_id, next_version, updated_at,
             context.account_id, session, current_version))
        if updated.rowcount != 1:
            raise AccountSessionVersionConflict(
                "account session phase changed during transition")
        return AccountSessionTransition(
            account_id=context.account_id, trading_session=session,
            previous_phase=row["phase"], phase=target_phase,
            previous_phase_version=current_version,
            phase_version=next_version, preopen_snapshot_id=snapshot_id,
            blocked_reason=None, changed=True,
            idempotency_key=cls.phase_idempotency_key(
                context.account_id, session, target_phase, snapshot_id))

    @classmethod
    def block(cls, connection, context, trading_session,
              expected_phase_version, reason, updated_at):
        session = cls._session(trading_session)
        if not reason:
            raise ValueError("blocked reason is required")
        row = cls._row(connection, context.account_id, session)
        if row is None:
            raise KeyError("account session has not been created")
        if row["blocked_reason"] == reason:
            return dict(row), False
        current_version = int(row["phase_version"])
        if current_version != int(expected_phase_version):
            raise AccountSessionVersionConflict(
                "account session expected phase version {} but is {}".format(
                    expected_phase_version, current_version))
        connection.execute(
            "UPDATE account_sessions SET blocked_reason=?, phase_version=?, "
            "updated_at=? WHERE account_id=? AND trading_session=?",
            (reason, current_version + 1, updated_at,
             context.account_id, session))
        return dict(cls._row(connection, context.account_id, session)), True

    @classmethod
    def unblock(cls, connection, context, trading_session,
                expected_phase_version, updated_at):
        session = cls._session(trading_session)
        row = cls._row(connection, context.account_id, session)
        if row is None:
            raise KeyError("account session has not been created")
        if row["blocked_reason"] is None:
            return dict(row), False
        current_version = int(row["phase_version"])
        if current_version != int(expected_phase_version):
            raise AccountSessionVersionConflict(
                "account session expected phase version {} but is {}".format(
                    expected_phase_version, current_version))
        connection.execute(
            "UPDATE account_sessions SET blocked_reason=NULL, phase_version=?, "
            "updated_at=? WHERE account_id=? AND trading_session=?",
            (current_version + 1, updated_at, context.account_id, session))
        return dict(cls._row(connection, context.account_id, session)), True

    @classmethod
    def require_phase(cls, connection, account_id, trading_session, phase):
        if phase not in cls.PHASES:
            raise ValueError("unknown required account session phase")
        row = cls._row(connection, account_id, cls._session(trading_session))
        if row is None or row["phase"] != phase or row["blocked_reason"] is not None:
            raise InvalidAccountSessionTransition(
                "account session is not ready for {}".format(phase))
        return dict(row)
