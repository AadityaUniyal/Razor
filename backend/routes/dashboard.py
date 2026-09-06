from collections import defaultdict
import time
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, desc, case as sql_case
from sqlalchemy.orm import Session, joinedload

from backend.core.config import GROQ_API_KEY, GEMINI_API_KEY, IS_VERCEL
from backend.core.security import get_current_user
from backend.services.policy_engine import latest_policy
from backend.services.websocket_manager import app_websockets
from database.connection import get_db, check_db_health
from database.models import (
    RecoveryCase, PaymentPromise, DecisionLedger, CaseEvent,
    RecoveryPolicy, SystemHealthEvent, Notification, Customer, User, Role, CaseState
)

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/summary")
@router.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    totals = db.execute(select(
        func.count(RecoveryCase.id),
        func.coalesce(func.sum(RecoveryCase.recovered_amount), 0),
        func.coalesce(func.sum(sql_case((RecoveryCase.recovered.is_(False), RecoveryCase.amount), else_=0)), 0),
        func.coalesce(func.sum(RecoveryCase.amount), 0),
        func.coalesce(func.sum(RecoveryCase.total_intervention_cost), 0),
        func.coalesce(func.sum(sql_case((RecoveryCase.recovered.is_(True), 1), else_=0)), 0),
        func.coalesce(func.sum(sql_case((RecoveryCase.state == CaseState.STOP.value, 1), else_=0)), 0),
        func.coalesce(func.sum(sql_case((RecoveryCase.state == CaseState.ESCALATED.value, 1), else_=0)), 0),
        func.coalesce(func.sum(sql_case((
            (RecoveryCase.recovered.is_(True)) &
            (RecoveryCase.failure_category == "UNCERTAIN_OR_TEMPORARY") &
            (RecoveryCase.communication_count == 0),
            RecoveryCase.amount,
        ), else_=0)), 0),
    )).one()
    total_cases, total_recovered, total_at_risk, total_seen, total_intervention_costs, recovered_count, safe_stop_count, escalation_count, prevented = totals
    net_recovery_value = total_recovered - total_intervention_costs
    recovery_time_rows = db.execute(select(RecoveryCase.closed_at, RecoveryCase.created_at).where(
        RecoveryCase.recovered.is_(True), RecoveryCase.closed_at.is_not(None)
    )).all()
    recovery_times = [(closed_at - created_at).total_seconds() for closed_at, created_at in recovery_time_rows]
    avg_time = round(sum(recovery_times) / len(recovery_times), 1) if recovery_times else 0

    return {
        "total_cases": total_cases,
        "total_at_risk": total_at_risk,
        "total_revenue_seen": total_seen,
        "total_recovered": total_recovered,
        "net_recovery_value": net_recovery_value,
        "total_intervention_costs": total_intervention_costs,
        "recovery_rate": round((total_recovered / total_seen * 100) if total_seen else 0, 1),
        "unnecessary_interventions_prevented": prevented,
        "safe_stop_count": int(safe_stop_count or 0),
        "escalation_count": int(escalation_count or 0),
        "average_recovery_time_seconds": float(avg_time or 0),
        "active_promises": db.scalar(select(func.count(PaymentPromise.id)).where(PaymentPromise.status == "PENDING")) or 0,
        "ai_fallback_events": db.scalar(select(func.count(SystemHealthEvent.id)).where(SystemHealthEvent.service_name == "groq", SystemHealthEvent.status.like("FALLBACK%"))) or 0,
    }


