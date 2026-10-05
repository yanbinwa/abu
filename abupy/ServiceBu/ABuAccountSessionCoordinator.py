from __future__ import absolute_import

from dataclasses import asdict

from .ABuAccountSession import AccountSessionStore
from .ABuTransactionalAccount import AccountEventEffects


class AccountSessionCoordinator(object):
    """Bind account-day phase changes to idempotent input event commits."""

    EVENT_TYPES = {
        "PREOPEN_INPUTS_READY": "PreopenInputsAccepted",
        "RECEIVABLES_APPLIED": "ReceivablesAndCorporateActionsApplied",
        "OPEN_SELLS_PROCESSED": "OpenSellsProcessed",
        "INTRADAY_BUYS_ENABLED": "IntradayBuysEnabled",
        "DAILY_CLOSE_COMPLETED": "DailyCloseCompleted",
    }

    def __init__(self, accounts, sessions=None):
        self.accounts = accounts
        self.sessions = sessions or AccountSessionStore()

    def _advance(self, account_id, event_id, expected_account_version,
                 trading_session, target_phase, expected_phase_version,
                 processed_at, *, preopen_snapshot_id=None,
                 apply_domain_changes=None, notification_parts=(),
                 affected_trade_ids=(),
                 affected_management_activation_ids=()):
        session = int(trading_session)
        if target_phase not in self.EVENT_TYPES:
            raise ValueError("unsupported coordinated account phase")
        if (apply_domain_changes is not None and
                not callable(apply_domain_changes)):
            raise TypeError("phase domain changes must be callable")

        def handler(connection, context, input_event):
            if (target_phase == "INTRADAY_BUYS_ENABLED" and
                    context.status not in ("SHADOW", "PAPER")):
                raise ValueError(
                    "paused account cannot enable intraday buys")
            if int(input_event["trading_session"] or 0) != session:
                raise ValueError("phase event trading session mismatch")
            if (target_phase == "PREOPEN_INPUTS_READY" and
                    input_event["snapshot_id"] != preopen_snapshot_id):
                raise ValueError("phase event does not reference preopen snapshot")
            transition = self.sessions.transition(
                connection, context, session, target_phase,
                expected_phase_version, processed_at,
                preopen_snapshot_id=preopen_snapshot_id)
            if not transition.changed:
                raise ValueError(
                    "phase was completed by a different input event")
            domain_value = (
                {} if apply_domain_changes is None else
                apply_domain_changes(connection, context, input_event))
            resolved_notification_parts = (
                notification_parts(domain_value)
                if callable(notification_parts) else notification_parts)
            resolved_trade_ids = (
                affected_trade_ids(domain_value)
                if callable(affected_trade_ids) else affected_trade_ids)
            resolved_activation_ids = (
                affected_management_activation_ids(domain_value)
                if callable(affected_management_activation_ids)
                else affected_management_activation_ids)
            payload = {
                "account_id": account_id,
                "trading_session": session,
                "transition": asdict(transition),
                "domain_value": domain_value,
            }
            return AccountEventEffects(
                account_event_type=self.EVENT_TYPES[target_phase],
                account_event_payload=payload,
                value={
                    "phase": target_phase,
                    "phase_version": transition.phase_version,
                    "idempotency_key": transition.idempotency_key,
                    "domain_value": domain_value,
                },
                notification_parts=tuple(resolved_notification_parts),
                affected_trade_ids=tuple(resolved_trade_ids),
                affected_management_activation_ids=tuple(
                    resolved_activation_ids),
                trading_session=session)

        return self.accounts.execute_event(
            account_id, event_id, expected_account_version, processed_at,
            handler)

    def accept_preopen_inputs(
            self, account_id, event_id, expected_account_version,
            trading_session, expected_phase_version, preopen_snapshot_id,
            processed_at):
        return self._advance(
            account_id, event_id, expected_account_version, trading_session,
            "PREOPEN_INPUTS_READY", expected_phase_version, processed_at,
            preopen_snapshot_id=preopen_snapshot_id)

    def apply_receivables_and_corporate_actions(
            self, account_id, event_id, expected_account_version,
            trading_session, expected_phase_version, processed_at,
            apply_domain_changes):
        return self._advance(
            account_id, event_id, expected_account_version, trading_session,
            "RECEIVABLES_APPLIED", expected_phase_version, processed_at,
            apply_domain_changes=apply_domain_changes)

    def process_open_sells(
            self, account_id, event_id, expected_account_version,
            trading_session, expected_phase_version, processed_at,
            apply_domain_changes, notification_parts=(),
            affected_trade_ids=(), affected_management_activation_ids=()):
        return self._advance(
            account_id, event_id, expected_account_version, trading_session,
            "OPEN_SELLS_PROCESSED", expected_phase_version, processed_at,
            apply_domain_changes=apply_domain_changes,
            notification_parts=notification_parts,
            affected_trade_ids=affected_trade_ids,
            affected_management_activation_ids=(
                affected_management_activation_ids))

    def enable_intraday_buys(
            self, account_id, event_id, expected_account_version,
            trading_session, expected_phase_version, processed_at):
        return self._advance(
            account_id, event_id, expected_account_version, trading_session,
            "INTRADAY_BUYS_ENABLED", expected_phase_version, processed_at)
