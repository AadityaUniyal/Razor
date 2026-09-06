import json
import logging
import os
import re
from secrets import token_hex
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import CLERK_PUBLISHABLE_KEY, CLERK_SECRET_KEY, COOKIE_NAME, RZP_COOKIE_NAME, SECRET_KEY
from backend.core.security import (
    create_token,
    decode_token,
    get_current_user,
    hash_password,
    verify_password,
)
from database.connection import get_db
from database.models import Merchant, RecoveryPolicy, Role, User

logger = logging.getLogger("razorrescue.auth")

router = APIRouter(prefix="/api", tags=["auth"])


def _set_auth_cookies(response: JSONResponse | RedirectResponse, token: str) -> None:
    """Sets secure HTTP-only cookies for both COOKIE_NAME and rzp_session."""
    is_secure = os.getenv("COOKIE_SECURE", "").strip().lower() == "true"
    max_age = 60 * 60 * 24 * 7  # 7 days
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=max_age,
    )
    response.set_cookie(
        RZP_COOKIE_NAME,
        token,
        httponly=True,
        secure=is_secure,
        samesite="lax",
        max_age=max_age,
    )


def _clear_auth_cookies(response: JSONResponse | RedirectResponse) -> None:
    """Clears all session cookies."""
    response.delete_cookie(COOKIE_NAME)
    response.delete_cookie(RZP_COOKIE_NAME)
    response.delete_cookie("rzp_session")


