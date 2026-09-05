from collections import defaultdict
import time
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, desc
from sqlalchemy.orm import Session

from backend.core.config import GROQ_API_KEY, GEMINI_API_KEY
from backend.core.security import get_current_user
from backend.services.policy_engine import latest_policy
from backend.services.websocket_manager import app_websockets
from database.connection import get_db, check_db_health
from database.models import (
    RecoveryCase, PaymentPromise, DecisionLedger, CaseEvent,
    RecoveryPolicy, SystemHealthEvent, User, Role, CaseState
)

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/summary")
@router.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    cases = db.scalars(select(RecoveryCase)).all()
    total_cases = len(cases)
    recovered_cases = [c for c in cases if c.recovered]
    total_recovered = sum(c.recovered_amount for c in cases)
    total_at_risk = sum(c.amount for c in cases if not c.recovered)
    total_seen = sum(c.amount for c in cases)
    total_intervention_costs = sum(c.total_intervention_cost for c in cases)
    net_recovery_value = total_recovered - total_intervention_costs

    prevented = sum(
        c.amount for c in cases
        if c.recovered and c.failure_category == "UNCERTAIN_OR_TEMPORARY" and c.communication_count == 0
    )

    recovery_times = [(c.closed_at - c.created_at).total_seconds() for c in recovered_cases if c.closed_at]
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
        "safe_stop_count": sum(1 for c in cases if c.state == CaseState.STOP.value),
        "escalation_count": sum(1 for c in cases if c.state == CaseState.ESCALATED.value),
        "average_recovery_time_seconds": avg_time,
        "active_promises": db.scalar(select(func.count(PaymentPromise.id)).where(PaymentPromise.status == "PENDING")) or 0,
        "ai_fallback_events": db.scalar(select(func.count(SystemHealthEvent.id)).where(SystemHealthEvent.service_name == "groq", SystemHealthEvent.status.like("FALLBACK%"))) or 0,
    }


@router.get("/dashboard/activity")
def dashboard_activity(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    events = db.scalars(select(CaseEvent).order_by(desc(CaseEvent.id)).limit(30)).all()
    return [{
        "case_id": e.case.case_id,
        "event_type": e.event_type,
        "message": e.message,
        "created_at": e.created_at.isoformat()
    } for e in events]


@router.get("/dashboard/metrics")
def dashboard_metrics(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    cases = db.scalars(select(RecoveryCase)).all()
    categories: dict[str, dict[str, int]] = defaultdict(lambda: {"at_risk": 0, "recovered": 0, "count": 0})
    for c in cases:
        row = categories[c.failure_category]
        row["count"] += 1
        row["recovered"] += c.recovered_amount
        row["at_risk"] += 0 if c.recovered else c.amount

    return {
        "by_category": categories,
        "recovered_cases": sum(1 for c in cases if c.recovered),
        "stopped_cases": sum(1 for c in cases if c.state == CaseState.STOP.value),
        "escalated_cases": sum(1 for c in cases if c.state == CaseState.ESCALATED.value),
        "verifying_cases": sum(1 for c in cases if c.state == CaseState.VERIFY.value),
        "waiting_cases": sum(1 for c in cases if c.state == CaseState.WAIT.value),
    }


@router.get("/promises")
def list_promises(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    promises = db.scalars(select(PaymentPromise).order_by(PaymentPromise.promised_at)).all()
    out = []
    for p in promises:
        case = db.get(RecoveryCase, p.case_id_ref)
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

    rows = db.scalars(query.limit(100)).all()
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
        "background_scheduler": "RUNNING",
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
