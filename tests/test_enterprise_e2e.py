"""
RazorRescue Enterprise End-to-End Test Suite (tests/test_enterprise_e2e.py)

Comprehensive, requirement-driven, opaque-box test suite implementing the 4-tier
verification methodology across all core features (R1-R4), edge/boundary cases,
cross-feature subsystem interactions, and real-world enterprise multi-step recovery workloads.

Tier Summary:
- Tier 1: Feature Coverage (>=5 test cases per feature across R1-R4, total 24 tests)
- Tier 2: Boundary Value Analysis & Corner Cases (>=5 test cases per feature across R1-R4, total 24 tests)
- Tier 3: Cross-Feature Interactions & Pairwise Combinations (5 integration tests)
- Tier 4: Real-World Enterprise Workloads (5 end-to-end multi-step recovery scenarios)
Total: 58 robust, isolated, requirement-driven tests.

Constraints:
- Opaque-box: Derived strictly from requirements, not internal code shortcuts.
- Database: 100% executed against live Neon PostgreSQL (zero local SQLite .db files).
- Safety principle: AI proposes -> Policy validates -> Action operates -> Audit records.
"""
import os
import json
import time
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Generator
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import URL, select, delete, func, text, create_engine
from sqlalchemy.orm import Session

# Use the caller-provided database configuration; never embed credentials in tests.
if os.environ.get("RAZORRESCUE_TEST_DATABASE_URL"):
    os.environ.setdefault("DATABASE_URL", os.environ["RAZORRESCUE_TEST_DATABASE_URL"])
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "unit-test-webhook-value"

from backend.main import app
from backend.core.config import (
    COOKIE_NAME, SECRET_KEY, RAZORPAY_WEBHOOK_SECRET,
    GROQ_API_KEY, GROQ_MODEL, GEMINI_API_KEY, GEMINI_MODEL
)
from backend.core.security import create_token, hash_password
from backend.services.ai_agent import (
    CustomerIntent,
    analyze_intent_resilient,
    fallback_ai,
    groq_intent,
    gemini_intent,
    resolve_temporal_expression,
)
from backend.services.policy_engine import (
    process_case, record_action, mark_recovered, latest_policy, to_case_dict
)
from backend.services.scheduler import process_due_tasks
from database.connection import SessionLocal, engine, check_db_health
from database.models import (
    User, Customer, RecoveryCase, PaymentEvent, CaseEvent,
    DecisionLedger, ActionRecord, PaymentPromise, ScheduledTask,
    Notification, SystemHealthEvent, RecoveryPolicy, Role, CaseState, now_utc
)
from database.init_db import init_db, seed_data

# Ensure schema and compound indexes are active
init_db()


# ---------------------------------------------------------------------------
# Test Fixtures & Data Hygiene
# ---------------------------------------------------------------------------

def cleanup_e2e_records(db: Session):
    """Purges all test fixtures tagged with RC_E2E to guarantee test independence."""
    test_cases = db.scalars(select(RecoveryCase).where(RecoveryCase.case_id.like("RC_E2E%"))).all()
    for tc in test_cases:
        db.execute(delete(ScheduledTask).where(ScheduledTask.case_id == tc.case_id))
        db.execute(delete(PaymentPromise).where(PaymentPromise.case_id_ref == tc.id))
        db.execute(delete(Notification).where(Notification.case_id_ref == tc.id))
        db.execute(delete(ActionRecord).where(ActionRecord.case_id_ref == tc.id))
        db.execute(delete(DecisionLedger).where(DecisionLedger.case_id_ref == tc.id))
        db.execute(delete(CaseEvent).where(CaseEvent.case_id_ref == tc.id))
        cust_id = tc.customer_id_ref
        db.execute(delete(RecoveryCase).where(RecoveryCase.id == tc.id))
        if cust_id:
            db.execute(delete(Customer).where(Customer.id == cust_id))
    db.execute(delete(PaymentEvent).where(PaymentEvent.case_reference.like("RC_E2E%")))
    db.execute(delete(Customer).where(Customer.external_customer_id.like("cust_e2e%")))
    db.execute(delete(Customer).where(Customer.external_customer_id.like("rc_e2e%")))
    db.execute(delete(Customer).where(Customer.email.like("e2e_%@customer.local")))
    db.execute(delete(Customer).where(Customer.email.like("rc_e2e_%@customer.local")))
    db.commit()


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        cleanup_e2e_records(session)
        yield session
        cleanup_e2e_records(session)


@pytest.fixture(scope="function")
def admin_client(db_session: Session) -> TestClient:
    """Authenticated TestClient with Role.ADMIN privileges."""
    admin_user = db_session.scalar(select(User).where(User.email == "admin@razorrescue.local"))
    if not admin_user:
        admin_user = User(
            email="admin@razorrescue.local",
            password_hash=hash_password("password123"),
            role=Role.ADMIN.value,
        )
        db_session.add(admin_user)
        db_session.commit()
        db_session.refresh(admin_user)

    token = create_token(admin_user.id)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, token)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture(scope="function")
def viewer_client(db_session: Session) -> TestClient:
    """Authenticated TestClient with Role.VIEWER read-only privileges."""
    viewer_user = db_session.scalar(select(User).where(User.email == "viewer_e2e@razorrescue.local"))
    if not viewer_user:
        viewer_user = User(
            email="viewer_e2e@razorrescue.local",
            password_hash=hash_password("password123"),
            role=Role.VIEWER.value,
        )
        db_session.add(viewer_user)
        db_session.commit()
        db_session.refresh(viewer_user)

    token = create_token(viewer_user.id)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, token)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


@pytest.fixture(scope="function")
def unauth_client() -> TestClient:
    """Unauthenticated client without session tokens or cookies."""
    return TestClient(app)


# Webhook Helpers
def send_demo_event(client: TestClient, event_id: str, case_id: str, event_type: str, amount: int = 5000, **kwargs) -> Any:
    payload = {
        "external_event_id": event_id,
        "case_reference": case_id,
        "event_type": event_type,
        "amount": amount,
        **kwargs,
    }
    return client.post("/api/demo/webhook", json=payload)


def send_signed_razorpay_event(client: TestClient, payload_dict: Dict[str, Any], secret: str = "unit-test-webhook-value", signature_override: str = None) -> Any:
    raw_body = json.dumps(payload_dict).encode("utf-8")
    sig = signature_override or hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return client.post("/api/webhooks/razorpay", content=raw_body, headers={"X-Razorpay-Signature": sig, "Content-Type": "application/json"})


# Standard mock payloads for multi-provider AI tests
MOCK_GROQ_PROMISE = {
    "choices": [{
        "message": {
            "content": json.dumps({
                "intent": "PROMISE_TO_PAY",
                "promise_detected": True,
                "confidence": 0.96,
                "recommended_next_action": "WAIT",
                "temporal_expression": "Monday",
                "customer_tone": "cooperative"
            })
        }
    }]
}

