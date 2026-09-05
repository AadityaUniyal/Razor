"""
RazorRescue — AI Revenue Recovery Orchestrator
Root Application Entrypoint

Re-exports modular components from:
- database/ (connection, models, schema initialization)
- backend/ (routes, services, security, core config)
- frontend/ (UI templates, styles, app state)
"""
from backend.main import app
from database.connection import Base, SessionLocal, engine, get_db, get_db_context
from database.models import (
    User, Customer, RecoveryPolicy, PaymentEvent, RecoveryCase, CaseEvent,
    DecisionLedger, ActionRecord, Notification, PaymentPromise, ScheduledTask,
    SystemHealthEvent, AIEvaluationRecord,
    Role, CaseState, ActionType, now_utc
)
from database.init_db import init_db, seed_data
from backend.services.ai_agent import (
    resolve_temporal_expression, fallback_ai, groq_intent, CustomerIntent
)
from backend.services.policy_engine import (
    process_case, record_action, mark_recovered, to_case_dict, latest_policy
)
from backend.services.strategy_scorer import calculate_strategy_scores

__all__ = [
    "app", "Base", "SessionLocal", "engine", "get_db", "get_db_context",
    "User", "Customer", "RecoveryPolicy", "PaymentEvent", "RecoveryCase", "CaseEvent",
    "DecisionLedger", "ActionRecord", "Notification", "PaymentPromise", "ScheduledTask",
    "SystemHealthEvent", "AIEvaluationRecord",
    "Role", "CaseState", "ActionType", "now_utc",
    "init_db", "seed_data", "resolve_temporal_expression", "fallback_ai",
    "groq_intent", "CustomerIntent", "process_case", "record_action",
    "mark_recovered", "to_case_dict", "latest_policy", "calculate_strategy_scores"
]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
