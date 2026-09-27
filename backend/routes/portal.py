"""
Customer Self-Service Recovery Portal routes.
Provides customer-facing payment pages, promise-to-pay scheduling, and communication preference controls.
"""
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.core.security import decrypt_secret
from backend.services.policy_engine import latest_policy
from backend.services.razorpay_provider import RazorpayProvider
from backend.services.websocket_manager import broadcast
from database.connection import get_db
from database.models import (
    CaseEvent,
    CaseState,
    Customer,
    IntegrationCredential,
    Merchant,
    PaymentPromise,
    RecoveryCase,
    ScheduledTask,
    now_utc,
)

router = APIRouter(tags=["customer-portal"])


def mask_string(s: Optional[str]) -> str:
    if not s or len(s) < 3:
        return "***"
    return s[0] + "*" * (len(s) - 2) + s[-1]


def mask_email(email: Optional[str]) -> str:
    if not email or "@" not in email:
        return "***@***.***"
    user_part, domain = email.split("@", 1)
    return mask_string(user_part) + "@" + domain


@router.get("/api/portal/{case_id}")
def get_portal_case_details(case_id: str, db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Public customer portal endpoint: returns recovery details, payment links,
    and current status for the customer to complete payment or request time.
    """
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id))
    if not case:
        raise HTTPException(404, "Payment case not found")

    merchant = db.get(Merchant, case.merchant_id)
    merchant_name = merchant.name if merchant else "Merchant Billing"

    # Generate or retrieve active payment link
    credential = db.scalar(
        select(IntegrationCredential)
        .where(IntegrationCredential.merchant_id == case.merchant_id, IntegrationCredential.provider == "razorpay")
        .order_by(desc(IntegrationCredential.id))
    )
    key_id = credential.key_id if credential else None
    key_secret = decrypt_secret(credential.secret_ref) if (credential and credential.secret_ref) else None
    provider = RazorpayProvider(key_id=key_id, key_secret=key_secret)

    plink = provider.create_payment_link(
        amount=case.amount,
        customer_name=case.customer_name,
        customer_email=case.customer_email,
        case_id=case.case_id,
        description=f"Payment for {merchant_name} (Ref: {case.case_id})",
    )
    payment_url = plink.get("short_url") or f"https://rzp.io/i/{case.case_id.lower()}"

    active_promise = db.scalar(
        select(PaymentPromise)
        .where(PaymentPromise.case_id_ref == case.id)
        .order_by(desc(PaymentPromise.id))
    )

    is_opted_out = bool(case.customer and case.customer.communication_opt_out)

    return {
        "case_id": case.case_id,
        "amount": case.amount,
        "currency": "INR",
        "merchant_name": merchant_name,
        "customer_name": mask_string(case.customer_name),
        "customer_email": mask_email(case.customer_email),
        "state": case.state,
        "recovered": case.recovered,
        "recovered_amount": case.recovered_amount,
        "payment_url": payment_url,
        "created_at": case.created_at.isoformat() if case.created_at else None,
        "opted_out": is_opted_out,
        "promise": {
            "promised_at": active_promise.promised_at.isoformat(),
            "status": active_promise.status,
            "promise_text": active_promise.promise_text,
        } if active_promise and active_promise.status != "EXPIRED" else None,
    }


@router.post("/api/portal/{case_id}/promise")
def portal_submit_promise(
    case_id: str,
    payload: Dict[str, Any],
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Customer self-service: promise to pay by a specific future date."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id))
    if not case:
        raise HTTPException(404, "Payment case not found")
    if case.recovered:
        raise HTTPException(400, "Case has already been recovered")

    promised_at_str = payload.get("promised_at")
    if not promised_at_str:
        raise HTTPException(400, "promised_at date is required")

    try:
        promised_at = datetime.fromisoformat(promised_at_str.replace("Z", "+00:00"))
        if promised_at.tzinfo is None:
            promised_at = promised_at.replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HTTPException(400, "Invalid date format. Use ISO format (YYYY-MM-DD)") from exc

    current_time = now_utc()
    if promised_at < current_time:
        raise HTTPException(400, "Promise date cannot be in the past")
    if promised_at > current_time + timedelta(days=45):
        raise HTTPException(400, "Promise date cannot be more than 45 days in the future")

    note = payload.get("note", "Customer promised payment via self-service portal")

    promise = db.scalar(select(PaymentPromise).where(PaymentPromise.case_id_ref == case.id))
    if promise:
        promise.promised_at = promised_at
        promise.promise_text = note
        promise.status = "PENDING"
        promise.follow_up_sent = False
    else:
        promise = PaymentPromise(
            case_id_ref=case.id,
            customer_name=case.customer_name,
            amount=case.amount,
            promise_text=note,
            promised_at=promised_at,
            confidence=1.0,
            status="PENDING",
        )
        db.add(promise)

    case.state = CaseState.WAIT.value
    case.current_action = "AWAITING_PROMISE"

    # Schedule verification check after promise date
    task = db.scalar(select(ScheduledTask).where(
        ScheduledTask.case_id == case.case_id,
        ScheduledTask.task_type == "VERIFY_PROMISE",
        ScheduledTask.status == "PENDING"
    ))
    check_time = promised_at + timedelta(hours=12)
    if task:
        task.scheduled_at = check_time
    else:
        db.add(ScheduledTask(
            case_id=case.case_id,
            task_type="VERIFY_PROMISE",
            scheduled_at=check_time,
            idempotency_key=f"promise_verify_{case.case_id}_{int(promised_at.timestamp())}",
        ))

    db.add(CaseEvent(
        case=case,
        event_type="CUSTOMER_PROMISE_RECORDED",
        message=f"Customer registered commitment to pay by {promised_at.strftime('%Y-%m-%d')}.",
        details={"promised_at": promised_at.isoformat(), "note": note},
    ))
    db.commit()

    broadcast({
        "type": "CASE_UPDATED",
        "case_id": case.case_id,
        "state": case.state,
        "timestamp": now_utc().isoformat(),
    })

    return {
        "ok": True,
        "status": "PROMISE_RECORDED",
        "promised_at": promised_at.isoformat(),
        "message": f"Thank you! Your payment window has been reserved until {promised_at.strftime('%B %d, %Y')}.",
    }


@router.post("/api/portal/{case_id}/opt-out")
def portal_customer_opt_out(
    case_id: str,
    payload: Dict[str, Any] = {},
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Customer self-service: opt out from receiving further automated payment reminders."""
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id))
    if not case:
        raise HTTPException(404, "Payment case not found")

    if case.customer:
        case.customer.communication_opt_out = True

    db.add(CaseEvent(
        case=case,
        event_type="CUSTOMER_OPT_OUT",
        message="Customer requested communication opt-out via self-service portal.",
        details={"channel": payload.get("channel", "PORTAL")},
    ))
    db.commit()

    broadcast({
        "type": "CUSTOMER_OPT_OUT",
        "case_id": case.case_id,
        "timestamp": now_utc().isoformat(),
    })

    return {
        "ok": True,
        "status": "OPTED_OUT",
        "message": "You have been unsubscribed from automated payment recovery notifications.",
    }


@router.get("/portal/{case_id}", response_class=HTMLResponse)
def portal_html_view(case_id: str, request: Request, db: Session = Depends(get_db)):
    """Serves the responsive customer recovery portal web page."""
    portal_file = Path(__file__).resolve().parent.parent.parent / "frontend" / "portal.html"
    if portal_file.exists():
        with open(portal_file, "r", encoding="utf-8") as f:
            template = f.read()
    else:
        # Fallback minimal embedded HTML template
        template = """<!doctype html>
<html><head><title>Payment Recovery Portal</title></head>
<body><div id="app">Loading portal...</div></body></html>"""

    # Inject case_id into script tag for instant hydration
    return HTMLResponse(template.replace("{{CASE_ID}}", case_id))
