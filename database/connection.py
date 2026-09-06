from contextlib import contextmanager
import os
import time
from typing import Any, Dict

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase

from dotenv import load_dotenv

load_dotenv()

# Neon PostgreSQL cloud database connection.
# Connection string is loaded strictly from environment variables.
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Normalise URL for psycopg driver if standard postgresql:// prefix is given
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

# Keep local startup responsive when the managed database is temporarily asleep
# or unreachable. Callers still receive the database error, but the web process
# does not remain blocked indefinitely while opening a connection.
if DATABASE_URL.startswith("postgresql") and "connect_timeout=" not in DATABASE_URL:
    DATABASE_URL = f"{DATABASE_URL}&connect_timeout=5" if "?" in DATABASE_URL else f"{DATABASE_URL}?connect_timeout=5"

is_serverless = bool(os.getenv("VERCEL") or os.getenv("NOW_REGION") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

engine_kwargs = {
    "future": True,
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

if is_serverless:
    # Serverless lambdas should keep a lean pool to prevent exhausting Neon connection limits
    engine_kwargs.update({"pool_size": 3, "max_overflow": 5, "pool_timeout": 10})
else:
    engine_kwargs.update({"pool_size": 10, "max_overflow": 20, "pool_timeout": 30})

# LIFO reuses warm connections first and lets idle overflow connections expire
# naturally, which is a better fit for Neon and bursty dashboard traffic.
engine_kwargs["pool_use_lifo"] = True

engine = create_engine(DATABASE_URL, **engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency yielding a managed session."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def get_db_context():
    """Context manager for standalone scripts and background tasks."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_db_health() -> Dict[str, Any]:
    """
    Checks connection health to Neon PostgreSQL cloud database.
    Tests live connection, computes latency_ms, captures pool statistics,
    and returns status ('CONNECTED' or 'DISCONNECTED') with diagnostics.
    """
    start_time = time.perf_counter()
    db_name = engine.url.database or "neondb"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1;"))
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        pool = engine.pool
        pool_stats = {
            "size": pool.size() if hasattr(pool, "size") else 0,
            "checkedin": pool.checkedin() if hasattr(pool, "checkedin") else 0,
            "checkedout": pool.checkedout() if hasattr(pool, "checkedout") else 0,
            "overflow": pool.overflow() if hasattr(pool, "overflow") else 0,
        }
        return {
            "status": "CONNECTED",
            "latency_ms": latency_ms,
            "database": db_name,
            "pool": pool_stats,
            "serverless": "Neon AWS us-east-2",
        }
    except Exception as e:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        pool = getattr(engine, "pool", None)
        pool_stats = {
            "size": pool.size() if pool and hasattr(pool, "size") else 0,
            "checkedin": pool.checkedin() if pool and hasattr(pool, "checkedin") else 0,
            "checkedout": pool.checkedout() if pool and hasattr(pool, "checkedout") else 0,
            "overflow": pool.overflow() if pool and hasattr(pool, "overflow") else 0,
        }
        return {
            "status": "DISCONNECTED",
            "latency_ms": latency_ms,
            "error": str(e),
            "database": db_name,
            "pool": pool_stats,
            "serverless": "Neon AWS us-east-2",
        }
