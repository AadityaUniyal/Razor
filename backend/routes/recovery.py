from typing import Any, Optional
from hashlib import sha256

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session, joinedload

from backend.core.security import get_current_user
from backend.core.tenancy import merchant_id_for_user, tenant_filter
from backend.services.razorpay_provider import RazorpayProvider
from backend.services.recommendations import approve_recommendation, create_recommendation, recommendation_context
from backend.services.policy_engine import mark_recovered
from database.connection import get_db
from database.models import AIRecommendation, ApprovalRequest, ApprovalStatus, CaseEvent, Customer, ExperimentAssignment, IntegrationCredential, Merchant, PaymentEvent, ProviderVerification, RecoveryCase, RecoveryOutcome, User, now_utc

router = APIRouter(prefix="/api", tags=["recovery-product"])


@router.get("/merchant")
def merchant_profile(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    merchant = db.get(Merchant, merchant_id_for_user(db, user))
    return {"id": merchant.id, "name": merchant.name, "slug": merchant.slug, "environment": merchant.environment, "timezone": merchant.timezone, "brand_config": merchant.brand_config}


@router.patch("/merchant")
def update_merchant(payload: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != "ADMIN":
        raise HTTPException(403, "Admin role required")
    merchant = db.get(Merchant, merchant_id_for_user(db, user))
    if not merchant:
        raise HTTPException(404, "Merchant workspace not found")
    if payload.get("name"):
        merchant.name = str(payload["name"]).strip()[:160]
    if payload.get("timezone"):
        merchant.timezone = str(payload["timezone"]).strip()[:64]
    if isinstance(payload.get("brand_config"), dict):
        merchant.brand_config = payload["brand_config"]
    db.commit()
    return {"ok": True, "name": merchant.name, "slug": merchant.slug, "environment": merchant.environment, "timezone": merchant.timezone, "brand_config": merchant.brand_config}


def approval_dict(item: ApprovalRequest, recommendation: Optional[AIRecommendation] = None) -> dict[str, Any]:
    rec = recommendation or item.recommendation
    return {
        "id": item.id,
        "case_id": item.case.case_id if item.case else None,
        "status": item.status,
        "recommendation_id": item.recommendation_id,
        "recommendation": {
            "intent": rec.intent,
            "confidence": rec.confidence,
            "risk_flags": rec.risk_flags,
            "recommended_action": rec.recommended_action,
            "recommended_channel": rec.recommended_channel,
            "recommended_at": rec.recommended_at.isoformat() if rec.recommended_at else None,
            "message_objective": rec.message_objective,
            "tone": rec.tone,
            "reason_codes": rec.reason_codes,
            "approval_level": rec.approval_level,
            "expected_value": rec.expected_value,
            "disturbance_cost": rec.disturbance_cost,
            "provider": rec.provider,
        } if rec else None,
        "final_action": item.final_action,
        "final_channel": item.final_channel,
        "edited_message": item.edited_message,
        "reason": item.reason,
        "created_at": item.created_at.isoformat(),
        "decided_at": item.decided_at.isoformat() if item.decided_at else None,
    }


@router.get("/cases/{case_id}/context")
def case_context(case_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    case = db.scalar(select(RecoveryCase).options(joinedload(RecoveryCase.customer), joinedload(RecoveryCase.subscription), joinedload(RecoveryCase.invoice)).where(RecoveryCase.case_id == case_id, tenant_filter(RecoveryCase, merchant_id_for_user(db, user))))
    if not case:
        raise HTTPException(404, "Recovery case not found")
    return recommendation_context(db, case)


@router.post("/cases/{case_id}/recommendations")
def create_case_recommendation(case_id: str, payload: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id, tenant_filter(RecoveryCase, merchant_id_for_user(db, user))))
    if not case:
        raise HTTPException(404, "Recovery case not found")
    try:
        recommendation, approval = create_recommendation(db, case, str(payload.get("message") or ""), payload.get("provider"))
    except Exception as exc:
        raise HTTPException(422, f"Unable to generate recommendation: {exc}") from exc
    return {"recommendation": {"id": recommendation.id, **approval_dict(approval, recommendation)["recommendation"]}, "approval": approval_dict(approval, recommendation)}


@router.get("/approvals")
def list_approvals(status: str = Query("PENDING_APPROVAL"), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    merchant_id = merchant_id_for_user(db, user)
    query = select(ApprovalRequest).options(joinedload(ApprovalRequest.case), joinedload(ApprovalRequest.recommendation)).where(tenant_filter(ApprovalRequest, merchant_id)).order_by(desc(ApprovalRequest.id))
    if status != "all":
        query = query.where(ApprovalRequest.status == status)
    return [approval_dict(item, item.recommendation) for item in db.scalars(query).all()]


def _approval_action(approval_id: int, action: str, payload: dict[str, Any], db: Session, user: User):
    approval = db.scalar(select(ApprovalRequest).options(joinedload(ApprovalRequest.case), joinedload(ApprovalRequest.recommendation)).where(ApprovalRequest.id == approval_id, tenant_filter(ApprovalRequest, merchant_id_for_user(db, user))))
    if not approval:
        raise HTTPException(404, "Approval request not found")
    try:
        if action == "approve":
            return approve_recommendation(db, approval, user, payload)
        if user.role not in {"ADMIN", "OPERATOR"}:
            raise PermissionError("Operator role required")
        if approval.status != ApprovalStatus.PENDING.value:
            raise ValueError("Approval request is no longer pending")
        approval.status = {"reject": ApprovalStatus.REJECTED.value, "defer": ApprovalStatus.DEFERRED.value}[action]
        approval.actor_user_id = user.id
        approval.reason = payload.get("reason") or f"{action.title()}d by operator"
        approval.decided_at = now_utc()
        db.commit()
        return {"approval_id": approval.id, "status": approval.status}
    except (PermissionError, ValueError) as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/approvals/{approval_id}/approve")
def approve(approval_id: int, payload: dict[str, Any] = {}, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _approval_action(approval_id, "approve", payload, db, user)


@router.post("/approvals/{approval_id}/reject")
def reject(approval_id: int, payload: dict[str, Any] = {}, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _approval_action(approval_id, "reject", payload, db, user)


@router.post("/approvals/{approval_id}/defer")
def defer(approval_id: int, payload: dict[str, Any] = {}, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return _approval_action(approval_id, "defer", payload, db, user)


@router.post("/cases/{case_id}/verify-payment")
def verify_payment(case_id: str, payload: dict[str, Any] = {}, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id, tenant_filter(RecoveryCase, merchant_id_for_user(db, user))))
    if not case:
        raise HTTPException(404, "Recovery case not found")
    reference = payload.get("payment_id") or case.external_payment_id
    verification_type = "payment"
    if not reference and case.invoice:
        reference = case.invoice.external_invoice_id
        verification_type = "invoice"
    if not reference:
        raise HTTPException(400, "No provider payment or invoice reference is available")
    result = RazorpayProvider().verify_invoice(reference) if verification_type == "invoice" else RazorpayProvider().verify_payment(reference)
    evidence = ProviderVerification(
        merchant_id=case.merchant_id,
        case_id_ref=case.id,
        verification_type=verification_type,
        external_reference=reference,
        status=result.status,
        request_id=result.request_id,
        response_status=result.response_status,
        evidence=result.evidence,
        error_message=result.error_message,
        checked_at=now_utc(),
    )
    db.add(evidence)
    db.add(CaseEvent(case=case, event_type="PROVIDER_VERIFICATION", message=f"Razorpay verification returned {result.status}", details={"request_id": result.request_id, "reference": reference}))
    if result.status == "MATCHED":
        mark_recovered(db, case, f"RAZORPAY_VERIFICATION:{result.request_id}")
        latest_outcome = db.scalar(select(RecoveryOutcome).where(RecoveryOutcome.case_id_ref == case.id).order_by(desc(RecoveryOutcome.id)))
        if latest_outcome:
            latest_outcome.payment_outcome = "RECOVERED"
            latest_outcome.recovered_amount = case.recovered_amount
            latest_outcome.executed_action = "PROVIDER_VERIFIED_CAPTURE"
    db.commit()
    return {"status": result.status, "request_id": result.request_id, "response_status": result.response_status, "evidence": result.evidence, "error": result.error_message}


@router.get("/integrations/razorpay/health")
def razorpay_health(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    merchant_id = merchant_id_for_user(db, user)
    credential = db.scalar(select(IntegrationCredential).where(IntegrationCredential.merchant_id == merchant_id, IntegrationCredential.provider == "razorpay").order_by(desc(IntegrationCredential.id)))
    result = RazorpayProvider().health()
    result.update({"connection_status": credential.status if credential else "NOT_CONNECTED", "environment": credential.environment if credential else "test", "last_checked_at": credential.last_checked_at.isoformat() if credential and credential.last_checked_at else None})
    return result


@router.post("/integrations/razorpay/connect")
def connect_razorpay(payload: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role not in {"ADMIN", "OPERATOR"}:
        raise HTTPException(403, "Admin or operator role required")
    merchant_id = merchant_id_for_user(db, user)
    provider = str(payload.get("provider", "razorpay")).lower()
    if provider != "razorpay":
        raise HTTPException(400, "Only Razorpay is supported in this release")
    api_key = str(payload.get("key_id") or "").strip()
    api_secret = str(payload.get("key_secret") or "").strip()
    webhook_secret = str(payload.get("webhook_secret") or "").strip()
    if not api_key or not api_secret:
        raise HTTPException(400, "key_id and key_secret are required")
    credential = IntegrationCredential(
        merchant_id=merchant_id,
        provider="razorpay",
        environment=str(payload.get("environment", "test")).lower(),
        key_id=api_key,
        secret_ref=sha256(api_secret.encode()).hexdigest(),
        webhook_secret_ref=sha256(webhook_secret.encode()).hexdigest() if webhook_secret else None,
        status="CONFIGURED",
        extra_data={"connected_by": user.email},
    )
    db.add(credential)
    db.commit()
    return {"ok": True, "provider": credential.provider, "environment": credential.environment, "status": credential.status, "credential_id": credential.id}


@router.get("/customers/{customer_id}/timeline")
def customer_timeline(customer_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    customer = db.get(Customer, customer_id)
    if not customer or (customer.merchant_id and customer.merchant_id != merchant_id_for_user(db, user)):
        raise HTTPException(404, "Customer not found")
    merchant_id = merchant_id_for_user(db, user)
    cases = db.scalars(select(RecoveryCase).where(RecoveryCase.customer_id_ref == customer_id, tenant_filter(RecoveryCase, merchant_id)).order_by(desc(RecoveryCase.updated_at))).all()
    events = []
    for case in cases:
        events.extend({"type": event.event_type, "message": event.message, "created_at": event.created_at.isoformat(), "case_id": case.case_id} for event in case.events)
    return {"customer": {"id": customer.id, "external_customer_id": customer.external_customer_id, "name": customer.name, "email": customer.email, "language": customer.language_preference, "opted_out": customer.communication_opt_out}, "cases": [case.case_id for case in cases], "events": sorted(events, key=lambda item: item["created_at"], reverse=True)}


@router.get("/integrations/{provider}/events")
def provider_events(provider: str, limit: int = Query(100, ge=1, le=500), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if provider.lower() != "razorpay":
        raise HTTPException(404, "Provider is not connected")
    events = db.scalars(select(PaymentEvent).where(PaymentEvent.merchant_id == merchant_id_for_user(db, user)).order_by(desc(PaymentEvent.id)).limit(limit)).all()
    return [{"external_event_id": item.external_event_id, "event_type": item.event_type, "case_reference": item.case_reference, "processing_status": item.processing_status, "signature_valid": item.signature_valid, "created_at": item.created_at.isoformat()} for item in events]


@router.get("/analytics/recovery-lift")
def recovery_lift(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    merchant_id = merchant_id_for_user(db, user)
    outcomes = db.scalars(select(RecoveryOutcome).where(tenant_filter(RecoveryOutcome, merchant_id))).all()
    treatment = [item for item in outcomes if not item.holdout]
    holdout = [item for item in outcomes if item.holdout]
    def rate(items):
        return round(sum(1 for item in items if item.payment_outcome == "RECOVERED") / len(items) * 100, 2) if items else 0.0
    treatment_rate = rate(treatment)
    holdout_rate = rate(holdout)
    return {"treatment": {"cases": len(treatment), "recovery_rate": treatment_rate, "recovered_amount": sum(item.recovered_amount for item in treatment)}, "holdout": {"cases": len(holdout), "recovery_rate": holdout_rate, "recovered_amount": sum(item.recovered_amount for item in holdout)}, "incremental_lift_points": round(treatment_rate - holdout_rate, 2)}


@router.get("/analytics/channel-performance")
def channel_performance(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    merchant_id = merchant_id_for_user(db, user)
    outcomes = db.scalars(select(RecoveryOutcome).where(tenant_filter(RecoveryOutcome, merchant_id))).all()
    grouped: dict[str, dict[str, Any]] = {}
    for item in outcomes:
        channel = item.executed_action or "UNKNOWN"
        row = grouped.setdefault(channel, {"channel": channel, "cases": 0, "recovered": 0, "cost": 0})
        row["cases"] += 1
        row["recovered"] += item.recovered_amount
        row["cost"] += item.intervention_cost
    for row in grouped.values():
        row["net_recovered"] = row["recovered"] - row["cost"]
    return list(grouped.values())


@router.post("/experiments/{experiment_key}/assign")
def assign_experiment(experiment_key: str, payload: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    merchant_id = merchant_id_for_user(db, user)
    customer_id = payload.get("customer_id")
    case_id = payload.get("case_id")
    case_pk = None
    if not customer_id and not case_id:
        raise HTTPException(400, "customer_id or case_id is required")
    if case_id:
        case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id, tenant_filter(RecoveryCase, merchant_id)))
        if not case:
            raise HTTPException(404, "Recovery case not found")
        customer_id = customer_id or case.customer_id_ref
        case_pk = case.id
    existing = db.scalar(select(ExperimentAssignment).where(
        ExperimentAssignment.merchant_id == merchant_id,
        ExperimentAssignment.experiment_key == experiment_key,
        ExperimentAssignment.customer_id_ref == customer_id,
    ))
    if existing:
        return {"experiment_key": existing.experiment_key, "variant": existing.variant, "holdout": existing.holdout, "assignment_id": existing.id}
    variants = payload.get("variants") or ["control", "message_v2"]
    if not isinstance(variants, list) or not variants or not all(isinstance(item, str) for item in variants):
        raise HTTPException(400, "variants must be a non-empty list of strings")
    digest = int(sha256(f"{merchant_id}:{customer_id}:{experiment_key}".encode()).hexdigest()[:8], 16)
    holdout_percent = max(0, min(100, int(payload.get("holdout_percent", 10))))
    holdout = digest % 100 < holdout_percent
    variant = "holdout" if holdout else variants[digest % len(variants)]
    assignment = ExperimentAssignment(merchant_id=merchant_id, customer_id_ref=customer_id, case_id_ref=case_pk, experiment_key=experiment_key, variant=variant, holdout=holdout)
    db.add(assignment)
    db.commit()
    return {"experiment_key": experiment_key, "variant": variant, "holdout": holdout, "assignment_id": assignment.id}
