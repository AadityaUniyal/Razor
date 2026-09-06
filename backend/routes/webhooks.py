import json
from hashlib import sha256
from hmac import compare_digest, new as hmac_new
from typing import Any, Optional
import os
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import RAZORPAY_WEBHOOK_SECRET, INGEST_API_KEY
from backend.core.security import get_current_user
from backend.core.tenancy import default_merchant
from backend.services.policy_engine import to_case_dict, process_case
from backend.services.websocket_manager import broadcast
from database.connection import get_db
from database.models import Customer, PaymentEvent, RecoveryCase, CaseEvent, SystemHealthEvent, SubscriptionContext, InvoiceContext, User, now_utc

router = APIRouter(prefix="/api", tags=["webhooks"])


def verify_ingest_auth(
    request: Request,
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    Authenticate event ingestion requests via either:
    1. X-API-Key header matching INGEST_API_KEY (or API_KEY)
    2. Valid authenticated user session (Bearer token or session cookie)

    Unauthenticated or invalid requests MUST return HTTP 401.
    """
    api_key = request.headers.get("X-API-Key", "").strip()
    configured_key = os.getenv("INGEST_API_KEY", os.getenv("API_KEY", INGEST_API_KEY)).strip()

    if api_key:
        if configured_key and compare_digest(api_key, configured_key):
            return None  # Authenticated via valid API Key
        raise HTTPException(status_code=401, detail="Invalid API key")

    try:
        return get_current_user(request, db)
    except HTTPException:
        raise HTTPException(
            status_code=401,
            detail="API key or session authentication required",
        )


@router.post("/events/ingest")
@router.post("/demo/webhook")  # Backwards-compatible alias for existing test suites
def ingest_recovery_event(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    auth: Any = Depends(verify_ingest_auth),
):
    """
    Standard event ingestion endpoint with strict event-level idempotency
    and terminal state protection.
    """
    external_event_id = payload.get("external_event_id")
    if not external_event_id:
        raise HTTPException(400, "external_event_id is required")

    merchant_id = getattr(auth, "merchant_id", None) or default_merchant(db).id

    # 1. Event-level Idempotency Check
    existing_event = db.scalar(select(PaymentEvent).where(PaymentEvent.external_event_id == external_event_id))
    if existing_event:
        db.add(SystemHealthEvent(service_name="webhook", status="DUPLICATE_EVENT_SAFELY_IGNORED", details={"external_event_id": external_event_id}))
        db.commit()
        return {"ok": True, "duplicate": True, "message": "Event already processed"}

    case_ref = payload.get("case_reference")
    if not case_ref:
        raise HTTPException(400, "case_reference is required")

    amount = payload.get("amount")
    if amount is None:
        raise HTTPException(400, "amount is required")
    try:
        amount = int(amount)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "amount must be an integer") from exc
    if amount < 0:
        raise HTTPException(400, "amount cannot be negative")

    case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_ref))

    if not case:
        customer_email = payload.get("customer_email") or f"{case_ref.lower()}@customer.local"
        customer_key = payload.get("external_customer_id") or customer_email.lower()
        customer = db.scalar(
            select(Customer).where(Customer.external_customer_id == str(customer_key))
        )
        if not customer:
            customer = Customer(
                merchant_id=merchant_id,
                external_customer_id=str(customer_key),
                name=payload.get("customer_name", "Unknown customer"),
                email=customer_email,
                phone=payload.get("customer_phone"),
            )
            db.add(customer)
            db.flush()
        case = RecoveryCase(
            merchant_id=merchant_id,
            case_id=case_ref,
            customer_id_ref=customer.id,
            customer_name=customer.name,
            customer_email=customer.email,
            amount=amount,
            failure_category=payload.get("failure_category", "UNCERTAIN_OR_TEMPORARY"),
            external_payment_id=payload.get("external_payment_id"),
            external_subscription_id=payload.get("external_subscription_id"),
        )
        db.add(case)
        db.flush()

        subscription_payload = payload.get("subscription") or {}
        if payload.get("external_subscription_id") or subscription_payload:
            case.subscription = SubscriptionContext(
                external_subscription_id=payload.get("external_subscription_id") or subscription_payload.get("id"),
                plan_id=subscription_payload.get("plan_id"),
                plan_name=subscription_payload.get("plan_name"),
                billing_period=subscription_payload.get("billing_period"),
                billing_cycle=subscription_payload.get("billing_cycle"),
                payment_method=subscription_payload.get("payment_method"),
                subscription_status=subscription_payload.get("status"),
                customer_lifetime_value=int(payload.get("customer_lifetime_value") or 0),
                churn_risk=float(payload.get("churn_risk") or 0),
                extra_data=subscription_payload,
            )
        invoice_payload = payload.get("invoice") or {}
        if invoice_payload or payload.get("external_invoice_id"):
            case.invoice = InvoiceContext(
                external_invoice_id=payload.get("external_invoice_id") or invoice_payload.get("id"),
                invoice_number=invoice_payload.get("number"),
                amount_due=int(invoice_payload.get("amount_due") or amount),
                amount_paid=int(invoice_payload.get("amount_paid") or 0),
                currency=invoice_payload.get("currency") or "INR",
                status=invoice_payload.get("status") or "OPEN",
                extra_data=invoice_payload,
            )

    event_type = payload.get("event_type", "payment.failed")
    normalized = event_type.upper()

    # Store raw event
    db.add(PaymentEvent(
        merchant_id=merchant_id,
        external_event_id=external_event_id,
        event_type=event_type,
        case_reference=case_ref,
        payload=payload,
        signature_valid=True,
    ))

    # 2. Out-of-Order Terminal State Protection:
    # A late failure event MUST NEVER reopen a verified, terminal RECOVERED case!
    if case.recovered and ("FAIL" in normalized or "PENDING" in normalized or "HALT" in normalized):
        db.add(CaseEvent(
            case=case,
            event_type="LATE_EVENT_IGNORED",
            message="Ignored late failure event because case is already verified as RECOVERED.",
            details=payload
        ))
        db.add(SystemHealthEvent(
            service_name="reconciliation",
            status="OUT_OF_ORDER_EVENT_DEFLECTED",
            details={"case_id": case.case_id, "event_type": event_type}
        ))
        db.commit()
        broadcast({"type": "CASE_UPDATED", "case_id": case.case_id, "new_state": case.state, "timestamp": now_utc().isoformat()})
        return {"ok": True, "late_event_ignored": True, "case": to_case_dict(case)}

    db.add(CaseEvent(case=case, event_type=event_type, message=payload.get("message", f"Received {event_type}"), details=payload))

    result = process_case(db, case, event_type, payload.get("message", ""))
    return {"ok": True, "case": to_case_dict(case), "result": result}


@router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: Session = Depends(get_db)):
    """
    Razorpay-compatible webhook receiver with HMAC-SHA256 signature verification.
    """
    raw_body = await request.body()
    # Retrieve the webhook secret at request time to respect runtime environment changes
    secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(500, "Razorpay webhook secret not configured")
    signature = request.headers.get("X-Razorpay-Signature", "")
    expected = hmac_new(secret.encode("utf-8"), raw_body, "sha256").hexdigest()
    if not compare_digest(signature, expected):
        raise HTTPException(401, "Invalid Razorpay webhook signature")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "Webhook payload must be valid JSON") from exc

    event_name = payload.get("event") or payload.get("event_type", "payment.failed")
    payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    sub_entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    external_id = payload.get("id") or payload.get("event_id") or payload.get("external_event_id")
    if not external_id:
        external_id = f"rzp_evt_{sha256(raw_body).hexdigest()[:24]}"

    case_ref = payment_entity.get("order_id") or sub_entity.get("id") or payload.get("case_reference") or f"RC_{external_id[-8:]}"
    
    # Handle amounts in paise safely
    raw_amount = payment_entity.get("amount") or sub_entity.get("plan_amount") or payload.get("amount")
    if raw_amount is None:
        raise HTTPException(400, "Webhook amount is required")
    amount_inr = int(raw_amount)
    # Razorpay entities use paise; generic internal payloads declare units.
    if "amount" in payment_entity or "plan_amount" in sub_entity or str(payload.get("currency_unit", "inr")).lower() in {"paise", "minor"}:
        amount_inr //= 100

    notes = payment_entity.get("notes", {}) or sub_entity.get("notes", {})
    customer_email = payment_entity.get("email") or sub_entity.get("customer_email") or "unknown@example.invalid"
    customer_name = notes.get("customer_name") or customer_email
    external_customer_id = payment_entity.get("customer_id") or sub_entity.get("customer_id") or customer_email

    normalized = {
        "external_event_id": external_id,
        "event_type": event_name,
        "case_reference": case_ref,
        "amount": amount_inr,
        "customer_name": customer_name,
        "customer_email": customer_email,
        "external_customer_id": external_customer_id,
        "message": payload.get("message", ""),
        "failure_category": payload.get("failure_category", "UNCERTAIN_OR_TEMPORARY"),
        "external_payment_id": payment_entity.get("id"),
        "external_subscription_id": sub_entity.get("id"),
    }
    return ingest_recovery_event(normalized, db, auth="razorpay_signature_verified")


# Backwards-compatible alias for existing test suites & callers
demo_webhook = ingest_recovery_event
