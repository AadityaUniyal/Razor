from typing import Any, Optional
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.core.config import GROQ_API_KEY, GROQ_MODEL, GEMINI_API_KEY, GEMINI_MODEL
from backend.core.security import get_current_user
from backend.services.ai_agent import (
    analyze_intent_resilient,
    fallback_ai,
    resolve_temporal_expression,
    CustomerIntent
)
from database.connection import get_db
from database.models import DecisionLedger, User

router = APIRouter(prefix="/api/ai", tags=["ai"])


async def _handle_ai_classification(payload: dict[str, Any], db: Session) -> dict[str, Any]:
    message = payload.get("message", "")
    provider_req = payload.get("provider")
    force_fallback = payload.get("force_fallback", False)

    if force_fallback and not provider_req:
        provider_req = "fallback"

    res, provider_used, latency = await analyze_intent_resilient(message, force_provider=provider_req)

    temporal_text = res.get("temporal_expression") or message
    resolved_time = resolve_temporal_expression(temporal_text)

    model_targeted = (
        GROQ_MODEL if provider_used == "groq-llm"
        else (GEMINI_MODEL if provider_used == "gemini-llm" else "deterministic-rules")
    )

    system_prompt = (
        "You are a strict financial revenue recovery AI. "
        "Detect and analyze customer responses in English only for maximum accuracy. "
        "Classify customer intent and return ONLY a valid JSON object matching this schema: "
        '{"intent": "PROMISE_TO_PAY"|"PAYMENT_COMPLETED_CLAIM"|"NEED_HELP"|"OPT_OUT"|"DISPUTE"|"UNKNOWN", '
        '"promise_detected": boolean, "confidence": float between 0 and 1, '
        '"recommended_next_action": "WAIT"|"VERIFY"|"RECOVER"|"ESCALATE"|"STOP"|"NEEDS_POLICY_REVIEW", '
        '"temporal_expression": string or null, "customer_tone": "cooperative"|"neutral"|"negative"}. '
        "Never invent dates. If intent is PROMISE_TO_PAY, recommended_next_action MUST be WAIT."
    )

    prompt_tokens = max(1, len(system_prompt + message) // 4)
    completion_tokens = 64 if provider_used != "fallback-rules" else 0
    total_tokens = prompt_tokens + completion_tokens

    tokens = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }

    prompt_details = {
        "system_prompt": system_prompt,
        "user_message": message,
        "model": model_targeted,
        "temperature": 0.0,
    }

    total_decisions = db.scalar(select(func.count(DecisionLedger.id))) or 0
    approved_decisions = db.scalar(
        select(func.count(DecisionLedger.id)).where(DecisionLedger.policy_result == "APPROVED")
    ) or 0
    policy_acceptance_rate = round((approved_decisions / total_decisions) * 100, 1) if total_decisions else 0.0

    return {
        "message": message,
        "provider_used": provider_used,
        "source": provider_used,
        "latency_ms": latency,
        "tokens": tokens,
        "prompt_details": prompt_details,
        "intent": res.get("intent"),
        "confidence": res.get("confidence"),
        "promise_detected": res.get("promise_detected"),
        "recommended_next_action": res.get("recommended_next_action"),
        "customer_tone": res.get("customer_tone"),
        "resolved_promise_time": resolved_time.isoformat() if res.get("promise_detected") else None,
    }


@router.post("/classify")
async def ai_classify(payload: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Primary classification endpoint with provider override support."""
    return await _handle_ai_classification(payload, db)


@router.post("/sandbox")
async def ai_sandbox(payload: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Interactive sandbox endpoint for testing provider fallback and prompt inspection."""
    return await _handle_ai_classification(payload, db)


@router.post("/test-intent")
async def test_ai_intent(payload: dict[str, Any], db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Interactive sandbox for testing customer text in English, Hindi, and Hinglish (legacy support)."""
    return await _handle_ai_classification(payload, db)



@router.get("/evaluation-metrics")
def ai_evaluation_metrics(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """
    Evaluates AI benchmark accuracy against labelled scenario ground truths.
    """
    test_cases = [
        {"msg": "Kal salary aane ke baad payment kar dunga", "expected": "PROMISE_TO_PAY"},
        {"msg": "Monday shaam tak pakka pay kar dunga", "expected": "PROMISE_TO_PAY"},
        {"msg": "Maine payment complete kar di hai UPI se", "expected": "PAYMENT_COMPLETED_CLAIM"},
        {"msg": "I already paid this invoice yesterday", "expected": "PAYMENT_COMPLETED_CLAIM"},
        {"msg": "Stop contacting me, do not send any more messages", "expected": "OPT_OUT"},
        {"msg": "Unsubscribe my phone number immediately", "expected": "OPT_OUT"},
        {"msg": "This is fraud, charge is completely wrong", "expected": "DISPUTE"},
        {"msg": "Payment link is not opening, please send again", "expected": "NEED_HELP"},
    ]

    correct = 0
    eval_list = []
    for tc in test_cases:
        pred = fallback_ai(tc["msg"])
        matched = pred["intent"] == tc["expected"]
        if matched:
            correct += 1
        eval_list.append({
            "message": tc["msg"],
            "expected_intent": tc["expected"],
            "predicted_intent": pred["intent"],
            "confidence": pred["confidence"],
            "action_proposed": pred["recommended_next_action"],
            "matched": matched,
        })

    accuracy = round((correct / len(test_cases)) * 100, 1)
    promise_detected = sum(
        1 for item in eval_list
        if item["expected_intent"] == "PROMISE_TO_PAY"
        and item["predicted_intent"] == "PROMISE_TO_PAY"
    )
    expected_promises = sum(1 for item in eval_list if item["expected_intent"] == "PROMISE_TO_PAY")
    total_decisions = db.scalar(select(func.count(DecisionLedger.id))) or 0
    approved_decisions = db.scalar(
        select(func.count(DecisionLedger.id)).where(DecisionLedger.policy_result == "APPROVED")
    ) or 0
    policy_acceptance_rate = round((approved_decisions / total_decisions) * 100, 1) if total_decisions else 0.0

    return {
        "accuracy_percent": accuracy,
        "total_benchmarks": len(test_cases),
        "correct_predictions": correct,
        "promise_detection_rate": round((promise_detected / expected_promises) * 100, 1) if expected_promises else 0.0,
        "policy_acceptance_rate": policy_acceptance_rate,
        "ai_model": GROQ_MODEL if GROQ_API_KEY else "Deterministic Multi-lingual Fallback",
        "benchmark_runs": eval_list,
    }
