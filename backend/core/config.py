import os
from secrets import token_urlsafe
from datetime import timedelta, timezone
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

APP_NAME = "RazorRescue"
COOKIE_NAME = os.getenv("COOKIE_NAME", "razorrescue_session")
RZP_COOKIE_NAME = "rzp_session"

# Production Environment Detection
ENV_NAME = os.getenv("ENVIRONMENT", os.getenv("ENV", os.getenv("NODE_ENV", ""))).strip().lower()
IS_PRODUCTION = bool(
    os.getenv("VERCEL")
    or os.getenv("NOW_REGION")
    or os.getenv("AWS_LAMBDA_FUNCTION_NAME")
    or ENV_NAME in ("production", "prod")
)

# Insecure development secrets blacklist
INSECURE_DEV_SECRETS = frozenset({
    "razorrescue-development-only-change-me",
    "change-me",
    "development",
    "dev",
    "secret",
})

RAW_SECRET = os.getenv("SESSION_SECRET", "").strip()
if IS_PRODUCTION:
    if not RAW_SECRET:
        raise RuntimeError("SESSION_SECRET must be configured in production")
    if RAW_SECRET in INSECURE_DEV_SECRETS or RAW_SECRET.startswith("local-"):
        raise RuntimeError("SESSION_SECRET cannot use default insecure development secret in production")
    SECRET_KEY = RAW_SECRET
else:
    if RAW_SECRET:
        SECRET_KEY = RAW_SECRET
    else:
        SECRET_KEY = f"local-{token_urlsafe(32)}"

# Database connection is supplied only through the deployment environment.
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# AI Models (Groq + Gemini Multi-Provider Resilience)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Razorpay Webhook Configuration
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")

# Clerk Authentication Configuration
CLERK_PUBLISHABLE_KEY = os.getenv(
    "CLERK_PUBLISHABLE_KEY",
    os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "")
)
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")

# Ingest API Key for external event ingestion (/api/events/ingest)
INGEST_API_KEY = os.getenv("INGEST_API_KEY", os.getenv("API_KEY", ""))

# Rate Limiter Configuration
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() not in ("false", "0", "no")
RATE_LIMIT_AUTH = int(os.getenv("RATE_LIMIT_AUTH", "10"))
RATE_LIMIT_AI = int(os.getenv("RATE_LIMIT_AI", "20"))
RATE_LIMIT_INGEST = int(os.getenv("RATE_LIMIT_INGEST", "60"))

# Timezone
APP_TZ = timezone(timedelta(hours=5, minutes=30))  # IST

# Deployment Environment
IS_VERCEL = IS_PRODUCTION

# CORS Origin Configuration - Enforce no wildcard '*' with credentials
DEFAULT_ALLOWED_ORIGINS = ["http://127.0.0.1:8000", "http://localhost:8000"]
raw_origins = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000",
    ).split(",")
    if origin.strip()
]
safe_origins = [origin for origin in raw_origins if origin != "*"]
ALLOWED_ORIGINS = safe_origins if safe_origins else DEFAULT_ALLOWED_ORIGINS
