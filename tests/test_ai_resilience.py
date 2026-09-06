"""
Comprehensive test suite for Milestone 2: Multi-Provider AI Resilience & Hinglish Intelligence.
Verifies:
1. Pydantic CustomerIntent schema equivalence across all providers.
2. Primary Groq execution with standardized 'groq-llm' provider name.
3. Secondary Google Gemini execution with standardized 'gemini-llm' provider name.
4. Tertiary deterministic fallback execution with 'fallback-rules'.
5. Multi-provider resilience router automatic fallback hierarchy (Groq -> Gemini -> Fallback).
6. Forced provider overrides ('groq', 'gemini', 'fallback').
7. Hinglish safety guardrails ('mat bhejo' strictly produces OPT_OUT and STOP, never NEED_HELP).
8. Hinglish and English temporal expression resolution (weekdays, kal, parso, salary, agla hafta, mahine ke aakhri).
9. DecisionLedger and AIEvaluationRecord provider tracking in database.
10. AI API routes (/api/ai/classify, /api/ai/sandbox, /api/ai/test-intent) with provider override, tokens, prompt details.
"""
import calendar
import json
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

from backend.core.config import GEMINI_API_KEY, GEMINI_MODEL, GROQ_API_KEY, GROQ_MODEL
from backend.services.ai_agent import (
    CustomerIntent,
    analyze_intent_resilient,
    fallback_ai,
    gemini_intent,
    groq_intent,
    resolve_temporal_expression,
)
from backend.services.policy_engine import process_case, record_ai_evaluation
from database.connection import SessionLocal
from database.models import CaseState, RecoveryCase, DecisionLedger, AIEvaluationRecord, now_utc
from backend.main import app


# ---------------------------------------------------------------------------
# Test Helpers & Fixtures
# ---------------------------------------------------------------------------
SAMPLE_GROQ_RESPONSE = {
    "choices": [
        {
            "message": {
                "content": json.dumps({
                    "intent": "PROMISE_TO_PAY",
                    "promise_detected": True,
                    "confidence": 0.95,
                    "recommended_next_action": "WAIT",
                    "temporal_expression": "Monday",
                    "customer_tone": "cooperative"
                })
            }
        }
    ]
}

SAMPLE_GEMINI_RESPONSE = {
    "candidates": [
        {
            "content": {
                "parts": [
                    {
                        "text": json.dumps({
                            "intent": "PROMISE_TO_PAY",
                            "promise_detected": True,
                            "confidence": 0.91,
                            "recommended_next_action": "WAIT",
                            "temporal_expression": "kal sham",
                            "customer_tone": "cooperative"
                        })
                    }
                ]
            }
        }
    ]
}


def fresh_auth_client() -> TestClient:
    client = TestClient(app)
    resp = client.post("/api/auth/login", data={"email": "admin@razorrescue.local", "password": "password123"})
    assert resp.status_code == 200
    return client


# ---------------------------------------------------------------------------
# 1. Schema Equivalence Across All Providers
# ---------------------------------------------------------------------------
def test_schema_equivalence_and_dual_access():
    """Verify that CustomerIntent supports both model attribute access and dictionary access."""
    sample = {
        "intent": "PROMISE_TO_PAY",
        "promise_detected": True,
        "confidence": 0.94,
        "recommended_next_action": "WAIT",
        "temporal_expression": "tomorrow",
        "customer_tone": "cooperative",
    }
    intent = CustomerIntent.model_validate(sample)

    # Attribute access
    assert intent.intent == "PROMISE_TO_PAY"
    assert intent.promise_detected is True
    assert intent.confidence == 0.94
    assert intent.recommended_next_action == "WAIT"
    assert intent.temporal_expression == "tomorrow"
    assert intent.customer_tone == "cooperative"

    # Dict-like access (__getitem__ & get)
    assert intent["intent"] == "PROMISE_TO_PAY"
    assert intent["promise_detected"] is True
    assert intent.get("recommended_next_action") == "WAIT"
    assert intent.get("non_existent", "default_val") == "default_val"
    assert "intent" in intent

    # Serialization
    dumped = intent.model_dump()
    assert isinstance(dumped, dict)
    assert dumped["intent"] == "PROMISE_TO_PAY"