@router.get("/dashboard/activity")
def dashboard_activity(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    events = db.scalars(select(CaseEvent).options(joinedload(CaseEvent.case)).order_by(desc(CaseEvent.id)).limit(30)).all()
    return [{
        "case_id": e.case.case_id,
        "event_type": e.event_type,
        "message": e.message,
        "created_at": e.created_at.isoformat()
    } for e in events]


@router.get("/dashboard/metrics")
def dashboard_metrics(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    categories: dict[str, dict[str, int]] = defaultdict(lambda: {"at_risk": 0, "recovered": 0, "count": 0})
    category_rows = db.execute(select(
        RecoveryCase.failure_category,
        func.count(RecoveryCase.id),
        func.coalesce(func.sum(RecoveryCase.recovered_amount), 0),
        func.coalesce(func.sum(sql_case((RecoveryCase.recovered.is_(False), RecoveryCase.amount), else_=0)), 0),
    ).group_by(RecoveryCase.failure_category)).all()
    for category, count, recovered, at_risk in category_rows:
        categories[category] = {"count": int(count or 0), "recovered": int(recovered or 0), "at_risk": int(at_risk or 0)}

    counts = db.execute(select(
        func.coalesce(func.sum(sql_case((RecoveryCase.recovered.is_(True), 1), else_=0)), 0),
        func.coalesce(func.sum(sql_case((RecoveryCase.state == CaseState.STOP.value, 1), else_=0)), 0),
        func.coalesce(func.sum(sql_case((RecoveryCase.state == CaseState.ESCALATED.value, 1), else_=0)), 0),
        func.coalesce(func.sum(sql_case((RecoveryCase.state == CaseState.VERIFY.value, 1), else_=0)), 0),
        func.coalesce(func.sum(sql_case((RecoveryCase.state == CaseState.WAIT.value, 1), else_=0)), 0),
    )).one()

    return {
        "by_category": categories,
        "recovered_cases": int(counts[0] or 0),
        "stopped_cases": int(counts[1] or 0),
        "escalated_cases": int(counts[2] or 0),
        "verifying_cases": int(counts[3] or 0),
        "waiting_cases": int(counts[4] or 0),
    }


@router.get("/dashboard/trends")
def dashboard_trends(days: int = 14, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Return a compact daily recovery series for the merchant dashboard."""
    days = max(7, min(days, 31))
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    window_start = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    cases = db.execute(select(
        RecoveryCase.recovered, RecoveryCase.recovered_amount, RecoveryCase.amount,
        RecoveryCase.closed_at, RecoveryCase.created_at,
    ).where(RecoveryCase.created_at >= window_start)).all()
    series = []
    for offset in range(days - 1, -1, -1):
        day = end - timedelta(days=offset)
        recovered = sum(
            recovered_amount for recovered_flag, recovered_amount, amount, closed_at, created_at in cases
            if recovered_flag and closed_at and closed_at.date() == day
        )
        at_risk = sum(
            amount for recovered, recovered_amount, amount, closed_at, created_at in cases
            if created_at and created_at.date() == day and not recovered
        )
        series.append({
            "date": day.isoformat(),
            "label": day.strftime("%d %b"),
            "recovered": recovered,
            "at_risk": at_risk,
        })
    return {"days": days, "series": series}


@router.get("/dashboard/notifications")
def dashboard_notifications(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    notifications = db.scalars(
        select(Notification)
        .options(joinedload(Notification.case))
        .order_by(desc(Notification.id))
        .limit(30)
    ).all()
    return [{
        "id": n.id,
        "channel": n.channel,
        "recipient": n.recipient,
        "status": n.status,
        "message": n.message_content,
        "case_id": n.case.case_id if n.case else None,
        "created_at": n.sent_at.isoformat(),
    } for n in notifications]


@router.get("/customers")
def list_customers(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    customers = db.scalars(
        select(Customer)
        .options(joinedload(Customer.cases))
        .order_by(desc(Customer.id))
        .limit(250)
    ).unique().all()
    cases = db.scalars(select(RecoveryCase).order_by(desc(RecoveryCase.id))).all()
    profiles: dict[str, dict[str, Any]] = {}

    for customer in customers:
        key = customer.email.strip().lower()
        customer_cases = sorted(customer.cases, key=lambda case: case.id, reverse=True)
        profiles[key] = {
            "id": customer.id,
            "external_customer_id": customer.external_customer_id,
            "name": customer.name,
            "email": customer.email,
            "phone": customer.phone,
            "language": customer.language_preference,
            "opt_out": customer.communication_opt_out,
            "case_ids": [case.case_id for case in customer_cases],
            "case_count": len(customer_cases),
            "at_risk": sum(case.amount for case in customer_cases if not case.recovered),
            "recovered": sum(case.recovered_amount for case in customer_cases),
            "last_seen": max((case.updated_at for case in customer_cases), default=customer.created_at),
        }

    # Older or imported cases can have customer identity without a normalized
    # Customer row. Keep those relationships visible in the directory too.
    for case in cases:
        key = case.customer_email.strip().lower()
        if key in profiles and case.customer_id_ref:
            continue
        profile = profiles.setdefault(key, {
            "id": None,
            "external_customer_id": None,
            "name": case.customer_name,
            "email": case.customer_email,
            "phone": None,
            "language": "en",
            "opt_out": False,
            "case_ids": [],
            "case_count": 0,
            "at_risk": 0,
            "recovered": 0,
            "last_seen": case.updated_at,
        })
        if case.case_id not in profile["case_ids"]:
            profile["case_ids"].append(case.case_id)
            profile["case_count"] += 1
            profile["at_risk"] += 0 if case.recovered else case.amount
            profile["recovered"] += case.recovered_amount
        profile["last_seen"] = max(profile["last_seen"], case.updated_at)

    for profile in profiles.values():
        profile["last_seen"] = profile["last_seen"].isoformat()
    return list(profiles.values())


@router.get("/promises")
def list_promises(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    promises = db.scalars(select(PaymentPromise).options(joinedload(PaymentPromise.case)).order_by(PaymentPromise.promised_at)).all()
    out = []
    for p in promises:
        case = p.case
        if case:
            out.append({
                "case_id": case.case_id,
                "customer": case.customer_name,
                "amount": case.amount,
                "promise_text": p.promise_text,
                "promised_at": p.promised_at.isoformat(),
                "confidence": p.confidence,
                "status": p.status,
                "next_check": p.promised_at.isoformat(),
                "follow_up_sent": p.follow_up_sent,
            })
    return out


@router.get("/audit")
def list_audit(case_id: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = select(DecisionLedger).order_by(desc(DecisionLedger.id))
    if case_id:
        c = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id))
        if not c:
            raise HTTPException(404, "Case not found")
        query = query.where(DecisionLedger.case_id_ref == c.id)

    rows = db.scalars(query.options(joinedload(DecisionLedger.case)).limit(100)).all()
    return [{
        "case_id": d.case.case_id,
        "selected_action": d.selected_action,
        "policy_result": d.policy_result,
        "policy_reason": d.policy_reason,
        "strategy_scores": d.strategy_scores,
        "deterministic_factors": d.deterministic_factors,
        "ai_analysis": d.ai_analysis,
        "llm_provider": d.llm_provider,
        "created_at": d.created_at.isoformat(),
    } for d in rows]


@router.get("/audit/{case_id}")
def audit_case(case_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return list_audit(case_id, db, user)


@router.get("/health")
def system_health(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    db_health = check_db_health()

    def count_health(service: str, pattern: str = "%") -> int:
        return db.scalar(select(func.count(SystemHealthEvent.id)).where(SystemHealthEvent.service_name == service, SystemHealthEvent.status.like(pattern))) or 0

    return {
        "webhook_receiver": "HEALTHY",
        "database": db_health.get("status", "DISCONNECTED"),
        "database_latency": f"{db_health.get('latency_ms', 0)} ms",
        "database_pool": f"size={db_health.get('pool', {}).get('size', 0)}, active={db_health.get('pool', {}).get('checkedout', 0)}, idle={db_health.get('pool', {}).get('checkedin', 0)}",
        "database_health": db_health,
        "groq_ai": "AVAILABLE" if GROQ_API_KEY else "FALLBACK MODE",
        "gemini_ai": "AVAILABLE" if GEMINI_API_KEY else "NOT CONFIGURED",
        "websocket": "CONNECTED" if app_websockets else "STANDBY",
        "background_scheduler": "DISABLED_SERVERLESS" if IS_VERCEL else "RUNNING",
        "duplicate_events_detected": count_health("webhook", "DUPLICATE%"),
        "ai_fallback_events": count_health("groq", "FALLBACK%"),
        "out_of_order_events": count_health("reconciliation", "OUT_OF_ORDER%"),
        "action_execution_failures": count_health("action_executor", "FAILED%"),
        "recent_events": [{
            "service_name": e.service_name,
            "status": e.status,
            "details": e.details,
            "created_at": e.created_at.isoformat()
        } for e in db.scalars(select(SystemHealthEvent).order_by(desc(SystemHealthEvent.id)).limit(20)).all()],
    }


@router.get("/policies")
def list_policies(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    policies = db.scalars(select(RecoveryPolicy).order_by(desc(RecoveryPolicy.id))).all()
    return [{
        "id": p.id,
        "version": p.version,
        "name": p.name,
        "configuration": p.configuration,
        "active": p.active,
        "created_at": p.created_at.isoformat(),
    } for p in policies]


@router.post("/policies")
def update_policy(payload: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role != Role.ADMIN.value:
        raise HTTPException(403, "Admin privileges required")
    current = latest_policy(db)
    current.active = False
    new_version = f"v{int(time.time())}"
    new_policy = RecoveryPolicy(
        version=new_version,
        name=f"Custom Recovery Policy {new_version}",
        configuration=payload.get("configuration", current.configuration),
        active=True
    )
    db.add(new_policy)
    db.commit()
    return {"ok": True, "version": new_policy.version, "configuration": new_policy.configuration}
