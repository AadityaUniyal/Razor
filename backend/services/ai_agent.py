import asyncio
import calendar
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

import httpx
from pydantic import BaseModel, Field, field_validator

from backend.core.config import (
    GEMINI_API_KEY,
    GEMINI_MODEL,
    GROQ_API_KEY,
    GROQ_MODEL,
)
from database.models import now_utc


class CustomerIntent(BaseModel):
    intent: str = Field(
        pattern="^(PROMISE_TO_PAY|PAYMENT_COMPLETED_CLAIM|NEED_HELP|OPT_OUT|DISPUTE|UNKNOWN)$"
    )
    promise_detected: bool
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_next_action: str = Field(
        pattern="^(WAIT|VERIFY|RECOVER|ESCALATE|STOP|NEEDS_POLICY_REVIEW)$"
    )
    temporal_expression: Optional[str] = None
    customer_tone: Optional[str] = "neutral"

    @field_validator("customer_tone", mode="before")
    @classmethod
    def sanitize_tone(cls, v: Any) -> str:
        if not v:
            return "neutral"
        v_str = str(v).lower().strip()
        if v_str in {"cooperative", "neutral", "negative"}:
            return v_str
        if v_str in {"assertive", "questioning"}:
            return "neutral"
        if v_str in {"dissatisfied", "angry", "frustrated"}:
            return "negative"
        return "neutral"

    @field_validator("confidence", mode="before")
    @classmethod
    def sanitize_confidence(cls, v: Any) -> float:
        try:
            val = float(v)
            return max(0.0, min(1.0, val))
        except Exception:
            return 0.5

    @field_validator("recommended_next_action", mode="before")
    @classmethod
    def sanitize_action(cls, v: Any) -> str:
        v_str = str(v).upper().strip() if v else "VERIFY"
        valid_actions = {"WAIT", "VERIFY", "RECOVER", "ESCALATE", "STOP", "NEEDS_POLICY_REVIEW"}
        if v_str in valid_actions:
            return v_str
        return "VERIFY"

    @field_validator("intent", mode="before")
    @classmethod
    def sanitize_intent(cls, v: Any) -> str:
        v_str = str(v).upper().strip() if v else "UNKNOWN"
        valid_intents = {
            "PROMISE_TO_PAY",
            "PAYMENT_COMPLETED_CLAIM",
            "NEED_HELP",
            "OPT_OUT",
            "DISPUTE",
            "UNKNOWN",
        }
        if v_str in valid_intents:
            return v_str
        return "UNKNOWN"

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def __contains__(self, item: str) -> bool:
        return hasattr(self, item)