def test_schema_sanitization_and_resilience():
    """Verify schema sanitizes out-of-spec tone, casing, and actions without throwing validation errors."""
    loose_data = {
        "intent": "promise_to_pay",
        "promise_detected": True,
        "confidence": 1.5,  # Out of range, should clamp to 1.0
        "recommended_next_action": "wait",
        "temporal_expression": "Monday",
        "customer_tone": "assertive",  # Non-standard tone, mapped to neutral
    }
    validated = CustomerIntent.model_validate(loose_data)
    assert validated.intent == "PROMISE_TO_PAY"
    assert validated.confidence == 1.0
    assert validated.recommended_next_action == "WAIT"
    assert validated.customer_tone == "neutral"


# ---------------------------------------------------------------------------
# 2. Primary Groq Provider Execution
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_groq_intent_success():
    """Verify primary Groq API call returns strict CustomerIntent, 'groq-llm' provider, and latency."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = SAMPLE_GROQ_RESPONSE
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        with patch("backend.services.ai_agent.GROQ_API_KEY", "mock_groq_key"):
            intent, provider, latency = await groq_intent("Monday ko pakka pay kar dunga")

            assert isinstance(intent, CustomerIntent)
            assert intent.intent == "PROMISE_TO_PAY"
            assert intent.promise_detected is True
            assert provider == "groq-llm"
            assert latency >= 0


# ---------------------------------------------------------------------------
# 3. Secondary Google Gemini Provider Execution
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_gemini_intent_success():
    """Verify secondary Gemini API call returns strict CustomerIntent, 'gemini-llm' provider, and latency."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = SAMPLE_GEMINI_RESPONSE
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        with patch("backend.services.ai_agent.GEMINI_API_KEY", "mock_gemini_key"):
            intent, provider, latency = await gemini_intent("Kal sham tak amount transfer kar dunga")

            assert isinstance(intent, CustomerIntent)
            assert intent.intent == "PROMISE_TO_PAY"
            assert intent.promise_detected is True
            assert intent.recommended_next_action == "WAIT"
            assert provider == "gemini-llm"
            assert latency >= 0


