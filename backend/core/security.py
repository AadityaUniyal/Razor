from hashlib import pbkdf2_hmac, sha256
from hmac import compare_digest
from secrets import token_hex
from typing import Optional

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from backend.core.config import COOKIE_NAME, SECRET_KEY
from database.connection import get_db
from database.models import User, Role

serializer = URLSafeTimedSerializer(SECRET_KEY)


def hash_password(password: str) -> str:
    salt = token_hex(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    if stored.startswith("pbkdf2_sha256$"):
        _, salt, expected = stored.split("$", 2)
        actual = pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310_000).hex()
        return compare_digest(actual, expected)
    return compare_digest(stored, sha256(password.encode("utf-8")).hexdigest())


def create_token(user_id: int) -> str:
    return serializer.dumps({"uid": user_id})


def decode_token(token: str) -> Optional[int]:
    try:
        data = serializer.loads(token, max_age=60 * 60 * 24 * 7)
        return int(data["uid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError):
        return None


import jwt
from sqlalchemy import select


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    # 1. Check Authorization Bearer header (Clerk session token or Bearer token)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:].strip()
        # Check if it's our internal serializer token
        uid = decode_token(bearer_token)
        if uid:
            user = db.get(User, uid)
            if user:
                return user

        # Check if it's a Clerk JWT
        try:
            claims = jwt.decode(bearer_token, options={"verify_signature": False})
            email = claims.get("email") or claims.get("primary_email_address")
            if not email and "sub" in claims:
                email = f"{claims['sub']}@clerk.local"
            if email:
                user = db.scalar(select(User).where(User.email == email))
                if not user:
                    user = User(email=email, password_hash="clerk_oauth", role=Role.VIEWER.value)
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                return user
        except Exception:
            pass

    # 2. Check Cookie session
    token = request.cookies.get(COOKIE_NAME)
    if token:
        uid = decode_token(token)
        if uid:
            user = db.get(User, uid)
            if user:
                return user

    raise HTTPException(status_code=401, detail="Not signed in")
