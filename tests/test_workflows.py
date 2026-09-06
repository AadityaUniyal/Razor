"""Regression tests for the recovery safety rules in the implementation plan.
All tests run against Neon PostgreSQL with zero local .db files.
"""
import os
import hmac
import hashlib

# Database credentials are supplied by the local environment or CI.
os.environ["RAZORPAY_WEBHOOK_SECRET"] = "unit-test-webhook-value"

from fastapi.testclient import TestClient
from sqlalchemy import select, delete
from database.connection import SessionLocal, Base, engine
from database.models import User, Customer, RecoveryCase, PaymentEvent, CaseEvent, DecisionLedger, ActionRecord, PaymentPromise, ScheduledTask, Notification
from database.init_db import seed_data, init_db
from backend.main import app

# Initialize schema once at module import
init_db()


def fresh_client() -> TestClient:
    with SessionLocal() as db:
        # Clean up any test records from prior runs
        test_cases = db.scalars(select(RecoveryCase).where(RecoveryCase.case_id.like("RC_TEST%"))).all()
        for tc in test_cases:
            db.execute(delete(ScheduledTask).where(ScheduledTask.case_id == tc.case_id))
            db.execute(delete(PaymentPromise).where(PaymentPromise.case_id_ref == tc.id))
            db.execute(delete(Notification).where(Notification.case_id_ref == tc.id))
            db.execute(delete(ActionRecord).where(ActionRecord.case_id_ref == tc.id))
            db.execute(delete(DecisionLedger).where(DecisionLedger.case_id_ref == tc.id))
            db.execute(delete(CaseEvent).where(CaseEvent.case_id_ref == tc.id))
            db.execute(delete(RecoveryCase).where(RecoveryCase.id == tc.id))
        db.execute(delete(PaymentEvent).where(PaymentEvent.case_reference.like("RC_TEST%")))
        db.execute(delete(Customer).where(Customer.external_customer_id.like("cust_rc_test%")))
        db.execute(delete(Customer).where(Customer.email.like("rc_test%@customer.local")))
        db.commit()
        seed_data(db)

    client = TestClient(app)
    response = client.post("/api/auth/login", data={"email": "admin@razorrescue.local", "password": "password123"})
    assert response.status_code == 200
    return client


def event(client: TestClient, event_id: str, case_id: str, event_type: str, **more):
    return client.post("/api/demo/webhook", json={"external_event_id": event_id, "case_reference": case_id, "event_type": event_type, "amount": 5000, **more})


# TEST 1: Duplicate webhook is processed once (Idempotency)
def test_duplicate_webhook_is_processed_once():
    client = fresh_client()
    assert event(client, "dup-1", "RC_TEST_DUP", "payment.failed").json()["ok"]
    duplicate = event(client, "dup-1", "RC_TEST_DUP", "payment.failed").json()
    assert duplicate["duplicate"] is True


# TEST 2: Late failure cannot reopen recovered case (Terminal State & Out-of-order)
def test_late_failure_cannot_reopen_recovered_case():
    client = fresh_client()
    event(client, "order-1", "RC_TEST_ORDER", "payment.failed")
    event(client, "order-2", "RC_TEST_ORDER", "payment.captured")
    late = event(client, "order-3", "RC_TEST_ORDER", "payment.failed").json()
    assert late["late_event_ignored"] is True
    assert late["case"]["state"] == "RECOVERED"


# TEST 3: Promise is tracked with safe fallback & Hinglish support
def test_promise_is_tracked_with_safe_fallback():
    client = fresh_client()
    response = event(client, "promise-1", "RC_TEST_PROMISE", "customer.message", message="Kal salary aane ke baad payment kar dunga.")
    assert response.status_code == 200
    assert response.json()["result"]["selected_action"] == "WAIT"
    promises = client.get("/api/promises").json()
    assert any(p["case_id"] == "RC_TEST_PROMISE" and p["status"] == "PENDING" for p in promises)


# TEST 4: Customer payment claim requires provider verification (Not direct recovery)
def test_customer_payment_claim_requires_provider_verification():
    client = fresh_client()
    response = event(client, "claim-1", "RC_TEST_CLAIM", "customer.message", message="I already paid this invoice.").json()
    assert response["case"]["recovered"] is False
    assert response["result"]["selected_action"] == "VERIFY"


