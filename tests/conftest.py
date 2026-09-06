import pytest
from backend.core.rate_limiter import default_rate_limiter

@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Resets sliding window rate limiter state before each test to prevent cross-test rate limit throttling."""
    default_rate_limiter.reset()
    yield
    default_rate_limiter.reset()
