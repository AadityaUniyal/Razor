"""
Tests for Production Upgrade Milestones:
- Authenticated credential encryption & decryption
- Live Razorpay provider & payment link generation
- Outbound multi-channel dispatch engine & opt-out suppression
- Multi-tenant webhook routing & HMAC verification
- Customer self-service recovery portal
- Scheduler cycle execution & API endpoints
- CSV export & A/B testing wiring
"""
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import os
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, delete

from backend.main import app
from backend.core.config import COOKIE_NAME
from backend.core.security import create_token, encrypt_secret, decrypt_secret, hash_password
from backend.services.dispatch_engine import dispatch_notification
from backend.services.razorpay_provider import RazorpayProvider
from backend.services.scheduler import run_scheduler_cycle
from database.connection import SessionLocal
from database.models import (
    ActionRecord,
    CaseState,
    Customer,
    IntegrationCredential,
    Merchant,
    Notification,
    PaymentPromise,
    RecoveryCase,
    Role,
    ScheduledTask,
    User,
    now_utc,
)


@pytest.fixture(scope="module")
def client():
    return TestClient(app)


@pytest.fixture(scope="function")
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture(scope="module")
def shared_merchant():
    with SessionLocal() as session:
        merchant = session.scalar(select(Merchant).where(Merchant.slug == "test-merchant-prod"))
        if not merchant:
            merchant = Merchant(name="Test Merchant Prod", slug="test-merchant-prod")
            session.add(merchant)
            session.commit()
            session.refresh(merchant)
        merchant_id = merchant.id
        merchant_slug = merchant.slug
        merchant_name = merchant.name
    return {"id": merchant_id, "slug": merchant_slug, "name": merchant_name}


@pytest.fixture(scope="module")
def shared_admin_user(shared_merchant):
    with SessionLocal() as session:
        user = session.scalar(select(User).where(User.email == "admin_prod_test@razorrescue.local"))
        if not user:
            user = User(
                email="admin_prod_test@razorrescue.local",
                password_hash=hash_password("AdminPass123!"),
                role=Role.ADMIN.value,
                merchant_id=shared_merchant["id"],
            )
            session.add(user)
            session.commit()
            session.refresh(user)
        user_id = user.id
    return {"id": user_id, "email": "admin_prod_test@razorrescue.local"}


@pytest.fixture(scope="module")
def admin_auth_headers(shared_admin_user):
    token = create_token(shared_admin_user["id"])
    return {
        "Authorization": f"Bearer {token}",
        "Cookie": f"{COOKIE_NAME}={token}",
    }


# ---------------------------------------------------------------------------
# Milestone 1: Authenticated Credential Encryption Tests
# ---------------------------------------------------------------------------
class TestCredentialSecurity:
    def test_encrypt_and_decrypt_roundtrip(self):
        raw_secret = "rzp_live_secret_key_1234567890abcdef"
        encrypted = encrypt_secret(raw_secret)
        assert isinstance(encrypted, str)
        assert encrypted != raw_secret
        assert len(encrypted) > 20

        decrypted = decrypt_secret(encrypted)
        assert decrypted == raw_secret

    def test_decrypt_plain_fallback(self):
        legacy_secret = "legacy_unencrypted_secret_999"
        decrypted = decrypt_secret(legacy_secret)
        assert decrypted == legacy_secret

    def test_decrypt_empty_and_none(self):
        assert decrypt_secret(None) is None
        assert decrypt_secret("") == ""


# ---------------------------------------------------------------------------
# Milestone 2: Razorpay Provider & Payment Links
# ---------------------------------------------------------------------------
class TestRazorpayProvider:
    def test_create_payment_link_simulated(self):
        provider = RazorpayProvider(key_id=None, key_secret=None)
        res = provider.create_payment_link(
            amount=1499,
            customer_name="Priya Sharma",
            customer_email="priya@example.com",
            case_id="RC_TEST_001",
        )
        assert res["simulated"] is True
        assert res["amount"] == 149900  # in paise
        assert "rzp.io/i/rc_test_001" in res["short_url"]

    def test_create_payment_link_with_api(self):
        provider = RazorpayProvider(key_id="rzp_test_12345", key_secret="secret_abc")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "plink_test_999",
            "short_url": "https://rzp.io/i/LiveShortUrl999",
            "amount": 250000,
            "status": "created",
        }

        with patch("backend.services.razorpay_provider.httpx.post", return_value=mock_response) as mock_post:
            res = provider.create_payment_link(
                amount=2500,
                customer_name="Aarav Patel",
                customer_email="aarav@example.com",
                case_id="RC_TEST_002",
            )
            assert res["id"] == "plink_test_999"
            assert res["short_url"] == "https://rzp.io/i/LiveShortUrl999"
            assert res["simulated"] is False
            mock_post.assert_called_once()

    def test_provider_health_check_states(self):
        # Standby when no credentials
        p_unconfigured = RazorpayProvider()
        assert p_unconfigured.health()["status"] == "NOT_CONNECTED"

        # Connected when API returns 200
        p_configured = RazorpayProvider(key_id="rzp_test_key", key_secret="rzp_test_sec")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("backend.services.razorpay_provider.httpx.get", return_value=mock_resp):
            assert p_configured.health()["status"] == "CONNECTED"


# ---------------------------------------------------------------------------
# Milestone 3: Dispatch Engine & Suppression
# ---------------------------------------------------------------------------
class TestDispatchEngine:
    def test_dispatch_email_simulation(self, db, shared_merchant):
        case = RecoveryCase(
            case_id=f"RC_DISPATCH_{int(now_utc().timestamp())}",
            merchant_id=shared_merchant["id"],
            amount=999,
            customer_name="Rohan Verma",
            customer_email="rohan@example.com",
            state=CaseState.RECOVER.value,
        )
        db.add(case)
        db.commit()

        action = ActionRecord(
            case_id_ref=case.id,
            action_type="DISPATCH_NOTIFICATION",
            channel="EMAIL",
            status="PENDING_EXECUTION",
            idempotency_key=f"idemp_{case.case_id}",
        )
        db.add(action)
        db.commit()

        notification = Notification(
            case_id_ref=case.id,
            channel="EMAIL",
            recipient="rohan@example.com",
            message_content="Please complete your pending subscription renewal.",
            status="PENDING_DISPATCH",
        )
        db.add(notification)
        db.commit()

        res = dispatch_notification(db, notification)
        assert res.success is True
        assert notification.status == "SENT"
        assert res.message_id is not None
        assert action.status == "EXECUTED"

    def test_dispatch_suppression_on_opt_out(self, db, shared_merchant):
        cust = Customer(
            external_customer_id=f"cust_opt_{int(now_utc().timestamp())}",
            merchant_id=shared_merchant["id"],
            name="Opted Out User",
            email="optout@example.com",
            communication_opt_out=True,
        )
        db.add(cust)
        db.commit()

        case = RecoveryCase(
            case_id=f"RC_OPTOUT_{int(now_utc().timestamp())}",
            merchant_id=shared_merchant["id"],
            customer_id_ref=cust.id,
            amount=500,
            customer_name="Opted Out User",
            customer_email="optout@example.com",
            state=CaseState.RECOVER.value,
        )
        db.add(case)
        db.commit()

        notification = Notification(
            case_id_ref=case.id,
            channel="EMAIL",
            recipient="optout@example.com",
            message_content="Reminder",
            status="PENDING_DISPATCH",
        )
        db.add(notification)
        db.commit()

        res = dispatch_notification(db, notification)
        assert res.status == "SUPPRESSED_OPT_OUT"
        assert notification.status == "SUPPRESSED_OPT_OUT"

    def test_run_scheduler_cycle(self, db):
        cycle = run_scheduler_cycle(db)
        assert "tasks_processed" in cycle
        assert "actions_executed" in cycle
        assert "dispatches_sent" in cycle


# ---------------------------------------------------------------------------
# Milestone 4: Multi-Tenant Webhook Routing
# ---------------------------------------------------------------------------
class TestMultiTenantWebhooks:
    def test_merchant_webhook_signature_verification(self, client, db, shared_merchant):
        secret = "tenant_test_webhook_secret_xyz"
        cred = db.scalar(
            select(IntegrationCredential).where(
                IntegrationCredential.merchant_id == shared_merchant["id"],
                IntegrationCredential.provider == "razorpay"
            )
        )
        if not cred:
            cred = IntegrationCredential(
                merchant_id=shared_merchant["id"],
                provider="razorpay",
                key_id="rzp_test_merchant_key",
                secret_ref=encrypt_secret("test_sec"),
                webhook_secret_ref=encrypt_secret(secret),
            )
            db.add(cred)
        else:
            cred.webhook_secret_ref = encrypt_secret(secret)
        db.commit()

        ts = int(now_utc().timestamp())
        payload = {
            "event": "payment.failed",
            "id": f"evt_webhook_{ts}",
            "payload": {
                "payment": {
                    "entity": {
                        "id": f"pay_failed_{ts}",
                        "order_id": f"order_{ts}",
                        "amount": 299900,  # paise
                        "email": "customer@merchantdomain.com",
                        "notes": {"customer_name": "Karan Mehra"},
                    }
                }
            }
        }
        body_bytes = json.dumps(payload).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

        resp = client.post(
            f"/api/webhooks/razorpay/{shared_merchant['slug']}",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": signature,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["case"]["amount"] == 2999
        assert data["case"]["merchant_id"] == shared_merchant["id"]

    def test_merchant_webhook_invalid_signature_rejected(self, client, shared_merchant):
        body_bytes = b'{"event":"payment.failed"}'
        resp = client.post(
            f"/api/webhooks/razorpay/{shared_merchant['slug']}",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Razorpay-Signature": "invalid_signature_hash",
            },
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Milestone 5: Customer Recovery Portal Tests
# ---------------------------------------------------------------------------
class TestCustomerPortal:
    def test_portal_case_details_and_masking(self, client, db, shared_merchant):
        case_id = f"RC_PORTAL_{int(now_utc().timestamp())}_A"
        case = RecoveryCase(
            case_id=case_id,
            merchant_id=shared_merchant["id"],
            amount=3499,
            customer_name="Sunita Rao",
            customer_email="sunita.rao@example.com",
            state=CaseState.RECOVER.value,
        )
        db.add(case)
        db.commit()

        resp = client.get(f"/api/portal/{case_id}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["case_id"] == case_id
        assert data["amount"] == 3499
        assert data["merchant_name"] == shared_merchant["name"]
        assert "@" in data["customer_email"]
        assert "sunita.rao" not in data["customer_email"]
        assert "rzp.io/i/" in data["payment_url"]

    def test_portal_promise_submission(self, client, db, shared_merchant):
        case_id = f"RC_PORTAL_{int(now_utc().timestamp())}_B"
        case = RecoveryCase(
            case_id=case_id,
            merchant_id=shared_merchant["id"],
            amount=1999,
            customer_name="Vikram Singh",
            customer_email="vikram@example.com",
            state=CaseState.RECOVER.value,
        )
        db.add(case)
        db.commit()

        promise_date = (now_utc() + timedelta(days=5)).strftime("%Y-%m-%d")
        resp = client.post(
            f"/api/portal/{case_id}/promise",
            json={"promised_at": promise_date, "note": "Will pay after invoice approval"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "PROMISE_RECORDED"

        db.refresh(case)
        assert case.state == CaseState.WAIT.value

        task = db.scalar(select(ScheduledTask).where(ScheduledTask.case_id == case.case_id))
        assert task is not None
        assert task.task_type == "VERIFY_PROMISE"

    def test_portal_customer_opt_out(self, client, db, shared_merchant):
        ts = int(now_utc().timestamp())
        cust = Customer(
            external_customer_id=f"cust_opt_portal_{ts}",
            merchant_id=shared_merchant["id"],
            name="Opt Out Tester",
            email=f"portalopt_{ts}@example.com",
            communication_opt_out=False,
        )
        db.add(cust)
        db.commit()

        case = RecoveryCase(
            case_id=f"RC_PORTAL_{ts}_C",
            merchant_id=shared_merchant["id"],
            customer_id_ref=cust.id,
            amount=800,
            customer_name="Opt Out Tester",
            customer_email=f"portalopt_{ts}@example.com",
            state=CaseState.RECOVER.value,
        )
        db.add(case)
        db.commit()

        resp = client.post(f"/api/portal/{case.case_id}/opt-out", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "OPTED_OUT"

        db.refresh(cust)
        assert cust.communication_opt_out is True

    def test_portal_html_view(self, client):
        resp = client.get("/portal/RC_PORTAL_VIEW_TEST")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "RC_PORTAL_VIEW_TEST" in resp.text
        assert "Pay Securely with Razorpay" in resp.text


# ---------------------------------------------------------------------------
# Milestone 6: Operational CSV Export & Scheduler API
# ---------------------------------------------------------------------------
class TestOperationalFeatures:
    def test_cases_csv_export(self, client, admin_auth_headers):
        resp = client.get("/api/cases/export/csv", headers=admin_auth_headers)
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        lines = resp.text.strip().split("\r\n") if "\r\n" in resp.text else resp.text.strip().split("\n")
        assert len(lines) >= 1
        headers = lines[0].split(",")
        assert "case_id" in headers
        assert "customer_email" in headers
        assert "amount" in headers
        assert "state" in headers

    def test_scheduler_tick_route(self, client, admin_auth_headers):
        resp = client.post("/api/scheduler/tick", headers=admin_auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "cycle" in data

    def test_scheduler_status_route(self, client):
        resp = client.get("/api/scheduler/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "pending_tasks" in data
        assert "pending_actions" in data
        assert "pending_dispatches" in data