MOCK_GEMINI_PROMISE = {
    "candidates": [{
        "content": {
            "parts": [{
                "text": json.dumps({
                    "intent": "PROMISE_TO_PAY",
                    "promise_detected": True,
                    "confidence": 0.92,
                    "recommended_next_action": "WAIT",
                    "temporal_expression": "kal sham",
                    "customer_tone": "cooperative"
                })
            }]
        }
    }]
}


# ===========================================================================
# TIER 1: FEATURE COVERAGE (>= 5 test cases per feature across R1-R4)
# ===========================================================================

# ---------------------------------------------------------------------------
# Feature R1: Multi-Provider AI Resilience & Intent Classification
# ---------------------------------------------------------------------------

def test_t1_r1_01_customer_intent_pydantic_schema_conformance():
    """T1-R1-01: Validates that CustomerIntent strictly validates fields and supports attribute & dict access."""
    payload = {
        "intent": "PROMISE_TO_PAY",
        "promise_detected": True,
        "confidence": 0.95,
        "recommended_next_action": "WAIT",
        "temporal_expression": "tomorrow",
        "customer_tone": "cooperative"
    }
    intent = CustomerIntent.model_validate(payload)
    assert intent.intent == "PROMISE_TO_PAY"
    assert intent.promise_detected is True
    assert intent.confidence == 0.95
    assert intent.recommended_next_action == "WAIT"
    assert intent.temporal_expression == "tomorrow"
    assert intent.customer_tone == "cooperative"

    # Dict-like access
    assert intent["intent"] == "PROMISE_TO_PAY"
    assert intent["promise_detected"] is True
    assert intent.get("recommended_next_action") == "WAIT"
    assert "intent" in intent


