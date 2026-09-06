"""Side-effect-free AI recommendation generation and approval execution."""

from typing import Any, Optional

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend.services.ai_agent import analyze_intent_resilient, resolve_temporal_expression
from backend.services.policy_engine import _execute_coro_sync, latest_policy, record_action, to_case_dict
from backend.services.strategy_scorer import calculate_strategy_scores
from database.models import (
    AIRecommendation, ApprovalRequest, ApprovalStatus, CaseEvent, CaseState,
    Notification, RecoveryCase, RecommendationStatus, Role, User, now_utc,
)


def recommendation_context(db: Session, case: RecoveryCase) -> dict[str, Any]:
    return {
        "case": to_case_dict(case),
        "customer": {
            "name": case.customer.name if case.customer else case.customer_name,
            "email": case.customer.email if case.customer else case.customer_email,
            "language": case.customer.language_preference if case.customer else "en",
            "opted_out": bool(case.customer and case.customer.communication_opt_out),
        },
        "subscription": {
            "id": case.subscription.external_subscription_id if case.subscription else case.external_subscription_id,
            "plan": case.subscription.plan_name if case.subscription else None,
            "billing_period": case.subscription.billing_period if case.subscription else None,
            "payment_method": case.subscription.payment_method if case.subscription else None,
            "churn_risk": case.subscription.churn_risk if case.subscription else 0.0,
            "lifetime_value": case.subscription.customer_lifetime_value if case.subscription else 0,
        },
        "invoice": {
            "id": case.invoice.external_invoice_id if case.invoice else None,
            "amount_due": case.invoice.amount_due if case.invoice else case.amount,
            "amount_paid": case.invoice.amount_paid if case.invoice else 0,
            "status": case.invoice.status if case.invoice else "OPEN",
        },
        "history": {
            "communications": case.communication_count,
            "retries": case.retry_count,
            "prior_decisions": len(case.decisions),
            "prior_actions": len(case.actions),
        },
    }


def create_recommendation(
    db: Session,
    case: RecoveryCase,
    message: str = "",
    force_provider: Optional[str] = None,
) -> tuple[AIRecommendation, ApprovalRequest]:
    policy_row = latest_policy(db, case.merchant_id)
    policy = policy_row.configuration or {}
    scores, scored_action = calculate_strategy_scores(case, policy)
    intent = "UNKNOWN"
    confidence = 0.0
    provider = "fallback-rules"
    tone = "neutral"
    temporal = None
    if message:
        intent_obj, provider, latency = _execute_coro_sync(analyze_intent_resilient(message, force_provider=force_provider))
        intent = intent_obj.intent
        confidence = intent_obj.confidence
        tone = intent_obj.customer_tone or "neutral"
        temporal = intent_obj.temporal_expression

    risks: list[str] = []
    reasons: list[str] = []
    if case.customer and case.customer.communication_opt_out:
        risks.append("CUSTOMER_OPTED_OUT")
        reasons.append("customer_opted_out")
    if intent in {"DISPUTE", "PAYMENT_COMPLETED_CLAIM"}:
        risks.append("PAYMENT_STATE_REQUIRES_VERIFICATION")
        reasons.append("provider_verification_required")
    if intent == "OPT_OUT":
        risks.append("OPT_OUT_REQUEST")
        reasons.append("suppress_communications")
    if confidence < float(policy.get("ai_confidence_threshold", 0.70)):
        risks.append("LOW_AI_CONFIDENCE")
        reasons.append("confidence_below_policy_threshold")
    if case.amount >= int(policy.get("minimum_amount_for_human_escalation", 10000)):
        risks.append("HIGH_VALUE_CASE")
        reasons.append("high_value_requires_operator")

    if intent == "OPT_OUT":
        action = "STOP"
    elif intent == "PAYMENT_COMPLETED_CLAIM":
        action = "VERIFY"
    elif intent == "DISPUTE":
        action = "ESCALATE"
    elif intent == "PROMISE_TO_PAY":
        action = "WAIT"
        temporal = temporal or message
    elif intent == "NEED_HELP":
        action = "RECOVER"
    else:
        action = scored_action

    if risks and any(r in risks for r in {"LOW_AI_CONFIDENCE", "HIGH_VALUE_CASE", "PAYMENT_STATE_REQUIRES_VERIFICATION", "OPT_OUT_REQUEST"}):
        approval_level = "OPERATOR_REVIEW"
    else:
        approval_level = "OPERATOR"

    recommended_at = resolve_temporal_expression(temporal) if temporal and action == "WAIT" else None
    score_data = scores.get(action, {})
    recommendation = AIRecommendation(
        merchant_id=case.merchant_id,
        case_id_ref=case.id,
        intent=intent,
        confidence=confidence,
        risk_flags=risks,
        recommended_action=action,
        recommended_channel="RECOVERY_LINK" if action == "RECOVER" else None,
        recommended_at=recommended_at,
        message_objective="Resolve payment safely without repeat contact" if action != "STOP" else "Honor customer suppression request",
        tone=tone,
        reason_codes=reasons or ["strategy_score_maximizes_net_recovery"],
        approval_level=approval_level,
        expected_value=int(case.amount * float(score_data.get("expected_probability", 0.0)) - score_data.get("operational_cost", 0) - score_data.get("disturbance_penalty", 0)),
        disturbance_cost=int(score_data.get("disturbance", 0)),
        policy_version=policy_row.version,
        provider=provider,
        model_metadata={"latency_ms": locals().get("latency", 0), "scored_action": scored_action},
        input_context={"message": message, **recommendation_context(db, case)},
    )
    db.add(recommendation)
    db.flush()
    approval = ApprovalRequest(
        merchant_id=case.merchant_id,
        case_id_ref=case.id,
        recommendation_id=recommendation.id,
        status=ApprovalStatus.PENDING.value,
        policy_version=policy_row.version,
    )
    db.add(approval)
    db.commit()
    db.refresh(recommendation)
    db.refresh(approval)
    return recommendation, approval