@router.post("/register")
@router.post("/auth/register")
async def register(request: Request, db: Session = Depends(get_db)):
    email = ""
    password = ""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
            email = str(body.get("email", "")).strip()
            password = str(body.get("password", "")).strip()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("Non-fatal JSON parsing error during registration: %s", exc)
        except Exception as exc:
            logger.debug("Unexpected error reading request JSON during registration: %s", exc)

    if not email:
        try:
            form = await request.form()
            email = str(form.get("email", "")).strip()
            password = str(form.get("password", "")).strip()
        except (ValueError, KeyError) as exc:
            logger.debug("Non-fatal form parsing error during registration: %s", exc)
        except Exception as exc:
            logger.debug("Unexpected error reading request form during registration: %s", exc)

    if not email or not password:
        raise HTTPException(400, "Email and password are required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "A valid email address is required")
    if len(password) < 8 or not any(c.isdigit() for c in password):
        raise HTTPException(400, "Password must contain at least 8 characters and at least one digit")

    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        raise HTTPException(400, "Email already registered")

    slug_base = re.sub(r"[^a-z0-9]+", "-", email.split("@", 1)[0].lower()).strip("-") or "merchant"
    merchant = Merchant(name=f"{email.split('@', 1)[0]} workspace", slug=f"{slug_base}-{token_hex(4)}", environment="test")
    db.add(merchant)
    db.flush()
    db.add(RecoveryPolicy(
        merchant_id=merchant.id,
        version=f"{merchant.slug}-v1",
        name=f"{merchant.name} Recovery Policy",
        configuration={
            "maximum_automated_attempts": 3,
            "maximum_customer_communications": 3,
            "minimum_amount_for_human_escalation": 10000,
            "promise_grace_hours": 2,
            "ai_confidence_threshold": 0.70,
            "approval_required": True,
            "approval_mode": "recommend_and_approve",
            "channel_costs": {"VERIFY": 0, "WAIT": 0, "RECOVERY_LINK": 2, "ESCALATE": 100, "STOP": 0},
        },
        active=True,
    ))
    user = User(email=email, password_hash=hash_password(password), role=Role.ADMIN.value, merchant_id=merchant.id)
    db.add(user)
    db.commit()

    token = create_token(user.id)
    response = (
        RedirectResponse("/", status_code=303)
        if "application/json" not in content_type
        else JSONResponse({"ok": True, "user": {"email": user.email, "role": user.role}})
    )
    _set_auth_cookies(response, token)
    return response


@router.post("/login")
@router.post("/auth/login")
async def login(request: Request, db: Session = Depends(get_db)):
    email = ""
    password = ""
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            body = await request.json()
            email = str(body.get("email", "")).strip()
            password = str(body.get("password", "")).strip()
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("Non-fatal JSON parsing error during login: %s", exc)
        except Exception as exc:
            logger.debug("Unexpected error reading request JSON during login: %s", exc)

    if not email:
        try:
            form = await request.form()
            email = str(form.get("email", "")).strip()
            password = str(form.get("password", "")).strip()
        except (ValueError, KeyError) as exc:
            logger.debug("Non-fatal form parsing error during login: %s", exc)
        except Exception as exc:
            logger.debug("Unexpected error reading request form during login: %s", exc)

    if not email or not password:
        raise HTTPException(400, "Email and password are required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "A valid email address is required")

    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")

    if not user.password_hash.startswith("pbkdf2_sha256$"):
        user.password_hash = hash_password(password)
        db.commit()

    token = create_token(user.id)
    response = (
        RedirectResponse("/", status_code=303)
        if "application/json" not in content_type
        else JSONResponse({"ok": True, "user": {"email": user.email, "role": user.role}})
    )
    _set_auth_cookies(response, token)
    return response


@router.get("/logout")
@router.post("/logout")
@router.get("/auth/logout")
@router.post("/auth/logout")
def logout(request: Request):
    response = RedirectResponse("/", status_code=303) if request.method == "GET" else JSONResponse({"ok": True})
    _clear_auth_cookies(response)
    return response


@router.get("/me")
@router.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return {"email": user.email, "role": user.role}


@router.get("/auth/clerk-config")
def clerk_config():
    return {
        "publishable_key": CLERK_PUBLISHABLE_KEY,
        "is_enabled": bool(CLERK_PUBLISHABLE_KEY),
    }


@router.post("/auth/clerk-sync")
@router.post("/clerk-sync")
async def clerk_sync(request: Request, db: Session = Depends(get_db)):
    """Restored & hardened Clerk SSO sync endpoint.

    Accepts sync requests from authenticated Clerk SSO sessions.
    Strict authentication protection:
    - Unauthenticated calls MUST return HTTP 401.
    - Verified callers receive a session cookie; if the user record does not exist,
      it is provisioned with password_hash='!clerk_sso_user' (impossible password hash).
    """
    # 1. Parse payload
    payload: dict = {}
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" in content_type:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        except Exception as exc:
            logger.debug("Non-fatal clerk-sync JSON parse error: %s", exc)

    if not payload:
        try:
            form = await request.form()
            payload = dict(form)
        except Exception:
            pass

    # 2. Strict Authentication Guard
    authenticated = False
    verified_email: Optional[str] = None

    # Check 2a: Existing active session cookie or serializer token
    try:
        current_user = get_current_user(request, db)
        authenticated = True
        verified_email = current_user.email
    except HTTPException:
        pass

    # Check 2b: Token passed in Authorization header or payload
    auth_header = request.headers.get("Authorization", "")
    token_str = ""
    if auth_header.startswith("Bearer "):
        token_str = auth_header[7:].strip()
    elif payload.get("token"):
        token_str = str(payload.get("token")).strip()

    if token_str and not authenticated:
        # Check if internal serializer token
        uid = decode_token(token_str)
        if uid:
            u = db.get(User, uid)
            if u:
                authenticated = True
                verified_email = u.email

        # Check if HS256 JWT signed with SECRET_KEY
        if not authenticated:
            try:
                claims = jwt.decode(
                    token_str,
                    SECRET_KEY,
                    algorithms=["HS256"],
                    options={"verify_signature": True, "verify_exp": True},
                )
                authenticated = True
                if claims.get("email"):
                    verified_email = str(claims["email"]).strip().lower()
            except jwt.PyJWTError:
                pass

    # If unauthenticated, reject immediately with HTTP 401
    if not authenticated:
        raise HTTPException(
            status_code=401,
            detail="Authentication required for Clerk sync. Please provide a valid session or token.",
        )

    # 3. Resolve target user email
    payload_email = (payload.get("email") or "").strip().lower()
    email = payload_email or verified_email
    if not email:
        raise HTTPException(400, "User email is required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(400, "A valid email address is required")

    # 4. Provision or sanitize user record
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        slug_base = re.sub(r"[^a-z0-9]+", "-", email.split("@", 1)[0].lower()).strip("-") or "merchant"
        merchant = Merchant(name=f"{email.split('@', 1)[0]} workspace", slug=f"{slug_base}-{token_hex(4)}", environment="test")
        db.add(merchant)
        db.flush()
        db.add(RecoveryPolicy(
            merchant_id=merchant.id,
            version=f"{merchant.slug}-v1",
            name=f"{merchant.name} Recovery Policy",
            configuration={
                "maximum_automated_attempts": 3,
                "maximum_customer_communications": 3,
                "minimum_amount_for_human_escalation": 10000,
                "promise_grace_hours": 2,
                "ai_confidence_threshold": 0.70,
                "approval_required": True,
                "approval_mode": "recommend_and_approve",
                "channel_costs": {"VERIFY": 0, "WAIT": 0, "RECOVERY_LINK": 2, "ESCALATE": 100, "STOP": 0},
            },
            active=True,
        ))
        user = User(
            email=email,
            password_hash="!clerk_sso_user",
            role=Role.VIEWER.value,
            merchant_id=merchant.id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        # Sanitize legacy/vulnerable placeholder hashes
        if user.password_hash in ("clerk_authenticated", "clerk_oauth"):
            user.password_hash = "!clerk_sso_user"
            db.commit()

    # 5. Issue session cookies and JSON response
    session_token = create_token(user.id)
    response = JSONResponse({
        "ok": True,
        "user": {"email": user.email, "role": user.role},
    })
    _set_auth_cookies(response, session_token)
    return response
