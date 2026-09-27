import os
import pytest

# Ensure safe deterministic test environment variables before module imports
if not os.getenv("DATABASE_URL"):
    os.environ["DATABASE_URL"] = "sqlite:///./test.db"
if not os.getenv("SESSION_SECRET"):
    os.environ["SESSION_SECRET"] = "ci-test-session-secret-deterministic-32b"
if not os.getenv("ENVIRONMENT"):
    os.environ["ENVIRONMENT"] = "testing"

from database.init_db import init_db
init_db()

from backend.core.rate_limiter import default_rate_limiter


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Resets sliding window rate limiter state before each test to prevent cross-test rate limit throttling."""
    default_rate_limiter.reset()
    yield
    default_rate_limiter.reset()