def approve_recommendation(db: Session, approval: ApprovalRequest, actor: User, payload: dict[str, Any]) -> dict[str, Any]:
    if actor.role not in {Role.ADMIN.value, Role.OPERATOR.value}:
        raise PermissionError("Operator role required")
    if approval.status != ApprovalStatus.PENDING.value:
        raise ValueError("Approval request is no longer pending")
    case = db.get(RecoveryCase, approval.case_id_ref)
    recommendation = db.get(AIRecommendation, approval.recommendation_id)
    if not case or not recommendation:
        raise ValueError("Approval context is missing")

    action = str(payload.get("action") or recommendation.recommended_action).upper()
    allowed = {"WAIT", "VERIFY", "RECOVER", "ESCALATE", "STOP"}
    if action not in allowed:
        raise ValueError("Unsupported approved action")
    if case.recovered and action != "STOP":
        raise ValueError("Recovered cases cannot receive recovery actions")
    if case.customer and case.customer.communication_opt_out and action == "RECOVER":
        raise ValueError("Customer suppression rule blocks communication")

    approval.status = ApprovalStatus.APPROVED.value
    approval.final_action = action
    approval.final_channel = payload.get("channel") or recommendation.recommended_channel
    approval.edited_message = payload.get("message")
    approval.actor_user_id = actor.id
    approval.reason = payload.get("reason") or "Approved by operator"
    approval.decided_at = now_utc()
    recommendation.status = RecommendationStatus.CONVERTED.value
    case.current_action = action
    case.state = {
        "WAIT": CaseState.WAIT.value,
        "VERIFY": CaseState.VERIFY.value,
        "RECOVER": CaseState.RECOVER.value,
        "ESCALATE": CaseState.ESCALATED.value,
        "STOP": CaseState.STOP.value,
    }[action]
    if action == "RECOVER":
        case.communication_count += 1
        db.add(Notification(
            case=case,
            channel=approval.final_channel or "RECOVERY_LINK",
            recipient=case.customer_email,
            message_content=approval.edited_message or "Payment recovery action approved; dispatch is pending connector confirmation.",
            status="PENDING_DISPATCH",
            cost=2,
        ))
    record_action(db, case, f"APPROVED_{action}", "PENDING_EXECUTION", {"actor": actor.email, "approval_id": approval.id}, channel=approval.final_channel, cost=0)
    from database.models import RecoveryOutcome
    db.add(RecoveryOutcome(
        merchant_id=case.merchant_id,
        case_id_ref=case.id,
        recommendation_id=recommendation.id,
        approved_action=action,
        payment_outcome="PENDING",
        executed_action=None,
        extra_data={"approval_id": approval.id},
    ))
    db.add(CaseEvent(case=case, event_type="APPROVAL_GRANTED", message=f"{actor.email} approved {action}", details={"approval_id": approval.id}))
    db.commit()
    return {"approval_id": approval.id, "status": approval.status, "case": to_case_dict(case)}