# ---------------------------------------------------------------------------
# 4. Multi-Provider Fallback Hierarchy Router
# ---------------------------------------------------------------------------
@pytest.mark.anyio
async def test_resilient_router_groq_primary():
    """Auto mode: Groq succeeds, so Groq is selected."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = SAMPLE_GROQ_RESPONSE
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        with patch("backend.services.ai_agent.GROQ_API_KEY", "mock_groq_key"):
            intent, provider, latency = await analyze_intent_resilient("I will pay next Monday")
            assert provider == "groq-llm"
            assert intent.intent == "PROMISE_TO_PAY"


@pytest.mark.anyio
async def test_resilient_router_fallback_to_gemini_on_groq_failure():
    """Auto mode: Groq encounters 429 rate limit or network failure -> Seamlessly falls back to Gemini."""
    def mock_post(url, **kwargs):
        if "groq.com" in str(url):
            raise RuntimeError("Groq 429 Rate Limit Exceeded")
        mock_gemini = MagicMock()
        mock_gemini.status_code = 200
        mock_gemini.json.return_value = SAMPLE_GEMINI_RESPONSE
        mock_gemini.raise_for_status = MagicMock()
        return mock_gemini

    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        with patch("backend.services.ai_agent.GROQ_API_KEY", "mock_groq_key"):
            with patch("backend.services.ai_agent.GEMINI_API_KEY", "mock_gemini_key"):
                intent, provider, latency = await analyze_intent_resilient("Kal sham salary aate hi kar dunga")
                assert provider == "gemini-llm"
                assert intent.intent == "PROMISE_TO_PAY"


@pytest.mark.anyio
async def test_resilient_router_fallback_to_rules_when_both_llms_fail():
    """Auto mode: Both Groq and Gemini fail -> Seamlessly falls back to deterministic rules."""
    def mock_post_all_fail(url, **kwargs):
        raise RuntimeError("Service Unavailable 503")

    with patch("httpx.AsyncClient.post", side_effect=mock_post_all_fail):
        with patch("backend.services.ai_agent.GROQ_API_KEY", "mock_groq_key"):
            with patch("backend.services.ai_agent.GEMINI_API_KEY", "mock_gemini_key"):
                intent, provider, latency = await analyze_intent_resilient("Maine UPI se payment kar diya hai")
                assert provider == "fallback-rules"
                assert intent.intent == "PAYMENT_COMPLETED_CLAIM"
                assert intent.recommended_next_action == "VERIFY"


@pytest.mark.anyio
async def test_resilient_router_forced_providers():
    """Forced provider overrides execute the requested provider directly."""
    # Force fallback
    intent, provider, latency = await analyze_intent_resilient("Stop sending messages", force_provider="fallback")
    assert provider == "fallback-rules"
    assert intent.intent == "OPT_OUT"
    assert intent.recommended_next_action == "STOP"

    # Force Gemini
    mock_gemini_resp = MagicMock()
    mock_gemini_resp.status_code = 200
    mock_gemini_resp.json.return_value = SAMPLE_GEMINI_RESPONSE
    mock_gemini_resp.raise_for_status = MagicMock()
    with patch("httpx.AsyncClient.post", return_value=mock_gemini_resp):
        with patch("backend.services.ai_agent.GEMINI_API_KEY", "mock_gemini_key"):
            intent, provider, latency = await analyze_intent_resilient("Monday ko pay kar dunga", force_provider="gemini")
            assert provider == "gemini-llm"


# ---------------------------------------------------------------------------
# 5. Hinglish Safety & Opt-Out Parsing Guardrails
# ---------------------------------------------------------------------------
def test_hinglish_mat_bhejo_opt_out_never_need_help():
    """CRITICAL SAFETY TEST: 'mat bhejo' MUST map to OPT_OUT and STOP, never NEED_HELP."""
    phrases = [
        "mat bhejo",
        "message mat karo",
        "msg mat bhejo",
        "call mat karo",
        "pareshan mat karo",
        "band karo",
        "kabhi mat bhejo",
        "mujhe koi message mat bhejo please",
    ]
    for phrase in phrases:
        res = fallback_ai(phrase)
        assert res.intent == "OPT_OUT", f"Failed on phrase: '{phrase}' (got intent {res.intent})"
        assert res.recommended_next_action == "STOP", f"Failed on phrase: '{phrase}' (got action {res.recommended_next_action})"
        assert res.customer_tone == "negative"


def test_hinglish_dispute_and_claim_and_help():
    """Verify distinct Hinglish categories are parsed accurately."""
    # Dispute
    dispute = fallback_ai("Ye fraud hai, galat charge lagaya hai")
    assert dispute.intent == "DISPUTE"
    assert dispute.recommended_next_action == "ESCALATE"

    # Payment done
    paid = fallback_ai("Maine complete kar diya hai bheja tha")
    assert paid.intent == "PAYMENT_COMPLETED_CLAIM"
    assert paid.recommended_next_action == "VERIFY"

    # Genuine help request
    help_req = fallback_ai("Payment link nahi chal raha help karo kaise karein")
    assert help_req.intent == "NEED_HELP"
    assert help_req.recommended_next_action == "RECOVER"


# ---------------------------------------------------------------------------
# 6. Hinglish & English Temporal Expression Resolution
# ---------------------------------------------------------------------------
def test_temporal_expression_resolution():
    """Test resolution of Hinglish & English temporal days and relative dates."""
    base = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)  # Monday

    # kal -> +1 day (Tuesday)
    t_kal = resolve_temporal_expression("kal sham", base_time=base)
    assert t_kal.day == 8
    assert t_kal.hour == 18 and t_kal.minute == 30

    # parso -> +2 days (Wednesday)
    t_parso = resolve_temporal_expression("parso dophar", base_time=base)
    assert t_parso.day == 9
    assert t_parso.hour == 14

    # somwar -> next Monday (+7 days)
    t_somwar = resolve_temporal_expression("somwar subah", base_time=base)
    assert t_somwar.day == 14
    assert t_somwar.hour == 10

    # salary -> +3 days
    t_salary = resolve_temporal_expression("salary aane ke baad", base_time=base)
    assert t_salary.day == 10

    # agla hafta -> +7 days
    t_hafta = resolve_temporal_expression("agla hafta", base_time=base)
    assert t_hafta.day == 14

    # mahine ke aakhri -> month end
    t_month_end = resolve_temporal_expression("mahine ke aakhri me pay karunga", base_time=base)
    last_day = calendar.monthrange(base.year, base.month)[1]
    assert t_month_end.day == last_day
    assert t_month_end.hour == 18


# ---------------------------------------------------------------------------
# 7. Database Persistence & Provider Tracking in DecisionLedger
# ---------------------------------------------------------------------------
def test_decision_ledger_provider_recording():
    """Verify process_case stores llm_provider accurately in DecisionLedger."""
    with SessionLocal() as db:
        # The shared Neon database contains historical terminal cases. Select
        # an active case so this persistence test exercises promise handling,
        # not the separate terminal-state invariant.
        case = (
            db.query(RecoveryCase)
            .filter(
                RecoveryCase.recovered.is_(False),
                RecoveryCase.state != CaseState.STOP.value,
            )
            .order_by(RecoveryCase.id)
            .first()
        )
        if not case:
            pytest.skip("No RecoveryCase found in database to test process_case")

        # Run process_case with a customer promise message forcing fallback
        result = process_case(db, case, "customer.message", "Kal shaam ko payment kar dunga", force_provider="fallback")
        assert result["llm_provider"] == "fallback-rules"

        # Verify ledger record
        latest_decision = db.query(DecisionLedger).filter(DecisionLedger.case_id_ref == case.id).order_by(DecisionLedger.id.desc()).first()
        assert latest_decision is not None
        assert latest_decision.llm_provider == "fallback-rules"
        assert latest_decision.selected_action == "WAIT"


def test_ai_evaluation_record_persistence():
    """Verify record_ai_evaluation correctly persists llm_provider in database."""
    with SessionLocal() as db:
        rec = record_ai_evaluation(
            db=db,
            scenario_id="SCENARIO_TEST_M2",
            customer_message="Kal shaam pakka",
            expected_intent="PROMISE_TO_PAY",
            predicted_intent="PROMISE_TO_PAY",
            expected_action="WAIT",
            suggested_action="WAIT",
            policy_result="APPROVED",
            confidence=0.95,
            is_fallback=False,
            llm_provider="gemini-llm",
            latency_ms=180,
        )
        assert rec.id is not None
        assert rec.llm_provider == "gemini-llm"
        assert rec.latency_ms == 180


# ---------------------------------------------------------------------------
# 8. AI API Endpoints (Classify, Sandbox, Test-Intent)
# ---------------------------------------------------------------------------
def test_api_ai_classify_and_sandbox_endpoints():
    """Verify /api/ai/classify and /api/ai/sandbox support provider override, tokens, and prompt details."""
    client = fresh_auth_client()

    # 1. /api/ai/classify with force fallback
    payload = {
        "message": "Bhai kal salary aane ke baad payment kar dunga",
        "provider": "fallback",
    }
    resp = client.post("/api/ai/classify", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data["provider_used"] == "fallback-rules"
    assert data["intent"] == "PROMISE_TO_PAY"
    assert data["promise_detected"] is True
    assert data["recommended_next_action"] == "WAIT"
    assert data["tokens"]["total_tokens"] > 0
    assert "prompt_details" in data
    assert data["resolved_promise_time"] is not None

    # 2. /api/ai/sandbox with opt-out
    sandbox_payload = {
        "message": "mat bhejo koi msg",
        "provider": "fallback",
    }
    resp_sb = client.post("/api/ai/sandbox", json=sandbox_payload)
    assert resp_sb.status_code == 200
    data_sb = resp_sb.json()
    assert data_sb["intent"] == "OPT_OUT"
    assert data_sb["recommended_next_action"] == "STOP"
    assert data_sb["provider_used"] == "fallback-rules"

    # 3. /api/ai/test-intent backward compatibility
    resp_legacy = client.post("/api/ai/test-intent", json={"message": "Kal sham ko pakka dunga", "force_fallback": True})
    assert resp_legacy.status_code == 200
    data_legacy = resp_legacy.json()
    assert data_legacy["intent"] == "PROMISE_TO_PAY"
    assert data_legacy["resolved_promise_time"] is not None
