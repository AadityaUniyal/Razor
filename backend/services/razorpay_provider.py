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

    def create_payment_link(
        self,
        amount: int,
        currency: str = "INR",
        description: str = "Recovery Payment",
        customer_name: str = "",
        customer_email: str = "",
        customer_phone: Optional[str] = None,
        reference_id: Optional[str] = None,
        case_id: Optional[str] = None,
        expire_by_seconds: int = 86400 * 3,
    ) -> dict[str, Any]:
        """
        Creates a real Razorpay Payment Link using POST /v1/payment_links.
        If credentials are not configured or the API is unreachable, falls back
        gracefully to a deterministic simulated payment link.
        """
        request_id = str(uuid4())
        ref_id = reference_id or case_id or f"rc_{request_id[:8]}"
        amount_paise = max(100, int(amount * 100))

        if not self.key_id or not self.key_secret:
            return {
                "id": f"plink_sim_{uuid4().hex[:12]}",
                "short_url": f"https://rzp.io/i/{ref_id.lower()}",
                "amount": amount_paise,
                "currency": currency,
                "status": "simulated",
                "simulated": True,
                "request_id": request_id,
            }

        payload = {
            "amount": amount_paise,
            "currency": currency,
            "accept_partial": False,
            "reference_id": ref_id,
            "description": description[:200],
            "customer": {
                "name": customer_name or "Valued Customer",
                "email": customer_email or "customer@example.com",
            },
            "notify": {
                "sms": False,
                "email": False,
            },
            "reminder_enable": True,
            "notes": {
                "case_id": case_id or ref_id,
                "source": "RazorRescue",
            },
        }
        if customer_phone:
            payload["customer"]["contact"] = customer_phone

        try:
            response = httpx.post(
                f"{self.base_url}/payment_links",
                auth=(self.key_id, self.key_secret),
                json=payload,
                timeout=self.timeout,
                headers={"X-Razorpay-Request-Id": request_id},
            )
            if response.status_code in {200, 201}:
                data = response.json()
                return {
                    "id": data.get("id"),
                    "short_url": data.get("short_url") or f"https://rzp.io/i/{data.get('id')}",
                    "amount": data.get("amount", amount_paise),
                    "currency": data.get("currency", currency),
                    "status": data.get("status", "created"),
                    "simulated": False,
                    "request_id": request_id,
                    "raw": data,
                }
            else:
                error_data = response.json() if response.content else {}
                return {
                    "id": f"plink_fallback_{uuid4().hex[:12]}",
                    "short_url": f"https://rzp.io/i/{ref_id.lower()}",
                    "amount": amount_paise,
                    "currency": currency,
                    "status": "fallback",
                    "simulated": True,
                    "request_id": request_id,
                    "gateway_status": response.status_code,
                    "gateway_error": error_data.get("error", {}).get("description") or f"HTTP {response.status_code}",
                }
        except Exception as exc:
            return {
                "id": f"plink_err_{uuid4().hex[:12]}",
                "short_url": f"https://rzp.io/i/{ref_id.lower()}",
                "amount": amount_paise,
                "currency": currency,
                "status": "error_fallback",
                "simulated": True,
                "request_id": request_id,
                "error": str(exc),
            }

    def retry_invoice(self, invoice_id: str) -> ProviderResult:
        request_id = str(uuid4())
        if not self.key_id or not self.key_secret:
            return ProviderResult("ERROR", request_id, None, {}, "Razorpay API credentials are not configured")
        try:
            response = httpx.post(
                f"{self.base_url}/invoices/{invoice_id}/resend",
                auth=(self.key_id, self.key_secret),
                timeout=self.timeout,
                headers={"X-Razorpay-Request-Id": request_id},
            )
            data = response.json() if response.content else {}
            if response.status_code in {200, 201}:
                return ProviderResult("MATCHED", request_id, response.status_code, data)
            return ProviderResult("ERROR", request_id, response.status_code, data, f"Retry returned {response.status_code}")
        except Exception as exc:
            return ProviderResult("ERROR", request_id, None, {}, str(exc))

    def health(self) -> dict[str, Any]:
        configured = bool(self.key_id and self.key_secret)
        if not configured:
            return {"provider": "razorpay", "configured": False, "mode": "unconfigured", "status": "NOT_CONNECTED"}
        request_id = str(uuid4())
        try:
            resp = httpx.get(
                f"{self.base_url}/payments",
                params={"count": 1},
                auth=(self.key_id, self.key_secret),
                timeout=min(self.timeout, 4.0),
                headers={"X-Razorpay-Request-Id": request_id},
            )
            if resp.status_code == 200:
                is_test = str(self.key_id).startswith("rzp_test_")
                return {
                    "provider": "razorpay",
                    "configured": True,
                    "mode": "test" if is_test else "live",
                    "status": "CONNECTED",
                    "latency_ms": round(resp.elapsed.total_seconds() * 1000, 1),
                }
            elif resp.status_code == 401:
                return {
                    "provider": "razorpay",
                    "configured": True,
                    "mode": "invalid_credentials",
                    "status": "AUTH_FAILED",
                    "error": "Invalid API key or secret",
                }
            else:
                return {
                    "provider": "razorpay",
                    "configured": True,
                    "mode": "live",
                    "status": f"HTTP_{resp.status_code}",
                }
        except Exception as exc:
            # If network error or offline, report graceful offline live mode
            return {
                "provider": "razorpay",
                "configured": True,
                "mode": "live",
                "status": "STANDBY",
                "error": str(exc),
            }
