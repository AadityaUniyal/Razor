"""Regression coverage for the approval-first revenue recovery product path."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from backend.core.config import COOKIE_NAME
from backend.core.security import create_token
from backend.main import app
from backend.services.razorpay_provider import RazorpayProvider
from database.connection import SessionLocal
from database.models import (
    ApprovalRequest,
    Customer,
    PaymentEvent,
    RecoveryCase,
    User,
)


def test_unconfigured_provider_never_claims_payment_success(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    result = RazorpayProvider().verify_payment("pay_missing_credentials")
    assert result.status == "ERROR"
    assert "not configured" in (result.error_message or "")


def test_provider_adapter_maps_captured_payment_to_matched(monkeypatch):
    class Response:
        status_code = 200
        content = b"{}"

        def json(self):
            return {"id": "pay_test", "status": "captured", "amount": 249900}

    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    monkeypatch.setattr("backend.services.razorpay_provider.httpx.get", lambda *args, **kwargs: Response())
    result = RazorpayProvider().verify_payment("pay_test")
    assert result.status == "MATCHED"
    assert result.response_status == 200


def test_recommendation_requires_approval_and_leaves_dispatch_pending():
    suffix = uuid4().hex[:10]
    case_id = f"RC_APPROVAL_TEST_{suffix}"
    external_customer_id = f"cust_approval_{suffix}"
    event_id = f"evt_approval_{suffix}"
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.role == "ADMIN").order_by(User.id))
        assert user is not None
        token = create_token(user.id)

    try:
        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, token)
            response = client.post("/api/events/ingest", json={
                "external_event_id": event_id,
                "case_reference": case_id,
                "event_type": "payment.failed",
                "amount": 2499,
                "customer_name": "Approval Test Customer",
                "customer_email": f"{suffix}@customer.local",
                "external_customer_id": external_customer_id,
                "external_subscription_id": f"sub_{suffix}",
                "subscription": {"plan_name": "Pro Monthly", "billing_period": "monthly", "payment_method": "card"},
                "invoice": {"id": f"inv_{suffix}", "amount_due": 2499, "status": "OPEN"},
            })
            assert response.status_code == 200, response.text

            response = client.post(
                f"/api/cases/{case_id}/recommendations",
                json={"message": "I need help paying this renewal", "provider": "fallback"},
            )
            assert response.status_code == 200, response.text
            approval = response.json()["approval"]
            assert approval["status"] == "PENDING_APPROVAL"
            assert approval["recommendation"]["recommended_action"] == "RECOVER"

            response = client.post(
                f"/api/approvals/{approval['id']}/approve",
                json={"message": "Please update your payment method securely."},
            )
            assert response.status_code == 200, response.text

        with SessionLocal() as db:
            case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id))
            assert case is not None
            assert case.state == "RECOVER"
            assert [notification.status for notification in case.notifications] == ["PENDING_DISPATCH"]
            assert case.subscription is not None
            assert case.subscription.plan_name == "Pro Monthly"
            assert case.invoice is not None
            assert case.invoice.amount_due == 2499
    finally:
        with SessionLocal() as db:
            case = db.scalar(select(RecoveryCase).where(RecoveryCase.case_id == case_id))
            if case:
                db.delete(case)
            db.execute(delete(PaymentEvent).where(PaymentEvent.external_event_id == event_id))
            db.execute(delete(Customer).where(Customer.external_customer_id == external_customer_id))
            db.commit()