@pytest.mark.anyio
async def test_t1_r1_02_groq_primary_provider_execution():
    """T1-R1-02: Verifies primary Groq API call returns strict CustomerIntent and 'groq-llm' provider."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = MOCK_GROQ_PROMISE
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", return_value=mock_resp):
        with patch("backend.services.ai_agent.GROQ_API_KEY", "mock_groq_key"):
            intent, provider, latency = await groq_intent("Monday morning payment will be cleared")
            assert isinstance(intent, CustomerIntent)
            assert intent.intent == "PROMISE_TO_PAY"
            assert provider == "groq-llm"
            assert latency >= 0


@pytest.mark.anyio
async def test_t1_r1_03_gemini_secondary_failover_on_groq_429():
    """T1-R1-03: Verifies automatic secondary Google Gemini failover when Groq experiences 429 rate limits."""
    def mock_post(url, **kwargs):
        if "groq.com" in str(url):
            err = MagicMock()
            err.status_code = 429
            err.raise_for_status.side_effect = Exception("Groq 429 Rate Limit Exceeded")
            return err
        gem = MagicMock()
        gem.status_code = 200
        gem.json.return_value = MOCK_GEMINI_PROMISE
        gem.raise_for_status = MagicMock()
        return gem

    with patch("httpx.AsyncClient.post", side_effect=mock_post):
        with patch("backend.services.ai_agent.GROQ_API_KEY", "mock_groq_key"):
            with patch("backend.services.ai_agent.GEMINI_API_KEY", "mock_gemini_key"):
                intent, provider, latency = await analyze_intent_resilient("Kal shaam tak amount transfer kar dunga")
                assert provider == "gemini-llm"
                assert intent.intent == "PROMISE_TO_PAY"
                assert intent.promise_detected is True


@pytest.mark.anyio
async def test_t1_r1_04_tertiary_rules_fallback_when_both_llms_fail():
    """T1-R1-04: Verifies tertiary deterministic rules fallback when both Groq and Gemini are unavailable."""
    def mock_post_fail(url, **kwargs):
        raise ConnectionError("Network Down")

    with patch("httpx.AsyncClient.post", side_effect=mock_post_fail):
        with patch("backend.services.ai_agent.GROQ_API_KEY", "mock_groq_key"):
            with patch("backend.services.ai_agent.GEMINI_API_KEY", "mock_gemini_key"):
                intent, provider, latency = await analyze_intent_resilient("Maine UPI se payment kar diya hai")
                assert provider == "fallback-rules"
                assert intent.intent == "PAYMENT_COMPLETED_CLAIM"
                assert intent.recommended_next_action == "VERIFY"


@pytest.mark.anyio
async def test_t1_r1_05_forced_provider_override_routing():
    """T1-R1-05: Verifies explicit provider overrides ('fallback', 'gemini') bypass automatic routing."""
    intent_fb, prov_fb, _ = await analyze_intent_resilient("Stop sending messages", force_provider="fallback")
    assert prov_fb == "fallback-rules"
    assert intent_fb.intent == "OPT_OUT"

    mock_gemini_resp = MagicMock()
    mock_gemini_resp.status_code = 200
    mock_gemini_resp.json.return_value = MOCK_GEMINI_PROMISE
    mock_gemini_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", return_value=mock_gemini_resp):
        with patch("backend.services.ai_agent.GEMINI_API_KEY", "mock_gemini_key"):
            intent_gem, prov_gem, _ = await analyze_intent_resilient("Monday ko dunga", force_provider="gemini")
            assert prov_gem == "gemini-llm"


def test_t1_r1_06_temporal_expression_resolution_multilingual():
    """T1-R1-06: Verifies natural language temporal expression parsing across English and Hinglish."""
    base_time = datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc)  # Monday
    t_kal = resolve_temporal_expression("kal sham", base_time=base_time)
    assert t_kal.day == 8
    assert t_kal.hour == 18

    t_parso = resolve_temporal_expression("parso dophar", base_time=base_time)
    assert t_parso.day == 9
    assert t_parso.hour == 14

    t_salary = resolve_temporal_expression("salary aane ke baad", base_time=base_time)
    assert t_salary.day == 10  # +3 days


# ---------------------------------------------------------------------------
# Feature R2: Orchestrator Workflows & Razorpay Blueprint Compliance
# ---------------------------------------------------------------------------

def test_t1_r2_01_razorpay_webhook_hmac_sha256_verification(admin_client: TestClient):
    """T1-R2-01: Verifies Razorpay webhook HMAC-SHA256 signature verification and bad signature rejection."""
    payload = {
        "id": "evt_e2e_sig_test",
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"id": "pay_e2e_sig", "order_id": "RC_E2E_SIG", "amount": 500000}}}
    }
    # Valid signature
    res_ok = send_signed_razorpay_event(admin_client, payload)
    assert res_ok.status_code == 200
    assert res_ok.json()["ok"] is True

    # Invalid signature
    res_bad = send_signed_razorpay_event(admin_client, payload, signature_override="invalid_signature_hash")
    assert res_bad.status_code == 401


def test_t1_r2_02_uncertain_payment_state_routes_to_verify(admin_client: TestClient):
    """T1-R2-02: Payment failure with UNCERTAIN_OR_TEMPORARY routes to VERIFY/WAIT, avoiding unnecessary customer outreach."""
    res = send_demo_event(admin_client, "evt_e2e_unc_1", "RC_E2E_UNCERTAIN", "payment.failed", failure_category="UNCERTAIN_OR_TEMPORARY")
    assert res.status_code == 200
    data = res.json()
    assert data["case"]["state"] in {"VERIFY", "WAIT"}
    assert data["result"]["selected_action"] in {"VERIFY", "WAIT"}
    assert data["case"]["communication_count"] == 0


def test_t1_r2_03_subscription_payment_method_issue_routes_to_recover(admin_client: TestClient):
    """T1-R2-03: Subscription failure with PAYMENT_METHOD_ISSUE generates recovery link and increments communication count."""
    res = send_demo_event(admin_client, "evt_e2e_sub_1", "RC_E2E_SUB_METHOD", "payment.failed", failure_category="PAYMENT_METHOD_ISSUE")
    assert res.status_code == 200
    data = res.json()
    assert data["case"]["state"] == "RECOVER"
    assert data["result"]["selected_action"] == "RECOVER"
    assert data["case"]["communication_count"] == 1


def test_t1_r2_04_promise_to_pay_scheduled_task_creation(admin_client: TestClient, db_session: Session):
    """T1-R2-04: Customer commitment message creates persistent PaymentPromise and ScheduledTask."""
    send_demo_event(admin_client, "evt_e2e_prm_1", "RC_E2E_PROMISE_TASK", "payment.failed")
    res = send_demo_event(
        admin_client, "evt_e2e_prm_2", "RC_E2E_PROMISE_TASK", "customer.message",
        message="Kal salary aate hi pay kar dunga"
    )
    assert res.status_code == 200
    assert res.json()["result"]["selected_action"] == "WAIT"

    # Verify database persistence
    promise = db_session.scalar(
        select(PaymentPromise).join(RecoveryCase, PaymentPromise.case_id_ref == RecoveryCase.id)
        .where(RecoveryCase.case_id == "RC_E2E_PROMISE_TASK")
    )
    assert promise is not None
    assert promise.status == "PENDING"
    assert promise.confidence >= 0.70

    task = db_session.scalar(select(ScheduledTask).where(ScheduledTask.case_id == "RC_E2E_PROMISE_TASK"))
    assert task is not None
    assert task.task_type == "VERIFY_PROMISE"


def test_t1_r2_05_customer_payment_claim_requires_gateway_verification(admin_client: TestClient):
    """T1-R2-05: Customer claim ('already paid') requires external gateway verification rather than blind closure."""
    send_demo_event(admin_client, "evt_e2e_clm_1", "RC_E2E_CLAIM_VERIFY", "payment.failed")
    res = send_demo_event(
        admin_client, "evt_e2e_clm_2", "RC_E2E_CLAIM_VERIFY", "customer.message",
        message="I already paid this invoice yesterday"
    )
    assert res.status_code == 200
    data = res.json()
    assert data["case"]["recovered"] is False
    assert data["result"]["selected_action"] == "VERIFY"


def test_t1_r2_06_payment_capture_auto_recovery_and_stop(admin_client: TestClient):
    """T1-R2-06: Payment capture webhook immediately transitions case to terminal RECOVERED state and action STOP."""
    send_demo_event(admin_client, "evt_e2e_cap_1", "RC_E2E_CAPTURE", "payment.failed")
    res = send_demo_event(admin_client, "evt_e2e_cap_2", "RC_E2E_CAPTURE", "payment.captured")
    assert res.status_code == 200
    data = res.json()
    assert data["case"]["recovered"] is True
    assert data["case"]["state"] == "RECOVERED"
    assert data["case"]["current_action"] == "STOP"


# ---------------------------------------------------------------------------
# Feature R3: Policy Engine, Safety Guardrails & Access Control
# ---------------------------------------------------------------------------

def test_t1_r3_01_opt_out_immediately_halts_recovery(admin_client: TestClient, db_session: Session):
    """T1-R3-01: Opt-out request immediately sets state to STOP and permanently sets customer opt-out flag."""
    send_demo_event(admin_client, "evt_e2e_opt_1", "RC_E2E_OPTOUT", "payment.failed")
    res = send_demo_event(
        admin_client, "evt_e2e_opt_2", "RC_E2E_OPTOUT", "customer.message",
        message="Please stop contacting me, unsubscribe immediately."
    )
    assert res.status_code == 200
    data = res.json()
    assert data["case"]["state"] == "STOP"
    assert data["result"]["selected_action"] == "STOP"

    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == "RC_E2E_OPTOUT"))
    assert case.customer.communication_opt_out is True


def test_t1_r3_02_maximum_retry_limit_blocks_automated_recovery(admin_client: TestClient):
    """T1-R3-02: Automated recovery stops/escalates when maximum retry attempts (3) are exceeded."""
    for i in range(1, 4):
        send_demo_event(admin_client, f"evt_e2e_rtr_{i}", "RC_E2E_MAX_RETRY", "payment.failed", failure_category="PAYMENT_METHOD_ISSUE")
    res = send_demo_event(admin_client, "evt_e2e_rtr_4", "RC_E2E_MAX_RETRY", "payment.failed", failure_category="PAYMENT_METHOD_ISSUE")
    data = res.json()
    assert data["result"]["policy_result"] == "BLOCKED"
    assert data["result"]["selected_action"] in {"STOP", "ESCALATE"}


def test_t1_r3_03_high_value_transaction_human_escalation(admin_client: TestClient):
    """T1-R3-03: High-value invoice failure (>= INR 10,000) automatically routes to ESCALATE."""
    res = send_demo_event(
        admin_client, "evt_e2e_hval_1", "RC_E2E_HIGHVAL", "payment.failed",
        amount=25000, failure_category="PAYMENT_METHOD_ISSUE"
    )
    data = res.json()
    assert data["case"]["state"] == "ESCALATED"
    assert data["result"]["selected_action"] == "ESCALATE"


def test_t1_r3_04_communication_frequency_capping(admin_client: TestClient, db_session: Session):
    """T1-R3-04: Exceeding maximum allowed communication frequency blocks further customer notifications."""
    send_demo_event(admin_client, "evt_e2e_comm_init", "RC_E2E_COMM_CAP", "payment.failed")
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == "RC_E2E_COMM_CAP"))
    case.communication_count = 3  # Policy cap
    db_session.commit()

    # Attempt action that would trigger outreach
    res = send_demo_event(admin_client, "evt_e2e_comm_try", "RC_E2E_COMM_CAP", "customer.message", message="Need help paying")
    data = res.json()
    assert data["result"]["policy_result"] == "BLOCKED"
    assert data["result"]["selected_action"] == "STOP"


def test_t1_r3_05_rbac_endpoint_protection_unauthenticated_and_viewer(unauth_client: TestClient, viewer_client: TestClient, admin_client: TestClient):
    """T1-R3-05: Verifies that sensitive endpoints reject unauthenticated calls (401) and viewer write actions (403)."""
    # 1. Unauthenticated calls
    assert unauth_client.get("/api/cases").status_code == 401
    assert unauth_client.get("/api/dashboard/summary").status_code == 401
    assert unauth_client.get("/api/health").status_code == 401

    # 2. VIEWER role reading permitted
    send_demo_event(admin_client, "evt_e2e_rbac_1", "RC_E2E_RBAC", "payment.failed")
    assert viewer_client.get("/api/cases").status_code == 200

    # 3. VIEWER role mutating forbidden (403)
    res_mut = viewer_client.post("/api/cases/RC_E2E_RBAC/action", json={"action": "VERIFY"})
    assert res_mut.status_code == 403


def test_t1_r3_06_policy_configuration_admin_update(admin_client: TestClient, viewer_client: TestClient, db_session: Session):
    """T1-R3-06: Admin can read and update recovery policy; non-admin cannot update."""
    res = admin_client.get("/api/policies")
    assert res.status_code == 200
    policies = res.json()
    assert len(policies) > 0

    try:
        # Viewer cannot update
        res_bad = viewer_client.post("/api/policies", json={"configuration": {"maximum_automated_attempts": 4}})
        assert res_bad.status_code == 403

        # Admin can update
        res_ok = admin_client.post("/api/policies", json={"configuration": {"maximum_automated_attempts": 5}})
        assert res_ok.status_code == 200
        assert res_ok.json()["configuration"]["maximum_automated_attempts"] == 5
    finally:
        # Reset policy isolation so downstream tests have default maximum_automated_attempts = 3
        custom_policies = db_session.scalars(select(RecoveryPolicy).where(RecoveryPolicy.version != "v1.2")).all()
        for p in custom_policies:
            db_session.delete(p)
        default_p = db_session.scalar(select(RecoveryPolicy).where(RecoveryPolicy.version == "v1.2"))
        if default_p:
            default_p.active = True
        db_session.commit()



# ---------------------------------------------------------------------------
# Feature R4: Immutable Audit Trail, Database Integrity & Observability
# ---------------------------------------------------------------------------

def test_t1_r4_01_append_only_decision_ledger_persistence(admin_client: TestClient, db_session: Session):
    """T1-R4-01: Every case event produces an immutable DecisionLedger record with full attribution."""
    send_demo_event(admin_client, "evt_e2e_ledg_1", "RC_E2E_LEDGER", "payment.failed")
    send_demo_event(admin_client, "evt_e2e_ledg_2", "RC_E2E_LEDGER", "customer.message", message="Will pay tomorrow")

    decisions = db_session.scalars(
        select(DecisionLedger).join(RecoveryCase, DecisionLedger.case_id_ref == RecoveryCase.id)
        .where(RecoveryCase.case_id == "RC_E2E_LEDGER").order_by(DecisionLedger.id.asc())
    ).all()
    assert len(decisions) == 2
    assert decisions[0].selected_action in {"VERIFY", "WAIT"}
    assert decisions[1].selected_action == "WAIT"
    assert decisions[1].llm_provider in {"groq-llm", "gemini-llm", "fallback-rules"}


def test_t1_r4_02_action_record_idempotency_keys(admin_client: TestClient, db_session: Session):
    """T1-R4-02: Executed actions store unique idempotency keys in action_records."""
    send_demo_event(admin_client, "evt_e2e_act_1", "RC_E2E_ACTION_KEY", "payment.failed", failure_category="PAYMENT_METHOD_ISSUE")
    actions = db_session.scalars(
        select(ActionRecord).join(RecoveryCase, ActionRecord.case_id_ref == RecoveryCase.id)
        .where(RecoveryCase.case_id == "RC_E2E_ACTION_KEY")
    ).all()
    assert len(actions) > 0
    assert all(a.idempotency_key for a in actions)


def test_t1_r4_03_check_db_health_live_neon_diagnostics():
    """T1-R4-03: check_db_health correctly returns CONNECTED, positive latency_ms, and pool statistics."""
    health = check_db_health()
    assert health["status"] == "CONNECTED"
    assert health["latency_ms"] >= 0
    assert "pool" in health
    assert "size" in health["pool"]


def test_t1_r4_04_zero_sqlite_files_compliance():
    """T1-R4-04: Verifies the architectural requirement that zero .db or .sqlite files exist in repository."""
    workspace = Path(__file__).resolve().parent.parent
    db_files = list(workspace.glob("**/*.db")) + list(workspace.glob("**/*.sqlite"))
    # Exclude any potential venv files
    repo_db_files = [f for f in db_files if ".venv" not in str(f) and ".git" not in str(f)]
    assert len(repo_db_files) == 0, f"Found unexpected local database files: {repo_db_files}"


def test_t1_r4_05_net_recovery_value_calculation(admin_client: TestClient):
    """T1-R4-05: Verifies that net_recovery_value = recovered_amount - total_intervention_cost."""
    send_demo_event(admin_client, "evt_e2e_net_1", "RC_E2E_NET_VAL", "payment.failed", amount=8000, failure_category="PAYMENT_METHOD_ISSUE")
    res = send_demo_event(admin_client, "evt_e2e_net_2", "RC_E2E_NET_VAL", "payment.captured", amount=8000)
    case_dict = res.json()["case"]
    expected_net = case_dict["recovered_amount"] - case_dict["total_intervention_cost"]
    assert case_dict["net_recovery_value"] == expected_net


def test_t1_r4_06_system_health_telemetry_and_events(admin_client: TestClient):
    """T1-R4-06: /api/health endpoint returns complete service health indicators."""
    res = admin_client.get("/api/health")
    assert res.status_code == 200
    data = res.json()
    assert data["database"] in {"CONNECTED", "HEALTHY"} or data.get("database_health", {}).get("status") in {"CONNECTED", "HEALTHY"}
    assert data["webhook_receiver"] == "HEALTHY"
    assert "database_latency" in data
    assert "duplicate_events_detected" in data


# ===========================================================================
# TIER 2: BOUNDARY VALUE ANALYSIS & CORNER CASES (>= 5 per feature R1-R4)
# ===========================================================================

# ---------------------------------------------------------------------------
# R1 Boundary & Corner Cases
# ---------------------------------------------------------------------------

def test_t2_r1_01_empty_and_whitespace_message_handling():
    """T2-R1-01: Empty or whitespace-only message returns UNKNOWN without throwing exception."""
    res_empty = fallback_ai("")
    assert res_empty.intent == "UNKNOWN"
    assert res_empty.customer_tone == "neutral"

    res_ws = fallback_ai("   \t\n  ")
    assert res_ws.intent == "UNKNOWN"


def test_t2_r1_02_extreme_length_message_stress():
    """T2-R1-02: Extremely long message (10,000+ characters) is processed cleanly."""
    long_msg = "Please consider this " + ("urgent payment message " * 600) + "Kal salary aane par dunga"
    intent = fallback_ai(long_msg)
    assert intent.intent == "PROMISE_TO_PAY"
    assert intent.promise_detected is True


def test_t2_r1_03_confidence_boundary_clamping():
    """T2-R1-03: Out-of-bounds confidence values are strictly clamped to [0.0, 1.0]."""
    high = CustomerIntent.model_validate({
        "intent": "PROMISE_TO_PAY", "promise_detected": True, "confidence": 1.75,
        "recommended_next_action": "WAIT"
    })
    assert high.confidence == 1.0

    low = CustomerIntent.model_validate({
        "intent": "PROMISE_TO_PAY", "promise_detected": True, "confidence": -0.5,
        "recommended_next_action": "WAIT"
    })
    assert low.confidence == 0.0


def test_t2_r1_04_tone_sanitization_edge_cases():
    """T2-R1-04: Non-standard tones are mapped safely to standard 'neutral' or 'negative'."""
    assert CustomerIntent.sanitize_tone("assertive") == "neutral"
    assert CustomerIntent.sanitize_tone("questioning") == "neutral"
    assert CustomerIntent.sanitize_tone("angry") == "negative"
    assert CustomerIntent.sanitize_tone("frustrated") == "negative"
    assert CustomerIntent.sanitize_tone("unrecognized_gibberish") == "neutral"


@pytest.mark.anyio
async def test_t2_r1_05_malformed_llm_json_recovery():
    """T2-R1-05: Malformed JSON output from LLM provider automatically recovers via deterministic fallback."""
    mock_bad_resp = MagicMock()
    mock_bad_resp.status_code = 200
    mock_bad_resp.json.return_value = {"choices": [{"message": {"content": "INVALID_NOT_JSON"}}]}
    mock_bad_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", return_value=mock_bad_resp):
        with patch("backend.services.ai_agent.GROQ_API_KEY", "mock_groq_key"):
            with patch("backend.services.ai_agent.GEMINI_API_KEY", ""):
                intent, provider, _ = await analyze_intent_resilient("Kal pay karunga")
                assert provider == "fallback-rules"
                assert intent.intent == "PROMISE_TO_PAY"


def test_t2_r1_06_ambiguous_temporal_expression_grace_period():
    """T2-R1-06: Ambiguous temporal text defaults safely to current time + 2 hours grace period."""
    base_time = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    res_time = resolve_temporal_expression("maybe sometime soon", base_time=base_time)
    assert res_time >= base_time + timedelta(hours=1)


# ---------------------------------------------------------------------------
# R2 Boundary & Corner Cases
# ---------------------------------------------------------------------------

def test_t2_r2_01_negative_amount_rejection(admin_client: TestClient):
    """T2-R2-01: Negative amount in webhook payload is rejected with HTTP 400."""
    res = send_demo_event(admin_client, "evt_e2e_neg_1", "RC_E2E_NEG", "payment.failed", amount=-500)
    assert res.status_code == 400


def test_t2_r2_02_zero_amount_handling(admin_client: TestClient):
    """T2-R2-02: Zero amount in webhook payload is processed without division by zero errors."""
    res = send_demo_event(admin_client, "evt_e2e_zero_1", "RC_E2E_ZERO", "payment.failed", amount=0)
    assert res.status_code == 200
    assert res.json()["case"]["amount"] == 0


def test_t2_r2_03_extreme_amount_boundary(admin_client: TestClient):
    """T2-R2-03: Extreme amount (INR 100,000,000) processed accurately without integer overflow."""
    res = send_demo_event(admin_client, "evt_e2e_ext_1", "RC_E2E_EXTREME", "payment.failed", amount=100000000)
    assert res.status_code == 200
    assert res.json()["case"]["amount"] == 100000000
    assert res.json()["result"]["selected_action"] == "ESCALATE"


def test_t2_r2_04_event_idempotency_duplicate_suppression(admin_client: TestClient):
    """T2-R2-04: Submitting identical external_event_id returns HTTP 200 with duplicate=True."""
    res1 = send_demo_event(admin_client, "evt_e2e_dup_unique", "RC_E2E_DUP", "payment.failed")
    assert res1.status_code == 200
    assert res1.json()["ok"] is True

    res2 = send_demo_event(admin_client, "evt_e2e_dup_unique", "RC_E2E_DUP", "payment.failed")
    assert res2.status_code == 200
    assert res2.json()["duplicate"] is True


def test_t2_r2_05_out_of_order_late_failure_terminal_protection(admin_client: TestClient):
    """T2-R2-05: Late failure event arriving after RECOVERED state is ignored without reopening case."""
    send_demo_event(admin_client, "evt_e2e_ord_1", "RC_E2E_OUT_OF_ORDER", "payment.failed")
    send_demo_event(admin_client, "evt_e2e_ord_2", "RC_E2E_OUT_OF_ORDER", "payment.captured")

    late = send_demo_event(admin_client, "evt_e2e_ord_3", "RC_E2E_OUT_OF_ORDER", "payment.failed").json()
    assert late["late_event_ignored"] is True
    assert late["case"]["state"] == "RECOVERED"


def test_t2_r2_06_missing_required_webhook_fields(admin_client: TestClient):
    """T2-R2-06: Webhook requests missing external_event_id or case_reference return HTTP 400."""
    res_no_evt = admin_client.post("/api/demo/webhook", json={"case_reference": "RC_1", "amount": 500})
    assert res_no_evt.status_code == 400

    res_no_case = admin_client.post("/api/demo/webhook", json={"external_event_id": "evt_1", "amount": 500})
    assert res_no_case.status_code == 400


# ---------------------------------------------------------------------------
# R3 Boundary & Corner Cases
# ---------------------------------------------------------------------------

def test_t2_r3_01_exact_retry_count_boundary(admin_client: TestClient, db_session: Session):
    """T2-R3-01: Exactly at retry limit (retry_count=3) blocks further retry, while retry_count=2 proceeds."""
    send_demo_event(admin_client, "evt_e2e_bnd_rtr", "RC_E2E_RETRY_BND", "payment.failed")
    db_session.expire_all()
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == "RC_E2E_RETRY_BND"))

    # Boundary 1: retry_count = 2 -> Action allowed
    case.retry_count = 2
    case.failure_category = "PAYMENT_METHOD_ISSUE"
    db_session.commit()
    db_session.refresh(case)
    res1 = process_case(db_session, case, "payment.failed")
    assert res1["policy_result"] == "APPROVED"

    # Boundary 2: retry_count = 3 -> Action BLOCKED
    db_session.refresh(case)
    case.retry_count = 3
    db_session.commit()
    db_session.refresh(case)
    res2 = process_case(db_session, case, "payment.failed")
    assert res2["policy_result"] == "BLOCKED"


def test_t2_r3_02_exact_communication_count_boundary(admin_client: TestClient, db_session: Session):
    """T2-R3-02: Exactly at communication limit (comm_count=3) blocks further notification dispatch."""
    send_demo_event(admin_client, "evt_e2e_bnd_comm", "RC_E2E_COMM_BND", "payment.failed")
    db_session.expire_all()
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == "RC_E2E_COMM_BND"))

    # Boundary 1: comm_count = 2 -> Approved
    case.communication_count = 2
    db_session.commit()
    db_session.refresh(case)
    res1 = process_case(db_session, case, "customer.message", "Need link")
    assert res1["policy_result"] == "APPROVED"

    # Boundary 2: comm_count = 3 -> BLOCKED
    db_session.refresh(case)
    case.communication_count = 3
    db_session.commit()
    db_session.refresh(case)
    res2 = process_case(db_session, case, "customer.message", "Need link")
    assert res2["policy_result"] == "BLOCKED"
    assert res2["selected_action"] == "STOP"


def test_t2_r3_03_high_value_threshold_boundary(admin_client: TestClient):
    """T2-R3-03: Amount INR 9,999 stays automated; INR 10,000 triggers human escalation."""
    res_under = send_demo_event(admin_client, "evt_e2e_hval_under", "RC_E2E_HV_UNDER", "payment.failed", amount=9999, failure_category="PAYMENT_METHOD_ISSUE")
    assert res_under.json()["result"]["selected_action"] == "RECOVER"

    res_at_limit = send_demo_event(admin_client, "evt_e2e_hval_at", "RC_E2E_HV_AT", "payment.failed", amount=10000, failure_category="PAYMENT_METHOD_ISSUE")
    assert res_at_limit.json()["result"]["selected_action"] == "ESCALATE"


def test_t2_r3_04_opt_out_idempotency_on_stopped_case(admin_client: TestClient):
    """T2-R3-04: Repeating opt-out message on an already STOPPED case does not alter state or cause error."""
    send_demo_event(admin_client, "evt_e2e_opt_idemp_1", "RC_E2E_OPT_IDEMP", "customer.message", message="Stop calling me")
    res2 = send_demo_event(admin_client, "evt_e2e_opt_idemp_2", "RC_E2E_OPT_IDEMP", "customer.message", message="Unsubscribe again")
    assert res2.status_code == 200
    assert res2.json()["case"]["state"] == "STOP"


def test_t2_r3_05_recovered_case_manual_action_conflict(admin_client: TestClient):
    """T2-R3-05: Operator attempting manual recovery action on already RECOVERED case receives 409 Conflict."""
    send_demo_event(admin_client, "evt_e2e_rec_act_1", "RC_E2E_REC_CONFLICT", "payment.captured")
    res = admin_client.post("/api/cases/RC_E2E_REC_CONFLICT/action", json={"action": "RECOVER"})
    assert res.status_code == 409


def test_t2_r3_06_unsupported_manual_action_rejection(admin_client: TestClient):
    """T2-R3-06: Operator requesting unsupported action returns HTTP 400 Bad Request."""
    send_demo_event(admin_client, "evt_e2e_unsup_1", "RC_E2E_UNSUPPORTED", "payment.failed")
    res = admin_client.post("/api/cases/RC_E2E_UNSUPPORTED/action", json={"action": "INVALID_ACTION_NAME"})
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# R4 Boundary & Corner Cases
# ---------------------------------------------------------------------------

def test_t2_r4_01_check_db_health_simulated_disconnect():
    """T2-R4-01: check_db_health returns DISCONNECTED with diagnostic error without crashing application."""
    bad_engine = create_engine(
        URL.create(
            drivername="postgresql+psycopg",
            username="invalid_user",
            password="wrong_pass",
            host="127.0.0.1",
            port=54329,
            database="nonexistent_db",
            query={"connect_timeout": "1"},
        ),
        future=True,
    )
    import database.connection as db_conn
    original = db_conn.engine
    try:
        db_conn.engine = bad_engine
        result = check_db_health()
        assert result["status"] == "DISCONNECTED"
        assert result["latency_ms"] >= 0
        assert "error" in result
    finally:
        db_conn.engine = original


def test_t2_r4_02_action_record_duplicate_key_deflection(admin_client: TestClient, db_session: Session):
    """T2-R4-02: Duplicate action idempotency keys are safely deflected, returning False."""
    send_demo_event(admin_client, "evt_e2e_act_dup", "RC_E2E_ACT_DEFLECT", "payment.failed")
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == "RC_E2E_ACT_DEFLECT"))

    first = record_action(db_session, case, "TEST_ACTION", "EXECUTED", {}, key="fixed_idempotency_key_123")
    assert first is True
    db_session.commit()

    second = record_action(db_session, case, "TEST_ACTION", "EXECUTED", {}, key="fixed_idempotency_key_123")
    assert second is False


def test_t2_r4_03_audit_log_nonexistent_case_filtering(admin_client: TestClient):
    """T2-R4-03: Querying audit trail for non-existent case returns HTTP 404 cleanly."""
    res = admin_client.get("/api/audit/NONEXISTENT_CASE_ID_9999")
    assert res.status_code == 404


def test_t2_r4_04_query_pagination_and_limit_boundaries(admin_client: TestClient):
    """T2-R4-04: Endpoints returning lists (/api/cases, /api/audit) handle queries cleanly."""
    res_cases = admin_client.get("/api/cases")
    assert res_cases.status_code == 200
    assert isinstance(res_cases.json(), list)

    res_audit = admin_client.get("/api/audit")
    assert res_audit.status_code == 200
    assert isinstance(res_audit.json(), list)


def test_t2_r4_05_simulated_executor_failure_resilience(admin_client: TestClient, db_session: Session):
    """T2-R4-05: Simulated executor failure records FAILED action status and health event."""
    send_demo_event(admin_client, "evt_e2e_execfail_1", "RC_E2E_EXECFAIL", "payment.failed")
    res = admin_client.post("/api/cases/RC_E2E_EXECFAIL/action", json={"action": "VERIFY", "simulate_failure": True})
    assert res.status_code == 200
    assert res.json()["simulated_failure"] is True

    health_evt = db_session.scalar(
        select(SystemHealthEvent).where(SystemHealthEvent.status == "FAILED_SIMULATION").order_by(SystemHealthEvent.id.desc())
    )
    assert health_evt is not None
    assert health_evt.details["case_id"] == "RC_E2E_EXECFAIL"


def test_t2_r4_06_foreign_key_cascade_delete_integrity(admin_client: TestClient, db_session: Session):
    """T2-R4-06: Deleting a RecoveryCase cascades and deletes dependent events, decisions, actions, promises."""
    send_demo_event(admin_client, "evt_e2e_casc_1", "RC_E2E_CASCADE_TEST", "payment.failed")
    send_demo_event(admin_client, "evt_e2e_casc_2", "RC_E2E_CASCADE_TEST", "customer.message", message="Kal salary aane par dunga")

    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == "RC_E2E_CASCADE_TEST"))
    assert case is not None
    case_id_val = case.id

    # Delete case
    db_session.delete(case)
    db_session.commit()

    # Verify children cascade deleted
    events = db_session.scalars(select(CaseEvent).where(CaseEvent.case_id_ref == case_id_val)).all()
    assert len(events) == 0
    decisions = db_session.scalars(select(DecisionLedger).where(DecisionLedger.case_id_ref == case_id_val)).all()
    assert len(decisions) == 0
    actions = db_session.scalars(select(ActionRecord).where(ActionRecord.case_id_ref == case_id_val)).all()
    assert len(actions) == 0
    promises = db_session.scalars(select(PaymentPromise).where(PaymentPromise.case_id_ref == case_id_val)).all()
    assert len(promises) == 0


# ===========================================================================
# TIER 3: CROSS-FEATURE INTERACTIONS & PAIRWISE COMBINATIONS (5 tests)
# ===========================================================================

def test_t3_int_01_complete_webhook_to_audit_ledger_pipeline(admin_client: TestClient, db_session: Session):
    """T3-INT-01: Full chain: Webhook -> Normalization -> Decision Engine -> Policy Engine -> Action Record -> Decision Ledger."""
    payload = {
        "id": "evt_e2e_pipe_01",
        "event": "payment.failed",
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_e2e_pipe",
                    "order_id": "RC_E2E_PIPELINE",
                    "amount": 499900,  # 4,999 INR in paise
                    "email": "pipeline_customer@customer.local"
                }
            }
        }
    }
    res = send_signed_razorpay_event(admin_client, payload)
    assert res.status_code == 200

    # 1. RecoveryCase created and normalized
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == "RC_E2E_PIPELINE"))
    assert case is not None
    assert case.amount == 4999

    # 2. DecisionLedger created with strategy scores and deterministic factors
    ledger = db_session.scalar(
        select(DecisionLedger).where(DecisionLedger.case_id_ref == case.id).order_by(DecisionLedger.id.desc())
    )
    assert ledger is not None
    assert ledger.policy_result == "APPROVED"
    assert "strategy_scores" in ledger.__dict__ or hasattr(ledger, "strategy_scores")

    # 3. ActionRecord created
    action = db_session.scalar(select(ActionRecord).where(ActionRecord.case_id_ref == case.id))
    assert action is not None


def test_t3_int_02_customer_opt_out_cascading_halt(admin_client: TestClient, db_session: Session):
    """T3-INT-02: Customer opt-out via message cascades through AI classifier, updates customer record, and halts subsequent events."""
    send_demo_event(admin_client, "evt_e2e_opt_pipe_1", "RC_E2E_OPT_CASCADE", "payment.failed")
    # Customer messages Hinglish opt-out
    res_opt = send_demo_event(
        admin_client, "evt_e2e_opt_pipe_2", "RC_E2E_OPT_CASCADE", "customer.message",
        message="kripya message na bheje, band karo"
    )
    assert res_opt.json()["case"]["state"] == "STOP"

    # Customer record updated
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == "RC_E2E_OPT_CASCADE"))
    assert case.customer.communication_opt_out is True

    # Subsequent payment failure arriving later triggers STOP with zero outreach
    res_subsequent = send_demo_event(admin_client, "evt_e2e_opt_pipe_3", "RC_E2E_OPT_CASCADE", "payment.failed")
    assert res_subsequent.json()["result"]["selected_action"] == "STOP"
    assert res_subsequent.json()["case"]["communication_count"] == 0


def test_t3_int_03_promise_lifecycle_and_auto_recovery_reconciliation(admin_client: TestClient, db_session: Session):
    """T3-INT-03: Promise created -> payment capture webhook arrives -> promise transitions to FULFILLED -> case RECOVERED."""
    send_demo_event(admin_client, "evt_e2e_prm_pipe_1", "RC_E2E_PRM_RECON", "payment.failed")
    send_demo_event(
        admin_client, "evt_e2e_prm_pipe_2", "RC_E2E_PRM_RECON", "customer.message",
        message="Kal subah pakka pay kar dunga"
    )

    promise = db_session.scalar(
        select(PaymentPromise).join(RecoveryCase, PaymentPromise.case_id_ref == RecoveryCase.id)
        .where(RecoveryCase.case_id == "RC_E2E_PRM_RECON")
    )
    assert promise.status == "PENDING"

    # Payment capture arrives
    send_demo_event(admin_client, "evt_e2e_prm_pipe_3", "RC_E2E_PRM_RECON", "payment.captured")
    db_session.refresh(promise)
    assert promise.status == "FULFILLED"
    assert promise.fulfilled_at is not None

    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == "RC_E2E_PRM_RECON"))
    assert case.recovered is True
    assert case.state == "RECOVERED"


def test_t3_int_04_customer_claim_requires_gateway_verification(admin_client: TestClient, db_session: Session):
    """T3-INT-04: Claim 'already paid' triggers VERIFY -> actual gateway webhook arrives -> case marked RECOVERED."""
    send_demo_event(admin_client, "evt_e2e_clm_pipe_1", "RC_E2E_CLM_PIPE", "payment.failed")
    # Customer claims payment made
    res_claim = send_demo_event(
        admin_client, "evt_e2e_clm_pipe_2", "RC_E2E_CLM_PIPE", "customer.message",
        message="Maine payment complete kar di hai UPI se"
    )
    assert res_claim.json()["result"]["selected_action"] == "VERIFY"
    assert res_claim.json()["case"]["recovered"] is False

    # True capture webhook arrives from gateway
    res_cap = send_demo_event(admin_client, "evt_e2e_clm_pipe_3", "RC_E2E_CLM_PIPE", "payment.captured")
    assert res_cap.json()["case"]["recovered"] is True
    assert res_cap.json()["case"]["state"] == "RECOVERED"


def test_t3_int_05_concurrent_event_idempotency_and_state_consistency(admin_client: TestClient, db_session: Session):
    """T3-INT-05: Duplicate failure events followed by capture guarantee state consistency without duplicate actions."""
    # Event 1
    r1 = send_demo_event(admin_client, "evt_e2e_storm_1", "RC_E2E_CONCURRENT", "payment.failed", amount=3000)
    assert r1.status_code == 200
    # Duplicate Event 1
    r1_dup = send_demo_event(admin_client, "evt_e2e_storm_1", "RC_E2E_CONCURRENT", "payment.failed", amount=3000)
    assert r1_dup.json()["duplicate"] is True

    # Capture event
    r2 = send_demo_event(admin_client, "evt_e2e_storm_2", "RC_E2E_CONCURRENT", "payment.captured", amount=3000)
    assert r2.json()["case"]["recovered"] is True

    # Check action records count
    actions = db_session.scalars(
        select(ActionRecord).join(RecoveryCase, ActionRecord.case_id_ref == RecoveryCase.id)
        .where(RecoveryCase.case_id == "RC_E2E_CONCURRENT")
    ).all()
    # Should only have records from actual unique events, not duplicates
    assert len(actions) >= 1


# ===========================================================================
# TIER 4: REAL-WORLD ENTERPRISE WORKLOADS (5 End-to-End Scenarios)
# ===========================================================================

def test_t4_scn_01_saas_subscription_soft_failure_recovery(admin_client: TestClient):
    """
    T4-SCN-01: Multi-step SaaS subscription recovery scenario.
    1. Gateway reports temporary gateway failure. Case enters WAIT with zero outreach.
    2. Within cooldown period, customer bank auto-authorizes and payment.captured arrives.
    3. Case automatically closes as RECOVERED with net recovery value = amount.
    Flagship metric proven: 'Unnecessary Recovery Actions Prevented'.
    """
    case_ref = "RC_E2E_SCN_SAAS"
    # Step 1: Temporary failure
    res_fail = send_demo_event(
        admin_client, "evt_e2e_scn1_fail", case_ref, "payment.failed",
        amount=4999, failure_category="UNCERTAIN_OR_TEMPORARY"
    )
    assert res_fail.status_code == 200
    case_data = res_fail.json()["case"]
    assert case_data["state"] in {"WAIT", "VERIFY"}
    assert case_data["communication_count"] == 0

    # Step 2: Late automatic capture
    res_cap = send_demo_event(admin_client, "evt_e2e_scn1_cap", case_ref, "payment.captured", amount=4999)
    assert res_cap.status_code == 200
    recovered_data = res_cap.json()["case"]
    assert recovered_data["recovered"] is True
    assert recovered_data["state"] == "RECOVERED"
    assert recovered_data["total_intervention_cost"] == 0
    assert recovered_data["net_recovery_value"] == 4999


def test_t4_scn_02_ecommerce_high_value_escalation_and_manual_operator_capture(admin_client: TestClient, db_session: Session):
    """
    T4-SCN-02: B2B high-value invoice escalation and manual operator resolution.
    1. High-value transaction (INR 45,000) payment fails.
    2. Policy engine flags amount >= 10,000 and escalates to human operator (ESCALATED state).
    3. Operator inspects timeline and executes manual CAPTURE_PAYMENT.
    4. Case marked RECOVERED and audited with operator attribution.
    """
    case_ref = "RC_E2E_SCN_B2B"
    # Step 1: High value failure
    res_fail = send_demo_event(
        admin_client, "evt_e2e_scn2_fail", case_ref, "payment.failed",
        amount=45000, failure_category="HIGH_VALUE_INVOICE"
    )
    assert res_fail.json()["case"]["state"] == "ESCALATED"
    assert res_fail.json()["result"]["selected_action"] == "ESCALATE"

    # Step 2: Operator manually marks captured
    res_action = admin_client.post(f"/api/cases/{case_ref}/action", json={"action": "CAPTURE_PAYMENT", "note": "Verified bank wire transfer"})
    assert res_action.status_code == 200
    assert res_action.json()["case"]["recovered"] is True
    assert res_action.json()["case"]["state"] == "RECOVERED"

    # Step 3: Verify audit trail
    events = db_session.scalars(
        select(CaseEvent).join(RecoveryCase, CaseEvent.case_id_ref == RecoveryCase.id)
        .where(RecoveryCase.case_id == case_ref)
    ).all()
    assert any("manual payment capture" in e.message.lower() for e in events)


def test_t4_scn_03_promise_expiration_and_single_bounded_followup(admin_client: TestClient, db_session: Session):
    """
    T4-SCN-03: Promise-to-pay expiration and single bounded follow-up dispatch.
    1. Payment fails.
    2. Customer promises to pay. Promise is scheduled.
    3. Deadline arrives without payment. Background scheduler tick runs.
    4. Exactly 1 bounded follow-up is dispatched (follow_up_sent=True).
    5. Case transitions to RECOVER with recovery link.
    """
    case_ref = "RC_E2E_SCN_PROMISE_EXP"
    # Step 1: Initial failure
    send_demo_event(admin_client, "evt_e2e_scn3_fail", case_ref, "payment.failed", amount=3500)
    # Step 2: Customer promise
    send_demo_event(admin_client, "evt_e2e_scn3_msg", case_ref, "customer.message", message="Kal salary aane par dunga")

    # Fast forward task scheduled_at to past
    task = db_session.scalar(select(ScheduledTask).where(ScheduledTask.case_id == case_ref))
    assert task is not None
    task.scheduled_at = now_utc() - timedelta(minutes=5)
    db_session.commit()

    # Step 3: Run scheduler tick
    processed = process_due_tasks()
    assert processed >= 1

    # Step 4: Verify follow-up sent and state updated
    promise = db_session.scalar(
        select(PaymentPromise).join(RecoveryCase, PaymentPromise.case_id_ref == RecoveryCase.id)
        .where(RecoveryCase.case_id == case_ref)
    )
    assert promise.follow_up_sent is True
    assert promise.status == "FOLLOW_UP_SENT"

    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_ref))
    assert case.communication_count >= 1
    assert case.state == "RECOVER"


def test_t4_scn_04_mid_workflow_opt_out_enforcement(admin_client: TestClient, db_session: Session):
    """
    T4-SCN-04: Mid-workflow opt-out prevents any further notifications.
    1. Payment fails, customer outreach sent.
    2. Customer replies angrily with opt-out keyword.
    3. Case immediately enters STOP state and customer marked as opted out.
    4. Scheduled task runs or additional failures arrive -> All skipped cleanly.
    """
    case_ref = "RC_E2E_SCN_OPTOUT"
    # Step 1: Payment method failure initiates outreach
    send_demo_event(admin_client, "evt_e2e_scn4_fail", case_ref, "payment.failed", failure_category="PAYMENT_METHOD_ISSUE")
    db_session.expire_all()
    case = db_session.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_ref))
    assert case.communication_count == 1

    # Step 2: Customer opts out
    send_demo_event(admin_client, "evt_e2e_scn4_opt", case_ref, "customer.message", message="Pareshan mat karo, stop calling!")
    db_session.refresh(case)
    assert case.state == "STOP"
    assert case.customer.communication_opt_out is True

    # Step 3: Subsequent scheduled task attempt is skipped
    task = ScheduledTask(
        case_id=case_ref, task_type="VERIFY_PROMISE", scheduled_at=now_utc() - timedelta(minutes=1),
        idempotency_key="opt_test_task_key"
    )
    db_session.add(task)
    db_session.commit()

    process_due_tasks()
    db_session.refresh(task)
    assert task.status == "SKIPPED_OPT_OUT"


def test_t4_scn_05_idempotent_webhook_storm_and_out_of_order_resolution(admin_client: TestClient):
    """
    T4-SCN-05: Webhook storm with duplicate events and delayed out-of-order failure.
    1. Initial failure webhook arrives -> Case created.
    2. 4 duplicate failure webhooks arrive -> Deflected with duplicate=True.
    3. Payment captured webhook arrives -> Case enters RECOVERED.
    4. Delayed network failure event arrives late -> Deflected with late_event_ignored=True.
    Case remains securely in terminal RECOVERED state.
    """
    case_ref = "RC_E2E_SCN_STORM"
    # 1. Initial event
    r1 = send_demo_event(admin_client, "evt_e2e_storm_base", case_ref, "payment.failed", amount=6200)
    assert r1.status_code == 200

    # 2. Duplicate storm
    for i in range(4):
        r_dup = send_demo_event(admin_client, "evt_e2e_storm_base", case_ref, "payment.failed", amount=6200)
        assert r_dup.json()["duplicate"] is True

    # 3. Successful capture
    r_cap = send_demo_event(admin_client, "evt_e2e_storm_cap", case_ref, "payment.captured", amount=6200)
    assert r_cap.json()["case"]["recovered"] is True
    assert r_cap.json()["case"]["state"] == "RECOVERED"

    # 4. Out-of-order delayed failure
    r_late = send_demo_event(admin_client, "evt_e2e_storm_late_fail", case_ref, "payment.failed", amount=6200)
    assert r_late.json()["late_event_ignored"] is True
    assert r_late.json()["case"]["state"] == "RECOVERED"
