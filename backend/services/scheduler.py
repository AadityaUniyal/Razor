import asyncio
import logging
from datetime import timedelta
from sqlalchemy import select
from database.connection import SessionLocal
from database.models import ApprovalRequest, ApprovalStatus, RecoveryCase, ScheduledTask, PaymentPromise, CaseEvent, CaseState, now_utc
from backend.services.policy_engine import latest_policy, record_action
from backend.services.recommendations import create_recommendation
from backend.services.websocket_manager import broadcast

logger = logging.getLogger(__name__)


def process_due_tasks() -> int:
    """Executes a single processing pass over due scheduled tasks."""
    processed_count = 0
    with SessionLocal() as db:
        due_ids = db.scalars(
            select(ScheduledTask.id).where(
                ScheduledTask.status == "PENDING",
                ScheduledTask.scheduled_at <= now_utc()
            ).with_for_update(skip_locked=True)
        ).all()

        for task_id in due_ids:
            try:
                task = db.get(ScheduledTask, task_id)
                if not task or task.status != "PENDING":
                    continue
                processed_count += 1
                case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == task.case_id))
                task.status = "COMPLETED"
                task.completed_at = now_utc()
                task.attempt_count += 1

                if case and not case.recovered:
                    if case.customer and case.customer.communication_opt_out:
                        task.status = "SKIPPED_OPT_OUT"
                    else:
                        promise = db.scalar(select(PaymentPromise).where(PaymentPromise.case_id_ref == case.id))
                        if promise and not promise.follow_up_sent:
                            policy = latest_policy(db, case.merchant_id).configuration or {}
                            pending_approval = db.scalar(select(ApprovalRequest).where(
                                ApprovalRequest.case_id_ref == case.id,
                                ApprovalRequest.status == ApprovalStatus.PENDING.value,
                            ))
                            if policy.get("approval_required") and not pending_approval:
                                create_recommendation(db, case, promise.promise_text, force_provider="fallback")
                                promise.status = "PENDING_APPROVAL"
                                task.status = "AWAITING_APPROVAL"
                                db.add(CaseEvent(
                                    case=case,
                                    event_type="PROMISE_APPROVAL_REQUIRED",
                                    message="Promise follow-up prepared for operator approval.",
                                    details={"approval_required": True},
                                ))
                            elif case.communication_count >= 3:
                                case.state = CaseState.STOP.value
                                case.current_action = "STOP"
                                promise.status = "EXPIRED_MAX_COMMS"
                                record_action(
                                    db, case, "STOP", "SUCCEEDED",
                                    {"message": "Communication limit reached, halting follow-up."}, cost=0
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
                                    message="Promise window expired without payment. Dispatched single bounded reminder.",
                                    details={"safe_follow_up": True}
                                ))
                db.commit()
                if case:
                    broadcast({"type": "CASE_UPDATED", "case_id": case.case_id, "new_state": case.state, "timestamp": now_utc().isoformat()})
            except Exception:
                db.rollback()
                logger.exception("Failed to process scheduled task %s", task_id)
                # Persist retry state in a fresh transaction so a failed task cannot
                # loop forever or lose its attempt counter during rollback.
                with SessionLocal() as retry_db:
                    retry_task = retry_db.get(ScheduledTask, task_id)
                    if retry_task:
                        retry_task.attempt_count += 1
                        retry_task.last_error = "Scheduled task execution failed; see worker logs."
                        if retry_task.attempt_count >= retry_task.max_attempts:
                            retry_task.status = "DEAD_LETTER"
                        else:
                            retry_task.status = "PENDING"
                            retry_task.scheduled_at = now_utc() + timedelta(minutes=2 ** retry_task.attempt_count)
                        retry_db.commit()
    return processed_count


async def scheduler_loop() -> None:
    while True:
        try:
            await asyncio.sleep(10)
            process_due_tasks()
        except Exception:
            logger.exception("Scheduled recovery task processing failed")
            await asyncio.sleep(5)
