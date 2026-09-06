"""
Zero-dependency sliding-window in-memory rate limiter for RazorRescue.
Enforces request quotas: Auth (10/min), AI (20/min), Ingest (60/min).
"""
import math
import os
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class SlidingWindowRateLimiter:
    """Thread-safe sliding-window rate limiter using deques of epoch timestamps."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._windows: Dict[str, deque] = defaultdict(deque)

    def is_allowed(
        self, key: str, limit: int, window_seconds: int = 60
    ) -> Tuple[bool, int]:
        if not self.enabled or limit <= 0:
            return True, 0

        now = time.monotonic()
        cutoff = now - window_seconds

        with self._lock:
            timestamps = self._windows[key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if len(timestamps) >= limit:
                oldest = timestamps[0]
                retry_after = max(1, math.ceil(oldest + window_seconds - now))
                return False, retry_after

            timestamps.append(now)
            return True, 0

    def reset(self):
        """Reset all rate limiter windows (used in tests)."""
        with self._lock:
            self._windows.clear()


RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() not in ("false", "0", "no")
RATE_LIMIT_AUTH = int(os.getenv("RATE_LIMIT_AUTH", "10"))
RATE_LIMIT_AI = int(os.getenv("RATE_LIMIT_AI", "20"))
RATE_LIMIT_INGEST = int(os.getenv("RATE_LIMIT_INGEST", "60"))

default_rate_limiter = SlidingWindowRateLimiter(enabled=RATE_LIMIT_ENABLED)


def get_client_identifier(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
        if client_ip:
            return client_ip
    if request.client and request.client.host:
        return request.client.host
    return "127.0.0.1"


def categorize_request(path: str, method: str) -> Optional[Tuple[str, int, int]]:
    norm_path = path.rstrip("/") or "/"
    method = method.upper()

    if method == "POST" and norm_path in {
        "/api/auth/login",
        "/api/login",
        "/api/auth/register",
        "/api/register",
        "/api/auth/clerk-sync",
        "/api/clerk-sync",
    }:
        return ("auth", RATE_LIMIT_AUTH, 60)

    if norm_path.startswith("/api/ai"):
        return ("ai", RATE_LIMIT_AI, 60)

    if method == "POST" and (
        norm_path in {"/api/events/ingest", "/api/demo/webhook", "/api/webhooks/razorpay"}
        or norm_path.startswith("/api/events/ingest")
    ):
        return ("ingest", RATE_LIMIT_INGEST, 60)

    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, limiter: Optional[SlidingWindowRateLimiter] = None):
        super().__init__(app)
        self.limiter = limiter or default_rate_limiter

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        limit_info = categorize_request(request.url.path, request.method)
        if limit_info:
            category, limit, window = limit_info
            client_ip = get_client_identifier(request)
            key = f"{category}:{client_ip}"

            allowed, retry_after = self.limiter.is_allowed(key, limit, window)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                    content={
                        "error": "rate_limit_exceeded",
                        "detail": f"Rate limit exceeded for {category} operations. Please retry after {retry_after} seconds.",
                        "retry_after": retry_after,
                    },
                )

        return await call_next(request)


def check_rate_limit(category: str, limit: int, window: int = 60):
    def dependency(request: Request):
        client_ip = get_client_identifier(request)
        key = f"{category}:{client_ip}"
        allowed, retry_after = default_rate_limiter.is_allowed(key, limit, window)
        if not allowed:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded for {category} operations. Please retry after {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )
    return dependency
