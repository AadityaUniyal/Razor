import asyncio
import concurrent.futures
from secrets import token_hex
from typing import Any, Optional
from fastapi import HTTPException
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from backend.services.ai_agent import (
    analyze_intent_resilient, fallback_ai, groq_intent,
    resolve_temporal_expression, CustomerIntent
)
from backend.services.strategy_scorer import calculate_strategy_scores
from backend.services.websocket_manager import broadcast
from database.models import (
    RecoveryCase, RecoveryPolicy, ActionRecord, DecisionLedger,
    PaymentPromise, ScheduledTask, Notification, CaseEvent,
    CaseState, AIEvaluationRecord, now_utc
)


def _execute_coro_sync(coro, timeout: float = 15.0):
    """
    Executes an async coroutine safely from either a synchronous context
    or from within an active asyncio event loop thread by offloading to
    an isolated thread executor.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Running the coroutine on the current loop and blocking it would deadlock.
        # Isolate the synchronous bridge in a worker thread instead.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(asyncio.run, coro)
            return future.result(timeout=timeout)
    else:
        return asyncio.run(coro)



def latest_policy(db: Session) -> RecoveryPolicy:
    policy = db.scalar(select(RecoveryPolicy).where(RecoveryPolicy.active.is_(True)).order_by(desc(RecoveryPolicy.id)))
    if not policy:
        raise HTTPException(500, "No active policy found")
    return policy


def to_case_dict(case: RecoveryCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "customer_name": case.customer_name,
        "customer_email": case.customer_email,
        "amount": case.amount,
        "currency": case.currency,
        "state": case.state,
        "failure_category": case.failure_category,
        "current_action": case.current_action,
        "policy_version": case.policy_version,
        "strategy_score": case.strategy_score,
        "total_intervention_cost": case.total_intervention_cost,
        "net_recovery_value": case.net_recovery_value,
        "promise_time": case.promise_time.isoformat() if case.promise_time else None,
        "communication_count": case.communication_count,
        "retry_count": case.retry_count,
        "ai_status": case.ai_status,
        "recovered_amount": case.recovered_amount,
        "recovered": case.recovered,
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
    }


def record_action(
    db: Session,
    case: RecoveryCase,
    action_type: str,
    status: str,
    result: dict[str, Any],
    channel: Optional[str] = None,
    cost: int = 0,
    key: Optional[str] = None
) -> bool:
    """Action-level idempotency protection."""
    key = key or f"{case.case_id}:{action_type}:{token_hex(8)}"
    if db.scalar(select(ActionRecord).where(ActionRecord.idempotency_key == key)):
        return False

    act = ActionRecord(
        case=case,
        action_type=action_type,
        channel=channel,
        cost=cost,
        status=status,
        idempotency_key=key,
        result=result
    )
    db.add(act)
    case.total_intervention_cost += cost
    case.net_recovery_value = case.recovered_amount - case.total_intervention_cost
    return True


def mark_recovered(db: Session, case: RecoveryCase, source: str) -> None:
    """Transitions a case into terminal RECOVERED state."""
    if case.recovered:
        return
    case.recovered = True
    case.recovered_amount = case.amount
    case.net_recovery_value = case.recovered_amount - case.total_intervention_cost
    case.state = CaseState.RECOVERED.value
    case.current_action = "STOP"
    case.closed_at = now_utc()
    case.updated_at = now_utc()

    record_action(db, case, "VERIFY_PAYMENT", "SUCCEEDED", {"source": source, "outcome": "PAYMENT_CAPTURED"}, cost=0)

    promise = db.scalar(select(PaymentPromise).where(PaymentPromise.case_id_ref == case.id))
    if promise:
        promise.status = "FULFILLED"
        promise.fulfilled_at = now_utc()


def create_or_update_promise(db: Session, case: RecoveryCase, message: str, promised_at: Any, confidence: float = 0.9) -> None:
    promise = db.scalar(select(PaymentPromise).where(PaymentPromise.case_id_ref == case.id))
    if promise:
        promise.promise_text = message
        promise.promised_at = promised_at
        promise.confidence = confidence
        promise.status = "PENDING"
    else:
        db.add(PaymentPromise(
            case_id_ref=case.id,
            customer_name=case.customer_name,
            amount=case.amount,
            promise_text=message,
            promised_at=promised_at,
            confidence=confidence,
            status="PENDING",
        ))

    db.add(ScheduledTask(
        case_id=case.case_id,
        task_type="VERIFY_PROMISE",
        scheduled_at=promised_at,
        idempotency_key=f"promise-verify:{case.case_id}:{int(promised_at.timestamp())}",
    ))


def process_case(db: Session, case: RecoveryCase, event_type: str, message: str = "", force_provider: Optional[str] = None) -> dict[str, Any]:
    policy_row = latest_policy(db)
    policy = policy_row.configuration
    available_actions = ["WAIT", "VERIFY", "RECOVER", "ESCALATE", "STOP"]

    customer_opted_out = False
    if case.customer and case.customer.communication_opt_out:
        customer_opted_out = True

    # 1. Deterministic Strategy Scoring
    strategy_scores, scored_candidate = calculate_strategy_scores(case, policy)

    factors = {
        "recovered": case.recovered,
        "communication_count": case.communication_count,
        "retry_count": case.retry_count,
        "failure_category": case.failure_category,
        "amount": case.amount,
        "customer_opted_out": customer_opted_out,
    }

    ai_result: dict[str, Any] = {}
    ai_status = "unused"
    llm_provider = "fallback-rules"
    latency_ms = 0
    selected = scored_candidate
    reason = "Selected via deterministic strategy scoring"

    normalized_event = event_type.upper()

    # Rule: Terminal state check
    if case.recovered or "CAPTURED" in normalized_event or "SUCCESS" in normalized_event or "PAID" in normalized_event:
        mark_recovered(db, case, event_type)
        db.commit()
        db.refresh(case)  # Ensure case state is up‑to‑date for subsequent actions
        selected = "STOP"
        reason = "Payment captured successfully; recovery complete."
    elif customer_opted_out or "OPT_OUT" in normalized_event:
        selected = "STOP"
        reason = "Customer has opted out of communication."
    elif "FAIL" in normalized_event or "HALT" in normalized_event:
        if case.failure_category in {"HIGH_VALUE_INVOICE"} or case.amount >= policy.get("minimum_amount_for_human_escalation", 10000):
            selected = "ESCALATE"
            reason = f"High-value invoice failure (INR {case.amount}) routed to human operator."
        elif case.failure_category in {"UNCERTAIN_OR_TEMPORARY"}:
            selected = "VERIFY"
            reason = "Payment state is uncertain or temporary; verify before contacting customer."
        elif case.failure_category in {"PAYMENT_METHOD_ISSUE"}:
            selected = "RECOVER"
            reason = "Payment method failed; request customer to update payment method or send recovery link."
        else:
            selected = "WAIT"
            reason = "Temporary failure detected; allowing safe cooldown window."

    # 2. AI Intelligence (if customer message provided)
    if message:
        try:
            intent_obj, provider_name, lat = _execute_coro_sync(
                analyze_intent_resilient(message, force_provider=force_provider)
            )
            ai_result = intent_obj.model_dump() if hasattr(intent_obj, "model_dump") else dict(intent_obj)
            ai_status = provider_name
            llm_provider = provider_name
            latency_ms = int(lat)
        except Exception:
            fallback_obj = fallback_ai(message)
            ai_result = fallback_obj.model_dump() if hasattr(fallback_obj, "model_dump") else dict(fallback_obj)
            ai_status = "fallback-rules"
            llm_provider = "fallback-rules"
            latency_ms = 1

        intent = ai_result.get("intent", "UNKNOWN")
        confidence = ai_result.get("confidence", 0.5)

        if intent == "OPT_OUT":
            selected = "STOP"
            reason = "Customer opt-out detected via language analysis."
            if case.customer:
                case.customer.communication_opt_out = True
        elif intent == "PAYMENT_COMPLETED_CLAIM":
            # Crucial Rule: Customer claim NEVER directly marks recovered without external gateway verification
            selected = "VERIFY"
            reason = "Customer claims payment was made; must verify with gateway before closing case."
        elif intent == "PROMISE_TO_PAY" and confidence >= policy.get("ai_confidence_threshold", 0.70):
            selected = "WAIT"
            reason = f"Promise to pay recognized (confidence: {confidence:.2f}); waiting for promised settlement."
            temporal_expr = ai_result.get("temporal_expression") or message
            promised_time = resolve_temporal_expression(temporal_expr)
            case.promise_time = promised_time
            create_or_update_promise(db, case, message, promised_time, confidence)
        elif intent == "DISPUTE":
            selected = "ESCALATE"
            reason = "Customer raised dispute or fraud claim; escalating to operator."
        elif intent == "NEED_HELP":
            selected = "RECOVER"
            reason = "Customer requested payment link or assistance."

    # 3. Policy Engine Validation (Final Gatekeeper)
    policy_result = "APPROVED"

    max_attempts = policy.get("maximum_automated_attempts", 3)
    max_comms = policy.get("maximum_customer_communications", 3)
    escalate_threshold = policy.get("minimum_amount_for_human_escalation", 10000)

    if case.recovered:
        selected = "STOP"
        policy_result = "APPROVED"
        reason = "Case is recovered; all actions stopped."
    elif case.retry_count >= max_attempts and selected in {"WAIT", "VERIFY", "RECOVER"}:
        policy_result = "BLOCKED"
        selected = "ESCALATE" if case.amount >= escalate_threshold else "STOP"
        reason = "Maximum automated recovery retry limit reached."
    elif case.communication_count >= max_comms and selected == "RECOVER":
        policy_result = "BLOCKED"
        selected = "STOP"
        reason = "Maximum allowed customer communication frequency exceeded."
    elif case.amount >= escalate_threshold and selected == "RECOVER":
        selected = "ESCALATE"
        reason = f"High-value amount (INR {case.amount}) exceeds policy escalation threshold."

    # 4. State Transitions & Action Execution
    if selected == "WAIT":
        case.state = CaseState.WAIT.value
        record_action(db, case, "WAIT", "EXECUTED", {"reason": reason}, cost=0)
    elif selected == "VERIFY":
        case.state = CaseState.VERIFY.value
        record_action(db, case, "VERIFY_PAYMENT", "QUEUED", {"reason": reason}, cost=0)
    elif selected == "RECOVER":
        case.state = CaseState.RECOVER.value
        case.communication_count += 1
        case.retry_count += 1
        link_cost = policy.get("channel_costs", {}).get("RECOVERY_LINK", 2)
        record_action(
            db, case, "GENERATE_RECOVERY_LINK", "SENT",
            {"reason": reason, "recovery_url": f"https://rzp.io/i/{case.case_id.lower()}"},
            channel="RECOVERY_LINK", cost=link_cost
        )
        db.add(Notification(
            case=case, channel="RECOVERY_LINK", recipient=case.customer_email,
            message_content=f"Please complete your payment of INR {case.amount} using secure link.",
            cost=link_cost, status="SENT"
        ))
    elif selected == "ESCALATE":
        case.state = CaseState.ESCALATED.value
        escalate_cost = policy.get("channel_costs", {}).get("ESCALATE", 100)
        record_action(
            db, case, "ESCALATE_TO_OPERATOR", "QUEUED",
            {"reason": reason, "priority": "HIGH"}, cost=escalate_cost
        )
    elif selected == "STOP":
        case.state = CaseState.RECOVERED.value if case.recovered else CaseState.STOP.value
        record_action(db, case, "STOP", "EXECUTED", {"reason": reason}, cost=0)

    case.current_action = selected
    case.ai_status = ai_status
    case.strategy_score = strategy_scores.get(selected, {}).get("score", 50)
    case.net_recovery_value = case.recovered_amount - case.total_intervention_cost
    case.updated_at = now_utc()

    # 5. Append-only Decision Ledger
    decision = DecisionLedger(
        case=case,
        policy_version=case.policy_version,
        available_actions=available_actions,
        strategy_scores=strategy_scores,
        selected_action=selected,
        deterministic_factors=factors,
        ai_analysis=ai_result,
        ai_recommendation={
            "suggested_action": ai_result.get("recommended_next_action"),
            "confidence": ai_result.get("confidence")
        },
        policy_result=policy_result,
        policy_reason=reason,
        final_outcome="RECOVERED" if case.recovered else (CaseState.STOP.value if case.state == CaseState.STOP.value else None),
        llm_provider=llm_provider,
    )
    db.add(decision)
    db.commit()
    db.refresh(case)

    # 6. Broadcast via WebSocket
    broadcast({
        "type": "CASE_UPDATED",
        "case": to_case_dict(case),
        "decision": {
            "selected_action": selected,
            "policy_result": policy_result,
            "policy_reason": reason,
            "ai": ai_result,
            "llm_provider": llm_provider,
            "latency_ms": latency_ms,
        },
        "timestamp": now_utc().isoformat(),
    })

    return {
        "selected_action": selected,
        "policy_result": policy_result,
        "policy_reason": reason,
        "ai": ai_result,
        "llm_provider": llm_provider,
        "latency_ms": latency_ms,
        "strategy_score": case.strategy_score,
        "net_recovery_value": case.net_recovery_value,
    }


def record_ai_evaluation(
    db: Session,
    scenario_id: str,
    customer_message: str,
    expected_intent: str,
    predicted_intent: str,
    expected_action: str,
    suggested_action: str,
    policy_result: str,
    confidence: float,
    is_fallback: bool = False,
    llm_provider: str = "fallback-rules",
    latency_ms: int = 0
) -> AIEvaluationRecord:
    """
    Persists an AI evaluation benchmark run to the ai_evaluation_records table
    with strict llm_provider tracking.
    """
    rec = AIEvaluationRecord(
        scenario_id=scenario_id,
        customer_message=customer_message,
        expected_intent=expected_intent,
        predicted_intent=predicted_intent,
        expected_action=expected_action,
        suggested_action=suggested_action,
        policy_result=policy_result,
        confidence=confidence,
        is_fallback=is_fallback,
        llm_provider=llm_provider,
        latency_ms=latency_ms,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec
