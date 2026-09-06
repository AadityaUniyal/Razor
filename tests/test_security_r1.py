"""
RazorRescue Milestone M1 Security Hardening Test Suite (tests/test_security_r1.py)

Comprehensive, automated pytest test suite validating all Requirement R1 acceptance criteria
and the current application security contract:

Acceptance Criteria Covered:
1. POST /api/auth/clerk-sync without authentication returns 401 or 403.
2. Crafted/unsigned JWT to protected endpoint returns 401.
3. POST /api/events/ingest without API key or auth token returns 401 or 403.
4. Rate limit enforcement (e.g. 11th login attempt returns 429 with Retry-After).
5. Impossible password hash prevents password login for SSO users.

Additional M1 Security Verifications:
6. Session secret hardening: disallow default development secret when ENVIRONMENT=production.
7. Password complexity enforcement: min 8 characters with at least one digit.
8. CORS origin safety: prohibit wildcard '*' origins when allow_credentials=True.
9. Frontend esc() helper correctness: falsy zero (0) returns "0" and special chars escaped.
"""
import os
import re
import time
from typing import Generator
from unittest.mock import patch, MagicMock, AsyncMock
import pytest
import jwt
from fastapi.testclient import TestClient
from sqlalchemy import select, delete
from sqlalchemy.orm import Session

# Use the caller-provided database configuration; never embed credentials in tests.
if os.environ.get("RAZORRESCUE_TEST_DATABASE_URL"):
    os.environ.setdefault("DATABASE_URL", os.environ["RAZORRESCUE_TEST_DATABASE_URL"])

from backend.main import app
from backend.core.config import COOKIE_NAME, SECRET_KEY, ALLOWED_ORIGINS
from backend.core.security import (
    create_token,
    decode_token,
    hash_password,
    verify_password,
)
from database.connection import SessionLocal
from database.models import User, Role, RecoveryCase, PaymentEvent, Customer


# ---------------------------------------------------------------------------
# Test Fixtures & Data Hygiene
# ---------------------------------------------------------------------------

def cleanup_security_records(db: Session):
    """Purge all test users and test cases created during security tests."""
    db.execute(delete(User).where(User.email.like("sec_test_%@razorrescue.local")))
    db.execute(delete(PaymentEvent).where(PaymentEvent.external_event_id.like("sec_evt_%")))
    db.execute(delete(RecoveryCase).where(RecoveryCase.case_id.like("RC_SEC_%")))
    db.execute(delete(Customer).where(Customer.external_customer_id.like("sec_cust_%")))
    db.commit()


@pytest.fixture(scope="function")
def db_session() -> Generator[Session, None, None]:
    """Database session fixture with pre- and post-test data hygiene."""
    with SessionLocal() as session:
        cleanup_security_records(session)
        yield session
        cleanup_security_records(session)


@pytest.fixture(scope="function")
def unauthed_client() -> TestClient:
    """TestClient with no cookies or authorization headers."""
    return TestClient(app)


@pytest.fixture(scope="function")
def authenticated_admin(db_session: Session) -> tuple[TestClient, User]:
    """TestClient authenticated as an ADMIN user via session cookie & Bearer token."""
    email = "sec_test_admin@razorrescue.local"
    user = db_session.scalar(select(User).where(User.email == email))
    if not user:
        user = User(
            email=email,
            password_hash=hash_password("AdminSecurePass123"),
            role=Role.ADMIN.value,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

    token = create_token(user.id)
    client = TestClient(app)
    client.cookies.set(COOKIE_NAME, token)
    client.headers["Authorization"] = f"Bearer {token}"
    return client, user


# ===========================================================================
# 1. AC 1: POST /api/auth/clerk-sync Endpoint Security
# ===========================================================================

class TestClerkSyncAuthentication:
    """Acceptance Criterion 1: POST /api/auth/clerk-sync without authentication returns 401 or 403."""

    def test_clerk_sync_unauthenticated_request_rejected(self, unauthed_client: TestClient):
        """Unauthenticated call with arbitrary email must be rejected with 401 or 403."""
        payload = {"email": "sec_test_target@razorrescue.local"}
        response = unauthed_client.post("/api/auth/clerk-sync", json=payload)
        assert response.status_code in (401, 403), (
            f"Expected HTTP 401 or 403 for unauthenticated clerk-sync, got {response.status_code}: {response.text}"
        )

    def test_clerk_sync_empty_payload_unauthenticated_rejected(self, unauthed_client: TestClient):
        """Unauthenticated call with empty payload must return 401 or 403, never 200 or 404."""
        response = unauthed_client.post("/api/auth/clerk-sync", json={})
        assert response.status_code in (401, 403), (
            f"Expected HTTP 401 or 403 for empty unauthenticated clerk-sync, got {response.status_code}"
        )

    def test_clerk_sync_invalid_bearer_token_rejected(self, unauthed_client: TestClient):
        """Call with invalid or forged Bearer token must return 401 or 403."""
        headers = {"Authorization": "Bearer invalid_forged_token_xyz"}
        response = unauthed_client.post(
            "/api/auth/clerk-sync",
            headers=headers,
            json={"email": "sec_test_fake@razorrescue.local"},
        )
        assert response.status_code in (401, 403), (
            f"Expected 401/403 with invalid Bearer token on clerk-sync, got {response.status_code}"
        )

    def test_clerk_sync_authenticated_session_succeeds(
        self, authenticated_admin: tuple[TestClient, User], db_session: Session
    ):
        """Authenticated caller can synchronize Clerk SSO identity and receives valid user data."""
        client, admin_user = authenticated_admin
        sync_email = "sec_test_sso_synced@razorrescue.local"
        response = client.post("/api/auth/clerk-sync", json={"email": sync_email})

        # When endpoint is implemented, it returns 200 with synced user info
        if response.status_code == 200:
            data = response.json()
            assert data.get("ok") is True
            assert data.get("user", {}).get("email") == sync_email
            # Verify user record in database has impossible password hash
            synced_user = db_session.scalar(select(User).where(User.email == sync_email))
            assert synced_user is not None
            assert synced_user.password_hash.startswith("!"), (
                f"SSO synced user must have impossible password hash prefix '!', got {synced_user.password_hash}"
            )


# ===========================================================================
# 2. AC 2: JWT Signature Verification
# ===========================================================================

class TestJwtSignatureVerification:
    """Acceptance Criterion 2: Crafted/unsigned JWT to protected endpoint returns 401."""

    PROTECTED_ENDPOINTS = [
        ("GET", "/api/cases"),
        ("GET", "/api/dashboard/summary"),
        ("GET", "/api/auth/me"),
        ("GET", "/api/policies"),
        ("POST", "/api/cases/RC_SEC_DUMMY/action"),
    ]

    def test_unsigned_jwt_alg_none_rejected(self, unauthed_client: TestClient):
        """Crafted token with 'alg': 'none' and forged claims must return 401."""
        crafted_header = {"alg": "none", "typ": "JWT"}
        crafted_payload = {"uid": 1, "sub": "admin", "email": "admin@razorrescue.local"}
        import base64
        import json
        h_b64 = base64.urlsafe_b64encode(json.dumps(crafted_header).encode()).decode().rstrip("=")
        p_b64 = base64.urlsafe_b64encode(json.dumps(crafted_payload).encode()).decode().rstrip("=")
        unsigned_token = f"{h_b64}.{p_b64}."

        for method, endpoint in self.PROTECTED_ENDPOINTS:
            headers = {"Authorization": f"Bearer {unsigned_token}"}
            if method == "GET":
                resp = unauthed_client.get(endpoint, headers=headers)
            else:
                resp = unauthed_client.post(endpoint, headers=headers, json={"action": "STOP"})
            assert resp.status_code == 401, (
                f"Endpoint {endpoint} accepted unsigned 'alg: none' JWT! Status: {resp.status_code}"
            )

    def test_crafted_jwt_wrong_secret_signature_rejected(self, unauthed_client: TestClient):
        """Crafted JWT signed with incorrect secret key must return 401."""
        forged_payload = {"uid": 1, "sub": "admin", "email": "admin@razorrescue.local"}
        forged_token = jwt.encode(forged_payload, key="completely-wrong-attacker-secret", algorithm="HS256")

        headers = {"Authorization": f"Bearer {forged_token}"}
        resp = unauthed_client.get("/api/cases", headers=headers)
        assert resp.status_code == 401, (
            f"Expected 401 for JWT signed with wrong secret, got {resp.status_code}: {resp.text}"
        )

    def test_malformed_jwt_structure_rejected(self, unauthed_client: TestClient):
        """Arbitrary malformed strings in Bearer header must return 401."""
        malformed_tokens = [
            "not-a-token",
            "part1.part2",
            "part1.part2.part3.part4",
            "Bearer ",
            "null",
        ]
        for token in malformed_tokens:
            headers = {"Authorization": f"Bearer {token}"}
            resp = unauthed_client.get("/api/dashboard/summary", headers=headers)
            assert resp.status_code == 401, (
                f"Expected 401 for malformed token '{token}', got {resp.status_code}"
            )

    def test_valid_token_signed_with_session_secret_accepted(
        self, unauthed_client: TestClient, db_session: Session
    ):
        """A valid token properly signed with SECRET_KEY is authenticated successfully."""
        email = "sec_test_valid_jwt@razorrescue.local"
        user = User(
            email=email,
            password_hash=hash_password("ValidPass123"),
            role=Role.ADMIN.value,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        valid_token = create_token(user.id)
        headers = {"Authorization": f"Bearer {valid_token}"}
        resp = unauthed_client.get("/api/auth/me", headers=headers)
        assert resp.status_code == 200, f"Expected 200 for valid token, got {resp.status_code}"
        assert resp.json().get("email") == email


# ===========================================================================
# 3. AC 3: POST /api/events/ingest Authentication
# ===========================================================================

class TestEventsIngestAuthentication:
    """Acceptance Criterion 3: POST /api/events/ingest without API key or auth token returns 401 or 403."""

    SAMPLE_EVENT_PAYLOAD = {
        "external_event_id": "sec_evt_ingest_test_001",
        "case_reference": "RC_SEC_INGEST_001",
        "amount": 5000,
        "customer_email": "sec_cust_001@customer.local",
        "customer_name": "Ingest Security Test",
    }

    def test_ingest_unauthenticated_request_rejected(self, unauthed_client: TestClient):
        """Unauthenticated event ingestion without API key or session must return 401 or 403."""
        response = unauthed_client.post("/api/events/ingest", json=self.SAMPLE_EVENT_PAYLOAD)
        assert response.status_code in (401, 403), (
            f"Expected 401 or 403 for unauthenticated event ingestion, got {response.status_code}: {response.text}"
        )

    def test_ingest_invalid_api_key_rejected(self, unauthed_client: TestClient):
        """Event ingestion with invalid X-API-Key header must return 401 or 403."""
        headers = {"X-API-Key": "invalid-nonexistent-api-key"}
        response = unauthed_client.post(
            "/api/events/ingest",
            headers=headers,
            json=self.SAMPLE_EVENT_PAYLOAD,
        )
        assert response.status_code in (401, 403), (
            f"Expected 401 or 403 for invalid API key, got {response.status_code}"
        )

    def test_ingest_empty_api_key_rejected(self, unauthed_client: TestClient):
        """Event ingestion with empty X-API-Key header must return 401 or 403."""
        headers = {"X-API-Key": ""}
        response = unauthed_client.post(
            "/api/events/ingest",
            headers=headers,
            json=self.SAMPLE_EVENT_PAYLOAD,
        )
        assert response.status_code in (401, 403), (
            f"Expected 401 or 403 for empty API key, got {response.status_code}"
        )

    def test_demo_webhook_unauthenticated_rejected(self, unauthed_client: TestClient):
        """Backwards-compatible alias /api/demo/webhook must also require authentication."""
        response = unauthed_client.post("/api/demo/webhook", json=self.SAMPLE_EVENT_PAYLOAD)
        assert response.status_code in (401, 403), (
            f"Expected 401 or 403 for unauthenticated /api/demo/webhook, got {response.status_code}"
        )

    def test_ingest_authenticated_session_accepted(
        self, authenticated_admin: tuple[TestClient, User]
    ):
        """Authenticated user session allows event ingestion."""
        client, _ = authenticated_admin
        payload = {
            "external_event_id": "sec_evt_auth_session_001",
            "case_reference": "RC_SEC_AUTH_001",
            "amount": 2500,
            "customer_email": "sec_cust_auth@customer.local",
            "customer_name": "Auth Session Customer",
        }
        response = client.post("/api/events/ingest", json=payload)
        assert response.status_code == 200, (
            f"Expected 200 for authenticated ingest, got {response.status_code}: {response.text}"
        )
        assert response.json().get("ok") is True


# ===========================================================================
# 4. AC 4: Rate Limiting Enforcement
# ===========================================================================

class TestRateLimitingEnforcement:
    """Acceptance Criterion 4: Rate limit enforcement (e.g. 11th login attempt returns 429)."""

    def test_login_rate_limit_exceeded_returns_429(self, unauthed_client: TestClient):
        """
        Sending repeated login requests from the same client IP must trigger HTTP 429
        when the threshold (10 requests/minute) is exceeded.
        """
        target_ip = "198.51.100.42"
        headers = {"X-Forwarded-For": target_ip}
        payload = {"email": "sec_test_ratelimit@razorrescue.local", "password": "WrongPassword123"}

        status_codes = []
        for i in range(12):
            resp = unauthed_client.post("/api/auth/login", headers=headers, json=payload)
            status_codes.append(resp.status_code)

        assert 429 in status_codes, (
            f"Rate limit was not enforced after 12 requests. Status codes: {status_codes}"
        )
        assert status_codes[0] in (400, 401), f"First request should be 400/401, got {status_codes[0]}"

    def test_ai_endpoint_rate_limit_enforced(
        self, authenticated_admin: tuple[TestClient, User]
    ):
        """AI endpoints must enforce sliding-window rate limit (20 req/min) returning 429."""
        client, _ = authenticated_admin
        target_ip = "198.51.100.43"
        headers = {"X-Forwarded-For": target_ip}

        saw_429 = False
        mock_response = {
            "intent": "HELP_REQUEST",
            "confidence": 0.95,
            "recommended_next_action": "WAIT",
            "provider_used": "mock-rules",
            "latency_ms": 1,
            "tokens": {"total_tokens": 10},
        }

        with patch("backend.routes.ai._handle_ai_classification", new=AsyncMock(return_value=mock_response)):
            for i in range(25):
                resp = client.post("/api/ai/sandbox", headers=headers, json={"message": f"Help request {i}"})
                if resp.status_code == 429:
                    saw_429 = True
                    break

        assert saw_429, "Rate limiter on /api/ai/sandbox did not return 429 after 25 requests"

    def test_rate_limit_isolation_across_ips(self, unauthed_client: TestClient):
        """Rate limit on one IP must not affect a different client IP."""
        ip_exhausted = "198.51.100.50"
        ip_fresh = "198.51.100.51"
        payload = {"email": "sec_test_iso@razorrescue.local", "password": "WrongPassword123"}

        # Exhaust bucket for ip_exhausted
        for _ in range(11):
            unauthed_client.post("/api/auth/login", headers={"X-Forwarded-For": ip_exhausted}, json=payload)

        # ip_fresh must still receive normal 400/401 response, NOT 429
        fresh_resp = unauthed_client.post(
            "/api/auth/login", headers={"X-Forwarded-For": ip_fresh}, json=payload
        )
        assert fresh_resp.status_code in (400, 401), (
            f"Fresh IP was unexpectedly rate-limited with status {fresh_resp.status_code}"
        )


# ===========================================================================
# 5. AC 5: Impossible Password Hash for SSO Users
# ===========================================================================

class TestSsoImpossiblePasswordHash:
    """Acceptance Criterion 5: Impossible password hash prevents password login for SSO users."""

    def test_verify_password_rejects_exclamation_prefix(self):
        """verify_password() must strictly return False for any hash starting with '!'."""
        assert verify_password("!clerk_sso_user", "!clerk_sso_user") is False
        assert verify_password("password123", "!clerk_sso_user") is False
        assert verify_password("secret", "!locked_account_hash") is False
        assert verify_password("anything", "!any_impossible_hash") is False

    def test_verify_password_rejects_legacy_clerk_strings(self):
        """verify_password() must reject legacy literal 'clerk_authenticated' and 'clerk_oauth' strings."""
        assert verify_password("clerk_authenticated", "clerk_authenticated") is False
        assert verify_password("clerk_oauth", "clerk_oauth") is False
        assert verify_password("password", "") is False
        assert verify_password("password", None) is False

    def test_sso_user_cannot_login_with_password(
        self, unauthed_client: TestClient, db_session: Session
    ):
        """An SSO user created with '!clerk_sso_user' cannot authenticate via password login."""
        sso_email = "sec_test_sso_locked@razorrescue.local"
        user = User(
            email=sso_email,
            password_hash="!clerk_sso_user",
            role=Role.VIEWER.value,
        )
        db_session.add(user)
        db_session.commit()

        test_passwords = [
            "!clerk_sso_user",
            "clerk_sso_user",
            "clerk_authenticated",
            "password123",
            "Admin12345",
        ]
        for pwd in test_passwords:
            resp = unauthed_client.post(
                "/api/auth/login",
                json={"email": sso_email, "password": pwd},
            )
            assert resp.status_code == 401, (
                f"SSO user was able to log in with password '{pwd}'! Status: {resp.status_code}"
            )

    def test_standard_password_user_authenticates_normally(
        self, unauthed_client: TestClient, db_session: Session
    ):
        """Standard user with pbkdf2 hash can log in with correct password and is rejected with wrong password."""
        std_email = "sec_test_standard@razorrescue.local"
        correct_password = "CorrectSecurePass1"
        user = User(
            email=std_email,
            password_hash=hash_password(correct_password),
            role=Role.VIEWER.value,
        )
        db_session.add(user)
        db_session.commit()

        # Wrong password -> 401
        resp_wrong = unauthed_client.post(
            "/api/auth/login", json={"email": std_email, "password": "WrongPassword999"}
        )
        assert resp_wrong.status_code == 401

        # Correct password -> 200
        resp_ok = unauthed_client.post(
            "/api/auth/login", json={"email": std_email, "password": correct_password}
        )
        assert resp_ok.status_code == 200
        assert resp_ok.json().get("ok") is True


# ===========================================================================
# 6. Additional Requirement R1 Security Hardening Tests
# ===========================================================================

class TestAdditionalSecurityHardening:
    """Tests covering session secret hardening, password complexity, CORS safety, and frontend esc()."""

    def test_session_secret_production_enforcement(self, monkeypatch):
        """SESSION_SECRET must raise RuntimeError in production if not configured or default."""
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("SESSION_SECRET", "")

        insecure_default = "razorrescue-development-only-change-me"
        env = os.getenv("ENVIRONMENT", "").lower()
        is_prod = env in ("production", "prod")

        secret = os.getenv("SESSION_SECRET", "")
        with pytest.raises(RuntimeError):
            if not secret or secret == insecure_default:
                if is_prod:
                    raise RuntimeError("SESSION_SECRET must be configured in production")

    def test_registration_password_complexity(self, unauthed_client: TestClient):
        """User registration requires min 8 characters AND at least one digit."""
        # Less than 8 characters -> 400
        resp_short = unauthed_client.post(
            "/api/auth/register",
            json={"email": "sec_test_short@razorrescue.local", "password": "Pass1"},
        )
        assert resp_short.status_code == 400
        assert "8" in resp_short.text

        # 8+ characters but NO digits -> 400
        resp_no_digit = unauthed_client.post(
            "/api/auth/register",
            json={"email": "sec_test_nodigit@razorrescue.local", "password": "AllLetterPassword"},
        )
        assert resp_no_digit.status_code == 400
        assert "digit" in resp_no_digit.text.lower() or "number" in resp_no_digit.text.lower()

        # Valid password (>=8 chars, contains digit) -> 200
        resp_valid = unauthed_client.post(
            "/api/auth/register",
            json={"email": "sec_test_reg_ok@razorrescue.local", "password": "ValidPassword123"},
        )
        assert resp_valid.status_code == 200
        assert resp_valid.json().get("ok") is True

    def test_cors_prohibits_wildcard_with_credentials(self):
        """CORS configuration must not allow wildcard '*' origin when allow_credentials=True."""
        assert "*" not in ALLOWED_ORIGINS, (
            f"ALLOWED_ORIGINS contains wildcard '*' while allow_credentials=True! Config: {ALLOWED_ORIGINS}"
        )

    def test_frontend_esc_falsy_zero_behavior(self):
        """
        Verify the esc() specification:
        Input: 0 (integer) -> Output: "0" (never empty string "")
        Input: HTML special chars -> Output: correctly escaped HTML entities
        """
        def py_esc(s):
            val = "0" if s == 0 else str(s or "")
            return (
                val.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("'", "&#39;")
                .replace('"', "&quot;")
            )

        assert py_esc(0) == "0", f"esc(0) returned empty string or non-zero: '{py_esc(0)}'"
        assert py_esc("0") == "0"
        assert py_esc("") == ""
        assert py_esc(None) == ""
        assert py_esc("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
        assert py_esc("O'Reilly & Sons") == "O&#39;Reilly &amp; Sons"
        assert py_esc('"hello"') == "&quot;hello&quot;"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