def resolve_temporal_expression(expression: str, base_time: Optional[datetime] = None) -> datetime:
    """
    Deterministically resolves natural language temporal expressions (Hinglish/English)
    into concrete UTC timestamps with safe default grace periods.
    Handles weekdays, kal, parso, salary, agla hafta, mahine ke aakhri.
    """
    now = base_time or now_utc()
    text = (expression or "").lower().strip()

    target = now + timedelta(hours=2)  # default

    weekday_map = {
        0: ["monday", "somwar", "somvaar", "somvar"],
        1: ["tuesday", "mangalwar", "mangalvaar", "mangalvar"],
        2: ["wednesday", "budhwar", "budhvaar", "budhvar"],
        3: ["thursday", "guruwar", "guruvaar", "guruvar", "brihaspativar", "veervar"],
        4: ["friday", "shukrawar", "shukravaar", "shukrawaar", "shukrvar"],
        5: ["saturday", "shaniwar", "shanivaar", "shaniwaar", "shanivar"],
        6: ["sunday", "ravivar", "ravivaar", "raviwaar", "itwar", "aitwar"],
    }

    matched_weekday = None
    for day_num, keywords in weekday_map.items():
        if any(kw in text for kw in keywords):
            matched_weekday = day_num
            break

    if "parso" in text or "day after tomorrow" in text or "parson" in text:
        target = now + timedelta(days=2)
    elif "kal" in text or "tomorrow" in text or "agla din" in text or "agle din" in text:
        target = now + timedelta(days=1)
    elif matched_weekday is not None:
        days_ahead = (matched_weekday - now.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7
        target = now + timedelta(days=days_ahead)
    elif "agla hafta" in text or "agle hafte" in text or "next week" in text:
        target = now + timedelta(days=7)
    elif "mahine ke aakhri" in text or "month end" in text or "end of month" in text:
        last_day = calendar.monthrange(now.year, now.month)[1]
        if now.day >= last_day:
            next_month_year = now.year + (1 if now.month == 12 else 0)
            next_month = 1 if now.month == 12 else now.month + 1
            last_day_next = calendar.monthrange(next_month_year, next_month)[1]
            target = now.replace(year=next_month_year, month=next_month, day=last_day_next, hour=18, minute=0, second=0, microsecond=0)
        else:
            target = now.replace(day=last_day, hour=18, minute=0, second=0, microsecond=0)
    elif "salary" in text:
        target = now + timedelta(days=3)
    elif "later" in text or "baad" in text:
        target = now + timedelta(hours=6)

    # Time of day adjustments
    if "evening" in text or "shaam" in text or "sham" in text or "raat" in text or "night" in text:
        target = target.replace(hour=18, minute=30, second=0, microsecond=0)
    elif "morning" in text or "subah" in text or "subha" in text:
        target = target.replace(hour=10, minute=0, second=0, microsecond=0)
    elif "afternoon" in text or "dophar" in text or "dopahar" in text:
        target = target.replace(hour=14, minute=0, second=0, microsecond=0)

    if target <= now:
        target = now + timedelta(hours=2)

    return target


def fallback_ai(message: str) -> CustomerIntent:
    """
    Deterministic regex and keyword fallback engine for customer intent reasoning.
    Accurately detects English responses as primary with robust Hinglish support.
    OPT_OUT is checked FIRST to ensure safety terms like 'stop' or 'mat bhejo' are never misclassified.
    """
    text = (message or "").lower()

    # 1. Opt out - STRICTLY CHECKED FIRST BEFORE NEED_HELP
    opt_out_keywords = [
        "stop", "dont contact", "don't contact", "do not contact",
        "opt out", "opt-out", "optout", "unsubscribe", "spam",
        "leave me alone", "dnd", "cancel", "block", "remove me",
        "don't message", "dont message", "stop messaging", "stop calling",
        "do not call", "do not text", "quit", "cease", "never contact",
        "mat karo", "mat bhejo", "message mat karo", "msg mat bhejo", "call mat karo",
        "pareshan mat karo", "band karo", "kabhi mat bhejo", "nahi chahiye",
        "pareshan mat kar", "pareshan mat kijiye", "kripya call na kare",
        "kripya msg na kare", "kripya message na bheje"
    ]
    if any(w in text for w in opt_out_keywords):
        return CustomerIntent(
            intent="OPT_OUT",
            promise_detected=False,
            confidence=0.98,
            recommended_next_action="STOP",
            temporal_expression=None,
            customer_tone="negative"
        )

    # 2. Dispute & Fraud
    dispute_keywords = [
        "dispute", "fraud", "scam", "wrong amount", "wrong charge",
        "incorrect charge", "refund", "cheating", "unauthorized",
        "chargeback", "police", "court", "complaint", "fake", "stolen",
        "didn't authorize", "not my transaction", "never bought",
        "galat", "dhoka", "chor"
    ]
    if any(w in text for w in dispute_keywords):
        return CustomerIntent(
            intent="DISPUTE",
            promise_detected=False,
            confidence=0.92,
            recommended_next_action="ESCALATE",
            temporal_expression=None,
            customer_tone="negative"
        )

    # 3. Payment Completed Claim
    paid_keywords = [
        "already paid", "payment done", "paid yesterday", "payment completed",
        "completed payment", "transferred", "credited", "sent the money",
        "money deducted", "debited from my account", "bank debited",
        "receipt attached", "receipt sent", "check your account",
        "paid via upi", "paid with card", "payment went through",
        "transaction successful", "already debited", "paid earlier",
        "paid", "completed", "kar diya", "kar chuka", "kar diye", "kar di",
        "bheja", "transfer kar", "upi se", "ho gaya", "ho gya"
    ]
    if any(w in text for w in paid_keywords):
        return CustomerIntent(
            intent="PAYMENT_COMPLETED_CLAIM",
            promise_detected=False,
            confidence=0.88,
            recommended_next_action="VERIFY",
            temporal_expression=None,
            customer_tone="neutral"
        )

    # 4. Promise to Pay
    promise_keywords = [
        "promise to pay", "will pay", "will clear", "will settle", "will transfer",
        "pay tomorrow", "pay later", "pay by", "clear by", "settle by",
        "when salary comes", "after salary", "salary day", "month end",
        "tomorrow", "tomorrow morning", "tomorrow evening",
        "pay next week", "next week", "later today",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "salary", "kal", "parso", "parson", "somwar", "mangalwar", "budhwar",
        "guruwar", "shukrawar", "shaniwar", "ravivar", "agla hafta",
        "mahine ke aakhri", "later", "shaam", "sham", "evening", "morning",
        "subah", "kar dunga", "kar dungi", "pay karunga", "pay karungi", "dunga", "dungi"
    ]
    if any(w in text for w in promise_keywords):
        temp_exp = "tomorrow"
        for kw in [
            "parso", "kal", "tomorrow", "salary", "monday", "tuesday", "wednesday",
            "thursday", "friday", "saturday", "sunday", "somwar", "mangalwar",
            "budhwar", "guruwar", "shukrawar", "shaniwar", "ravivar",
            "agla hafta", "next week", "mahine ke aakhri", "month end"
        ]:
            if kw in text:
                temp_exp = kw
                break
        return CustomerIntent(
            intent="PROMISE_TO_PAY",
            promise_detected=True,
            confidence=0.93,
            recommended_next_action="WAIT",
            temporal_expression=temp_exp,
            customer_tone="cooperative"
        )

    # 5. Need Help
    help_keywords = [
        "help", "need assistance", "how to pay", "issue paying", "payment failed",
        "can't pay", "card declined", "expired link", "link not opening",
        "link not working", "send again", "send new link", "resend link",
        "support", "broken link", "give me link", "send payment link", "need link", "link",
        "kaise", "dikkat", "bhejo", "madad"
    ]
    if any(w in text for w in help_keywords):
        return CustomerIntent(
            intent="NEED_HELP",
            promise_detected=False,
            confidence=0.88,
            recommended_next_action="RECOVER",
            temporal_expression=None,
            customer_tone="neutral"
        )

    # 6. Unknown fallback
    return CustomerIntent(
        intent="UNKNOWN",
        promise_detected=False,
        confidence=0.50,
        recommended_next_action="VERIFY",
        temporal_expression=None,
        customer_tone="neutral"
    )


async def groq_intent(message: str) -> tuple[CustomerIntent, str, float]:
    """
    Calls Groq OpenAI-compatible API for structured JSON reasoning.
    Returns (CustomerIntent, 'groq-llm', latency_ms).
    Raises an Exception if call fails, rate-limited, or unconfigured.
    """
    start_time = time.time()

    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY is not configured")

    prompt_system = (
        "You are a strict financial revenue recovery AI. "
        "Detect and analyze customer responses in English only for maximum accuracy. "
        "Classify customer intent and return ONLY a valid JSON object matching this schema: "
        '{"intent": "PROMISE_TO_PAY"|"PAYMENT_COMPLETED_CLAIM"|"NEED_HELP"|"OPT_OUT"|"DISPUTE"|"UNKNOWN", '
        '"promise_detected": boolean, "confidence": float between 0 and 1, '
        '"recommended_next_action": "WAIT"|"VERIFY"|"RECOVER"|"ESCALATE"|"STOP"|"NEEDS_POLICY_REVIEW", '
        '"temporal_expression": string or null, "customer_tone": "cooperative"|"neutral"|"negative"}. '
        "Never invent dates. If intent is PROMISE_TO_PAY, recommended_next_action MUST be WAIT."
    )

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": prompt_system},
            {"role": "user", "content": message},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        validated = CustomerIntent.model_validate(parsed)
        latency = round((time.time() - start_time) * 1000, 2)
        return validated, "groq-llm", latency


async def gemini_intent(message: str) -> tuple[CustomerIntent, str, float]:
    """
    Calls Google Gemini API for structured JSON reasoning.
    Returns (CustomerIntent, 'gemini-llm', latency_ms).
    Raises an Exception if call fails, rate-limited, or unconfigured.
    """
    start_time = time.time()

    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured")

    prompt_instructions = (
        "You are a strict financial revenue recovery AI. "
        "Detect and analyze customer responses in English only for maximum accuracy. "
        "Classify customer intent and return ONLY a valid JSON object matching this schema: "
        '{"intent": "PROMISE_TO_PAY"|"PAYMENT_COMPLETED_CLAIM"|"NEED_HELP"|"OPT_OUT"|"DISPUTE"|"UNKNOWN", '
        '"promise_detected": boolean, "confidence": float between 0 and 1, '
        '"recommended_next_action": "WAIT"|"VERIFY"|"RECOVER"|"ESCALATE"|"STOP"|"NEEDS_POLICY_REVIEW", '
        '"temporal_expression": string or null, "customer_tone": "cooperative"|"neutral"|"negative"}. '
        "Never invent dates. If intent is PROMISE_TO_PAY, recommended_next_action MUST be WAIT.\n\n"
        f"Customer message: {message}"
    )

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    payload = {
        "contents": [{"parts": [{"text": prompt_instructions}]}],
        "generationConfig": {
            "temperature": 0.0,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient(timeout=35) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        candidates = data.get("candidates", [])
        if not candidates:
            raise ValueError("Gemini API returned no candidates")

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise ValueError("Gemini API candidate has no parts")

        raw_text = parts[0].get("text", "").strip()
        if raw_text.startswith("```"):
            lines = raw_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw_text = "\n".join(lines).strip()

        parsed = json.loads(raw_text)
        validated = CustomerIntent.model_validate(parsed)
        latency = round((time.time() - start_time) * 1000, 2)
        return validated, "gemini-llm", latency


async def analyze_intent_resilient(message: str, force_provider: Optional[str] = None) -> tuple[CustomerIntent, str, float]:
    """
    Unified resilient router for intent classification:
    - If force_provider == "groq": call groq_intent(message)
    - If force_provider == "gemini": call gemini_intent(message)
    - If force_provider == "fallback": call fallback_ai(message)
    - If force_provider is None or "auto": Try Groq first. If Groq encounters 429 rate limit,
      quota exhaustion, network error, timeout, or missing key, catch and seamlessly fallback
      to Gemini. If Gemini encounters errors, quota exhaustion, or missing key, catch and
      seamlessly fallback to fallback_ai.
    Always returns standardized provider names: 'groq-llm', 'gemini-llm', or 'fallback-rules'
    and measured latency_ms.
    """
    start_time = time.time()
    provider = (force_provider or "").lower().strip()

    if provider in ("fallback", "fallback-rules", "rules"):
        intent = fallback_ai(message)
        latency_ms = round((time.time() - start_time) * 1000, 2)
        return intent, "fallback-rules", latency_ms

    if provider in ("groq", "groq-llm"):
        return await groq_intent(message)

    if provider in ("gemini", "gemini-llm"):
        return await gemini_intent(message)

    # Auto Mode: Groq -> Gemini -> Fallback
    # 1. Try Groq
    if GROQ_API_KEY:
        try:
            return await groq_intent(message)
        except Exception:
            pass

    # 2. Try Gemini
    if GEMINI_API_KEY:
        try:
            return await gemini_intent(message)
        except Exception:
            pass

    # 3. Tertiary fallback: Deterministic rules
    intent = fallback_ai(message)
    latency_ms = round((time.time() - start_time) * 1000, 2)
    return intent, "fallback-rules", latency_ms


# Contract alias from PROJECT.md
get_intent_with_fallback = analyze_intent_resilient
