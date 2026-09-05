import os
from datetime import timedelta, timezone
from dotenv import load_dotenv

# Load environment variables from .env if present
load_dotenv()

APP_NAME = "RazorRescue"
COOKIE_NAME = "razorrescue_session"
SECRET_KEY = os.getenv("SESSION_SECRET", "razorrescue-development-only-change-me")

# Neon PostgreSQL connection string (Official project database)
DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# AI Models (Groq + Gemini Multi-Provider Resilience)
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Razorpay Webhook Configuration
RAZORPAY_WEBHOOK_SECRET = os.getenv("RAZORPAY_WEBHOOK_SECRET", "test_webhook_secret_key")

# Clerk Authentication Configuration
CLERK_PUBLISHABLE_KEY = os.getenv(
    "CLERK_PUBLISHABLE_KEY",
    os.getenv("NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY", "")
)
CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")

# Timezone
APP_TZ = timezone(timedelta(hours=5, minutes=30))  # IST

# Deployment Environment
IS_VERCEL = bool(os.getenv("VERCEL") or os.getenv("NOW_REGION") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

