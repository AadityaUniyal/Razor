"""
Scheduler API routes for triggering recovery cycles, cron sweeps, and inspecting queues.
Supports Vercel cron headers, external cron services, and manual operator runs.
"""
import os
from hmac import compare_digest
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.security import get_current_user_optional
from backend.services.scheduler import run_scheduler_cycle
from database.connection import get_db
from database.models import ActionRecord, Notification, ScheduledTask, User, now_utc

router = APIRouter(prefix="/api/scheduler", tags=["scheduler"])


def verify_cron_or_admin(
    request: Request,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> bool:
    """
    Authorizes cron ticks via:
    1. Vercel Cron secret (CRON_SECRET) in Authorization header
    2. Admin/Operator authenticated user
    3. Open if CRON_SECRET is not configured (development/local test mode)
    """
    configured_cron_secret = os.getenv("CRON_SECRET", "").strip()
    if configured_cron_secret:
        if authorization:
            token = authorization.replace("Bearer ", "").strip()
            if compare_digest(token, configured_cron_secret):
                return True
        # Check current user
        user = get_current_user_optional(request, db)
        if user and user.role in {"ADMIN", "OPERATOR"}:
            return True
        raise HTTPException(status_code=401, detail="Unauthorized cron tick")
    return True


@router.post("/tick")
@router.get("/tick")  # Allow GET for web cron / Vercel crons
def trigger_scheduler_tick(
    db: Session = Depends(get_db),
    authorized: bool = Depends(verify_cron_or_admin),
) -> Dict[str, Any]:
    """Runs a complete tick of the recovery scheduler across tasks, actions, and dispatches."""
    cycle_result = run_scheduler_cycle(db)
    return {"ok": True, "cycle": cycle_result}


@router.get("/status")
def scheduler_queue_status(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Returns queue depths for background tasks, pending actions, and dispatches."""
    pending_tasks = db.scalar(
        select(func.count(ScheduledTask.id)).where(
            ScheduledTask.status == "PENDING",
            ScheduledTask.scheduled_at <= now_utc(),
        )
    ) or 0
    pending_actions = db.scalar(
        select(func.count(ActionRecord.id)).where(ActionRecord.status == "PENDING_EXECUTION")
    ) or 0
    pending_dispatches = db.scalar(
        select(func.count(Notification.id)).where(Notification.status == "PENDING_DISPATCH")
    ) or 0

    return {
        "status": "healthy",
        "pending_tasks": pending_tasks,
        "pending_actions": pending_actions,
        "pending_dispatches": pending_dispatches,
        "checked_at": now_utc().isoformat(),
    }