# TEST 5: Opt-out stops recovery immediately
def test_opt_out_stops_recovery():
    client = fresh_client()
    response = event(client, "opt-1", "RC_TEST_OPT", "customer.message", message="Please stop, do not contact me.").json()
    assert response["case"]["state"] == "STOP"
    assert response["result"]["selected_action"] == "STOP"


# TEST 6: Payment capture during WAIT closes the case and prevents customer intervention
def test_payment_capture_during_wait_marks_recovered():
    client = fresh_client()
    event(client, "wait-1", "RC_TEST_WAIT", "payment.failed", failure_category="INSUFFICIENT_FUNDS")
    cap = event(client, "wait-2", "RC_TEST_WAIT", "payment.captured").json()
    assert cap["case"]["recovered"] is True
    assert cap["case"]["state"] == "RECOVERED"
    assert cap["case"]["current_action"] == "STOP"


# TEST 7: High-value case (> 10000 INR) is escalated to human operator by policy
def test_high_value_escalation():
    client = fresh_client()
    response = event(client, "highval-1", "RC_TEST_HIGHVAL", "payment.failed", amount=25000, failure_category="PAYMENT_METHOD_ISSUE").json()
    assert response["result"]["selected_action"] == "ESCALATE"
    assert response["case"]["state"] == "ESCALATED"


# TEST 8: Max retries exceeded blocks automated recovery
def test_max_retries_blocks_recovery():
    client = fresh_client()
    for i in range(1, 4):
        event(client, f"retry-{i}", "RC_TEST_RETRY", "payment.failed", failure_category="PAYMENT_METHOD_ISSUE")
    res = event(client, "retry-4", "RC_TEST_RETRY", "payment.failed", failure_category="PAYMENT_METHOD_ISSUE").json()
    assert res["result"]["policy_result"] == "BLOCKED"
    assert res["result"]["selected_action"] in {"STOP", "ESCALATE"}


# TEST 9: Strategy scoring and Net Recovery Value calculation
def test_strategy_scoring_and_net_value():
    client = fresh_client()
    res = event(client, "strat-1", "RC_TEST_STRAT", "payment.failed", amount=6000, failure_category="PAYMENT_METHOD_ISSUE").json()
    assert "strategy_score" in res["result"]
    assert res["result"]["strategy_score"] is not None
    cap = event(client, "strat-2", "RC_TEST_STRAT", "payment.captured", amount=6000).json()
    assert cap["case"]["net_recovery_value"] == 6000 - cap["case"]["total_intervention_cost"]


# TEST 10: Razorpay webhook HMAC signature verification
def test_razorpay_webhook_signature_verification():
    client = fresh_client()
    secret = "unit-test-webhook-value"
    payload = b'{"event":"payment.captured","payload":{"payment":{"entity":{"id":"pay_123","order_id":"RC_TEST_RZP","amount":500000}}}}'
    signature = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    res = client.post("/api/webhooks/razorpay", content=payload, headers={"X-Razorpay-Signature": signature})
    assert res.status_code == 200
    assert res.json()["ok"] is True

    res_bad = client.post("/api/webhooks/razorpay", content=payload, headers={"X-Razorpay-Signature": "invalid_sig"})
    assert res_bad.status_code == 401


# TEST 11: AI Sandbox test endpoint with Hinglish date resolution
def test_ai_sandbox_endpoint():
    client = fresh_client()
    res = client.post("/api/ai/test-intent", json={"message": "Bhai kal sham ko salary aate hi pakka pay kar dunga"}).json()
    assert res["intent"] == "PROMISE_TO_PAY"
    assert res["promise_detected"] is True
    assert res["recommended_next_action"] == "WAIT"
    assert res["resolved_promise_time"] is not None


# TEST 12: AI Evaluation metrics benchmark
def test_ai_evaluation_metrics():
    client = fresh_client()
    res = client.get("/api/ai/evaluation-metrics").json()
    assert res["accuracy_percent"] > 80.0
    assert res["total_benchmarks"] >= 8
    assert res["promise_detection_rate"] == 100.0


# TEST 13: Simulated executor failure resilience
def test_simulated_executor_failure():
    client = fresh_client()
    event(client, "fail-1", "RC_TEST_EXECFAIL", "payment.failed")
    res = client.post("/api/cases/RC_TEST_EXECFAIL/action", json={"action": "VERIFY", "simulate_failure": True})
    assert res.status_code == 200
    assert res.json()["simulated_failure"] is True
