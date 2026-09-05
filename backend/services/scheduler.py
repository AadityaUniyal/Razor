import asyncio
import logging
from sqlalchemy import select
from database.connection import SessionLocal
from database.models import RecoveryCase, ScheduledTask, PaymentPromise, CaseEvent, CaseState, now_utc
from backend.services.policy_engine import record_action
from backend.services.websocket_manager import broadcast

logger = logging.getLogger(__name__)


def process_due_tasks() -> int:
    """Executes a single processing pass over due scheduled tasks."""
    processed_count = 0
    with SessionLocal() as db:
        due = db.scalars(
            select(ScheduledTask).where(
                ScheduledTask.status == "PENDING",
                ScheduledTask.scheduled_at <= now_utc()
            )
        ).all()

        for task in due:
            processed_count += 1
            case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == task.case_id))
            task.status = "COMPLETED"
            task.completed_at = now_utc()
            task.attempt_count += 1

            if case and not case.recovered:
                # Check customer opt-out first
                if case.customer and case.customer.communication_opt_out:
                    task.status = "SKIPPED_OPT_OUT"
                    continue

                promise = db.scalar(select(PaymentPromise).where(PaymentPromise.case_id_ref == case.id))
                if promise and not promise.follow_up_sent:
                    # Policy check: limit communications
                    if case.communication_count >= 3:
                        case.state = CaseState.STOP.value
                        case.current_action = "STOP"
                        promise.status = "EXPIRED_MAX_COMMS"
                        record_action(
                            db, case, "STOP", "SUCCEEDED",
                            {"message": "Communication limit reached, halting follow-up."},
                            cost=0
                        )
                    else:
                        promise.follow_up_sent = True
                        promise.status = "FOLLOW_UP_SENT"
                        case.state = CaseState.RECOVER.value
                        case.current_action = "GENERATE_RECOVERY_LINK"
                        case.communication_count += 1

                        record_action(
                            db, case, "SEND_PROMISE_FOLLOW_UP", "SENT",
                            {"message": "Promise window elapsed. Sending single bounded reminder."},
                            channel="WHATSAPP", cost=2
                        )
                        db.add(CaseEvent(
                            case=case,
                            event_type="PROMISE_CHECK_DUE",
                            message="Promise window expired without payment. Dispatched single follow-up.",
                            details={"safe_follow_up": True}
                        ))
        if due:
            db.commit()
            for task in due:
                c = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == task.case_id))
                if c:
                    broadcast({"type": "CASE_UPDATED", "case_id": c.case_id, "new_state": c.state, "timestamp": now_utc().isoformat()})
    return processed_count


async def scheduler_loop() -> None:
    while True:
        try:
            await asyncio.sleep(10)
            process_due_tasks()
        except Exception:
            logger.exception("Scheduled recovery task processing failed")
            await asyncio.sleep(5)
