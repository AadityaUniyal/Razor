import os

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.core.config import COOKIE_NAME
from backend.core.security import hash_password, verify_password, create_token, get_current_user
from database.connection import get_db
from database.models import User, Role

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/register")
@router.post("/auth/register")
def register(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if len(password) < 8:
        raise HTTPException(400, "Password must contain at least 8 characters")
    existing = db.scalar(select(User).where(User.email == email))
    if existing:
        raise HTTPException(400, "Email already registered")
    user = User(email=email, password_hash=hash_password(password), role=Role.VIEWER.value)
    db.add(user)
    db.commit()
    token = create_token(user.id)
    response = JSONResponse({"ok": True, "user": {"email": user.email, "role": user.role}})
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=os.getenv("COOKIE_SECURE", "").lower() == "true",
        samesite="lax", max_age=60 * 60 * 24 * 7
    )
    return response


from fastapi import Request

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
        except Exception:
            pass
    if not email:
        try:
            form = await request.form()
            email = str(form.get("email", "")).strip()
            password = str(form.get("password", "")).strip()
        except Exception:
            pass
    if not email or not password:
        raise HTTPException(400, "Email and password are required")

    user = db.scalar(select(User).where(User.email == email))
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    if not user.password_hash.startswith("pbkdf2_sha256$"):
        user.password_hash = hash_password(password)
        db.commit()
    token = create_token(user.id)
    response = JSONResponse({"ok": True, "user": {"email": user.email, "role": user.role}})
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=os.getenv("COOKIE_SECURE", "").lower() == "true",
        samesite="lax", max_age=60 * 60 * 24 * 7
    )
    return response


@router.post("/logout")
@router.post("/auth/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME)
    return response


@router.get("/me")
@router.get("/auth/me")
def me(user: User = Depends(get_current_user)):
    return {"email": user.email, "role": user.role}


@router.get("/auth/clerk-config")
def clerk_config():
    from backend.core.config import CLERK_PUBLISHABLE_KEY
    return {
        "publishable_key": CLERK_PUBLISHABLE_KEY,
        "is_enabled": bool(CLERK_PUBLISHABLE_KEY),
    }


@router.post("/auth/clerk-sync")
def clerk_sync(payload: dict = None, db: Session = Depends(get_db)):
    from backend.core.config import CLERK_PUBLISHABLE_KEY
    payload = payload or {}
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(400, "User email is required")

    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(email=email, password_hash="clerk_authenticated", role=Role.VIEWER.value)
        db.add(user)
        db.commit()
        db.refresh(user)

    token = create_token(user.id)
    response = JSONResponse({"ok": True, "user": {"email": user.email, "role": user.role}})
    response.set_cookie(
        COOKIE_NAME, token, httponly=True, secure=os.getenv("COOKIE_SECURE", "").lower() == "true",
        samesite="lax", max_age=60 * 60 * 24 * 7
    )
    return response
