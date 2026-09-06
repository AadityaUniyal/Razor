from secrets import token_hex
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from backend.core.security import get_current_user
from backend.core.tenancy import merchant_id_for_user, tenant_filter
from backend.services.policy_engine import (
    to_case_dict, record_action, mark_recovered, process_case
)
from backend.services.websocket_manager import broadcast
from database.connection import get_db
from database.models import RecoveryCase, CaseEvent, SystemHealthEvent, User, Role, now_utc

router = APIRouter(prefix="/api", tags=["cases"])


@router.get("/cases")
def list_cases(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    merchant_id = merchant_id_for_user(db, user)
    cases = db.scalars(select(RecoveryCase).where(tenant_filter(RecoveryCase, merchant_id)).order_by(desc(RecoveryCase.id)).offset(offset).limit(limit)).all()
    return [to_case_dict(c) for c in cases]


@router.get("/cases/{case_id}")
def case_detail(case_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id, tenant_filter(RecoveryCase, merchant_id_for_user(db, user))))
    if not case:
        raise HTTPException(404, "Recovery case not found")

    return {
        "case": to_case_dict(case),
        "customer": {
            "name": case.customer.name if case.customer else case.customer_name,
            "email": case.customer.email if case.customer else case.customer_email,
            "opt_out": case.customer.communication_opt_out if case.customer else False,
        },
        "events": [{
            "event_type": e.event_type,
            "message": e.message,
            "details": e.details,
            "created_at": e.created_at.isoformat()
        } for e in case.events],
        "decisions": [{
            "selected_action": d.selected_action,
            "strategy_scores": d.strategy_scores,
            "policy_result": d.policy_result,
            "policy_reason": d.policy_reason,
            "deterministic_factors": d.deterministic_factors,
            "ai_analysis": d.ai_analysis,
            "created_at": d.created_at.isoformat()
        } for d in case.decisions],
        "actions": [{
            "action_type": a.action_type,
            "channel": a.channel,
            "cost": a.cost,
            "status": a.status,
            "result": a.result,
            "created_at": a.created_at.isoformat()
        } for a in case.actions],
        "notifications": [{
            "channel": n.channel,
            "recipient": n.recipient,
            "message_content": n.message_content,
            "cost": n.cost,
            "sent_at": n.sent_at.isoformat()
        } for n in case.notifications],
    }


@router.get("/cases/{case_id}/timeline")
def case_timeline(case_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id, tenant_filter(RecoveryCase, merchant_id_for_user(db, user))))
    if not case:
        raise HTTPException(404, "Case not found")
    return [{
        "event_type": e.event_type,
        "message": e.message,
        "details": e.details,
        "created_at": e.created_at.isoformat()
    } for e in case.events]


@router.post("/cases/{case_id}/action")
def manual_action(case_id: str, payload: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role not in {Role.ADMIN.value, Role.OPERATOR.value}:
        raise HTTPException(403, "Operator role required")
    case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id, tenant_filter(RecoveryCase, merchant_id_for_user(db, user))))
    if not case:
        raise HTTPException(404, "Case not found")

    action = payload.get("action", "VERIFY").upper()
    if action not in {"VERIFY", "RECOVER", "ESCALATE", "STOP", "CAPTURE_PAYMENT"}:
        raise HTTPException(400, f"Unsupported action {action}")

    if case.recovered and action != "STOP":
        raise HTTPException(409, "Recovered cases cannot receive further recovery actions")
    if case.state == "STOP" and action != "STOP":
        raise HTTPException(409, "Stopped cases cannot receive further recovery actions")

    if action == "CAPTURE_PAYMENT":
        mark_recovered(db, case, "MANUAL_OPERATOR_CAPTURE")
        db.add(CaseEvent(case=case, event_type="PAYMENT_CAPTURED", message=f"{user.email} confirmed manual payment capture.", details=payload))
        db.commit()
        broadcast({"type": "CASE_UPDATED", "case_id": case.case_id, "new_state": case.state, "timestamp": now_utc().isoformat()})
        return {"ok": True, "case": to_case_dict(case)}

    if payload.get("simulate_failure"):
        record_action(db, case, f"MANUAL_{action}", "FAILED", {"actor": user.email, "reason": "Simulated executor failure"}, key=f"{case.case_id}:FAIL:{token_hex(4)}")
        db.add(SystemHealthEvent(service_name="action_executor", status="FAILED_SIMULATION", details={"case_id": case.case_id, "action": action}))
        db.commit()
        return {"ok": False, "simulated_failure": True, "case": to_case_dict(case)}

    case.current_action = action
    case.state = {"VERIFY": "VERIFY", "RECOVER": "RECOVER", "ESCALATE": "ESCALATED", "STOP": "STOP"}[action]
    case.updated_at = now_utc()

    record_action(db, case, f"MANUAL_{action}", "APPROVED", {"actor": user.email, "note": payload.get("note", "")})
    db.add(CaseEvent(case=case, event_type="OPERATOR_ACTION", message=f"{user.email} selected action {action}", details=payload))
    db.commit()
    broadcast({"type": "CASE_UPDATED", "case_id": case.case_id, "new_state": case.state, "timestamp": now_utc().isoformat()})
    return {"ok": True, "case": to_case_dict(case)}
