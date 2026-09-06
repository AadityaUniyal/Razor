"""Small Razorpay provider adapter with evidence-first verification semantics."""

from dataclasses import dataclass
import os
from typing import Any, Optional
from uuid import uuid4

import httpx


@dataclass
class ProviderResult:
    status: str
    request_id: str
    response_status: Optional[int]
    evidence: dict[str, Any]
    error_message: Optional[str] = None


class RazorpayProvider:
    base_url = "https://api.razorpay.com/v1"

    def __init__(self, key_id: Optional[str] = None, key_secret: Optional[str] = None, timeout: float = 8.0):
        self.key_id = key_id or os.getenv("RAZORPAY_KEY_ID", "")
        self.key_secret = key_secret or os.getenv("RAZORPAY_KEY_SECRET", "")
        self.timeout = timeout

    def _get(self, path: str) -> ProviderResult:
        request_id = str(uuid4())
        if not self.key_id or not self.key_secret:
            return ProviderResult("ERROR", request_id, None, {}, "Razorpay API credentials are not configured")
        try:
            response = httpx.get(
                f"{self.base_url}/{path.lstrip('/')}",
                auth=(self.key_id, self.key_secret),
                timeout=self.timeout,
                headers={"X-Razorpay-Request-Id": request_id},
            )
            data = response.json() if response.content else {}
            if response.status_code == 404:
                return ProviderResult("NOT_FOUND", request_id, response.status_code, data)
            if response.status_code >= 400:
                return ProviderResult("ERROR", request_id, response.status_code, data, f"Razorpay returned HTTP {response.status_code}")
            status = str(data.get("status", "")).lower()
            if status in {"captured", "paid", "active"}:
                return ProviderResult("MATCHED", request_id, response.status_code, data)
            if status in {"partially_paid", "partial"}:
                return ProviderResult("PARTIAL", request_id, response.status_code, data)
            return ProviderResult("PENDING", request_id, response.status_code, data)
        except (httpx.TimeoutException, httpx.HTTPError, ValueError) as exc:
            return ProviderResult("ERROR", request_id, None, {}, str(exc))

    def verify_payment(self, payment_id: str) -> ProviderResult:
        return self._get(f"payments/{payment_id}")

    def verify_invoice(self, invoice_id: str) -> ProviderResult:
        return self._get(f"invoices/{invoice_id}")

    def verify_subscription(self, subscription_id: str) -> ProviderResult:
        return self._get(f"subscriptions/{subscription_id}")

    def health(self) -> dict[str, Any]:
        configured = bool(self.key_id and self.key_secret)
        return {"provider": "razorpay", "configured": configured, "mode": "live" if configured else "unconfigured"}
