from datetime import datetime, timedelta, timezone
from hashlib import pbkdf2_hmac, sha256
from hmac import compare_digest
import logging
from secrets import token_hex
from typing import Optional

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
import jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import COOKIE_NAME, RZP_COOKIE_NAME, SECRET_KEY
from database.connection import get_db
from database.models import User

logger = logging.getLogger("razorrescue.security")

serializer = URLSafeTimedSerializer(SECRET_KEY)

CLERK_PLACEHOLDER_HASHES = frozenset({
    "clerk_authenticated",
    "clerk_oauth",
    "!clerk_sso_user",
})


def hash_password(password: str) -> str:
    """Hashes a plain password using PBKDF2-HMAC-SHA256 with a unique salt."""
    salt = token_hex(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    """Verifies a plain password against a stored hash in constant time.

    Security rules:
    - Rejects empty stored hashes or empty passwords immediately.
    - Rejects accounts locked with '!' prefix (Unix shadow convention, e.g. '!clerk_sso_user').
    - Rejects legacy/known Clerk SSO placeholder hashes immediately.
    - Constant-time verification for PBKDF2-HMAC-SHA256 and legacy SHA-256 hashes.
    """
    if not stored or not password:
        return False

    # Immediate rejection for locked SSO accounts and placeholder hashes
    if stored.startswith("!") or stored in CLERK_PLACEHOLDER_HASHES:
        return False

    if stored.startswith("pbkdf2_sha256$"):
        parts = stored.split("$", 2)
        if len(parts) != 3:
            return False
        _, salt, expected = parts
        try:
            actual = pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310_000).hex()
            return compare_digest(actual, expected)
        except Exception:
            return False

    # Legacy SHA-256 fallback (never executed for '!' or placeholder hashes)
    try:
        return compare_digest(stored, sha256(password.encode("utf-8")).hexdigest())
    except Exception:
        return False


def create_token(user_id: int) -> str:
    """Creates a signed session token using URLSafeTimedSerializer."""
    return serializer.dumps({"uid": user_id})


def create_jwt_token(user_id: int, email: str = "", expires_in_seconds: int = 60 * 60 * 24 * 7) -> str:
    """Creates an HS256-signed JWT token using SECRET_KEY."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "uid": user_id,
        "email": email,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in_seconds)).timestamp()),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_token(token: str) -> Optional[int]:
    """Decodes a session token (either itsdangerous serializer or valid HS256 JWT) to a user ID.

    Returns user ID integer if valid and unexpired; None otherwise.
    """
    if not token or not isinstance(token, str):
        return None

    # 1. Check internal URLSafeTimedSerializer format
    try:
        data = serializer.loads(token, max_age=60 * 60 * 24 * 7)
        return int(data["uid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        pass
    except Exception:
        pass

    # 2. Check HS256 JWT format verified against SECRET_KEY
    try:
        claims = jwt.decode(
            token,
            SECRET_KEY,
            algorithms=["HS256"],
            options={"verify_signature": True, "verify_exp": True},
        )
        uid = claims.get("uid") or claims.get("sub")
        if uid is not None:
            return int(uid)
    except (jwt.PyJWTError, ValueError, TypeError):
        pass

    return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Resolves and authenticates the requesting User from Authorization header or session cookies.

    Security enforcement:
    1. If an 'Authorization: Bearer <token>' header is present:
       - Checks if it matches internal URLSafeTimedSerializer token.
       - Validates HS256 JWT signature strictly against SECRET_KEY.
       - Unsigned (alg: none), tampered, or invalid JWTs MUST raise HTTP 401 immediately.
    2. If no Bearer header is present, falls back to session cookies:
       - Checks 'COOKIE_NAME' (razorrescue_session) and 'RZP_COOKIE_NAME' (rzp_session).
    3. If unauthenticated, raises HTTP 401 Unauthorized.
    """
    # 1. Check Authorization Bearer header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:].strip()
        if not bearer_token:
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        # 1a. Check internal serializer token
        try:
            data = serializer.loads(bearer_token, max_age=60 * 60 * 24 * 7)
            uid = int(data.get("uid"))
            user = db.get(User, uid)
            if user:
                return user
        except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
            pass

        # 1b. Enforce strict JWT signature verification using HS256 against SECRET_KEY
        try:
            payload = jwt.decode(
                bearer_token,
                SECRET_KEY,
                algorithms=["HS256"],
                options={"verify_signature": True, "verify_exp": True},
            )
        except jwt.PyJWTError as exc:
            logger.warning("Rejected invalid/tampered Bearer JWT: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid authentication credentials")

        # Resolve user from JWT claims
        user = None
        uid = payload.get("uid") or payload.get("sub")
        if uid is not None:
            try:
                user = db.get(User, int(uid))
            except (ValueError, TypeError):
                pass
        if not user and payload.get("email"):
            email_val = str(payload.get("email")).strip().lower()
            user = db.scalar(select(User).where(User.email == email_val))

        if user:
            return user

        raise HTTPException(status_code=401, detail="User not found")

    # 2. Check Cookie session (rzp_session and razorrescue_session)
    cookie_token = (
        request.cookies.get(COOKIE_NAME)
        or request.cookies.get(RZP_COOKIE_NAME)
        or request.cookies.get("rzp_session")
    )
    if cookie_token:
        # Check serializer
        uid = decode_token(cookie_token)
        if uid:
            user = db.get(User, uid)
            if user:
                return user

        # Check HS256 JWT in cookie
        try:
            payload = jwt.decode(
                cookie_token,
                SECRET_KEY,
                algorithms=["HS256"],
                options={"verify_signature": True, "verify_exp": True},
            )
            uid = payload.get("uid") or payload.get("sub")
            if uid is not None:
                try:
                    user = db.get(User, int(uid))
                    if user:
                        return user
                except (ValueError, TypeError):
                    pass
            if payload.get("email"):
                email_val = str(payload.get("email")).strip().lower()
                user = db.scalar(select(User).where(User.email == email_val))
                if user:
                    return user
        except jwt.PyJWTError:
            pass

    raise HTTPException(status_code=401, detail="Not signed in")
