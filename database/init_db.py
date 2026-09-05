import os
from hashlib import pbkdf2_hmac
from secrets import token_hex
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from database.connection import engine, Base, SessionLocal
from database.models import (
    User, Customer, RecoveryPolicy, PaymentEvent, RecoveryCase, CaseEvent,
    DecisionLedger, ActionRecord, Notification, PaymentPromise, ScheduledTask,
    SystemHealthEvent, AIEvaluationRecord,
    Role, CaseState
)




def hash_password(password: str) -> str:
    salt = token_hex(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def seed_data(db: Session) -> None:
    # 1. Admin User
    if db.scalar(select(func.count(User.id))) == 0:
        initial_pwd = os.getenv("ADMIN_INITIAL_PASSWORD", "AdminSecure#2026")
        db.add(User(
            email="admin@razorrescue.local",
            password_hash=hash_password(initial_pwd),
            role=Role.ADMIN.value,
        ))

    # 2. Recovery Policy
    if db.scalar(select(func.count(RecoveryPolicy.id))) == 0:
        db.add(RecoveryPolicy(
            version="v1.2",
            name="Default Recovery Policy v1.2",
            configuration={
                "maximum_automated_attempts": 3,
                "maximum_customer_communications": 3,
                "minimum_amount_for_human_escalation": 10000,
                "promise_grace_hours": 2,
                "enable_ai_promise_detection": True,
                "simulation_mode": True,
                "ai_confidence_threshold": 0.70,
                "channel_costs": {
                    "VERIFY": 0,
                    "WAIT": 0,
                    "EMAIL": 1,
                    "WHATSAPP": 2,
                    "SMS": 3,
                    "RECOVERY_LINK": 2,
                    "ESCALATE": 100,
                },
            },
            active=True,
        ))


    db.commit()


_db_initialized = False


def init_db(force: bool = False):
    """Initializes and migrates the database schema on Neon PostgreSQL and seeds initial records."""
    global _db_initialized
    if _db_initialized and not force:
        return

    from sqlalchemy import text

    # Step 1: Ensure all tables and base schemas exist first
    Base.metadata.create_all(bind=engine)

    # Step 2: Non-destructive DDL migrations & compound index creations
    with engine.connect() as conn:
        conn.execute(text("""
            -- Column migrations
            ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;
            ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS customer_id_ref INTEGER;
            ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS external_payment_id VARCHAR(128);
            ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS external_subscription_id VARCHAR(128);
            ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS strategy_score INTEGER DEFAULT 0;
            ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS total_intervention_cost INTEGER DEFAULT 0;
            ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS net_recovery_value INTEGER DEFAULT 0;
            ALTER TABLE recovery_cases ADD COLUMN IF NOT EXISTS closed_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE payment_events ADD COLUMN IF NOT EXISTS signature_valid BOOLEAN DEFAULT TRUE;
            ALTER TABLE payment_events ADD COLUMN IF NOT EXISTS processing_status VARCHAR(32) DEFAULT 'PROCESSED';
            ALTER TABLE action_records ADD COLUMN IF NOT EXISTS channel VARCHAR(32);
            ALTER TABLE action_records ADD COLUMN IF NOT EXISTS cost INTEGER DEFAULT 0;
            ALTER TABLE payment_promises ADD COLUMN IF NOT EXISTS customer_name VARCHAR(128) DEFAULT '';
            ALTER TABLE payment_promises ADD COLUMN IF NOT EXISTS amount INTEGER DEFAULT 0;
            ALTER TABLE payment_promises ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION DEFAULT 0.9;
            ALTER TABLE payment_promises ADD COLUMN IF NOT EXISTS fulfilled_at TIMESTAMP WITH TIME ZONE;
            ALTER TABLE decision_ledger ADD COLUMN IF NOT EXISTS strategy_scores JSON DEFAULT '{}';
            ALTER TABLE decision_ledger ADD COLUMN IF NOT EXISTS llm_provider VARCHAR(64) DEFAULT 'fallback-rules';
            ALTER TABLE ai_evaluation_records ADD COLUMN IF NOT EXISTS llm_provider VARCHAR(64) DEFAULT 'fallback-rules';

            -- Compound & Performance Indexes
            CREATE INDEX IF NOT EXISTS ix_decision_ledger_case_created ON decision_ledger (case_id_ref, created_at);
            CREATE INDEX IF NOT EXISTS ix_action_records_case_created ON action_records (case_id_ref, created_at);
            CREATE INDEX IF NOT EXISTS ix_recovery_cases_state_updated ON recovery_cases (state, updated_at);
            CREATE INDEX IF NOT EXISTS ix_payment_promises_promised_status ON payment_promises (promised_at, status);
            CREATE UNIQUE INDEX IF NOT EXISTS ix_payment_events_external_event_id ON payment_events (external_event_id);
        """))
        conn.commit()

        # Step 3: Upgrade existing foreign keys to ON DELETE CASCADE / SET NULL using DO $$ blocks
        foreign_key_specs = [
            ("case_events", "case_id_ref", "recovery_cases", "id", "CASCADE"),
            ("decision_ledger", "case_id_ref", "recovery_cases", "id", "CASCADE"),
            ("action_records", "case_id_ref", "recovery_cases", "id", "CASCADE"),
            ("payment_promises", "case_id_ref", "recovery_cases", "id", "CASCADE"),
            ("notifications", "case_id_ref", "recovery_cases", "id", "CASCADE"),
            ("scheduled_tasks", "case_id", "recovery_cases", "case_id", "CASCADE"),
            ("recovery_cases", "customer_id_ref", "customers", "id", "SET NULL"),
        ]

        for child_table, fk_col, ref_table, ref_col, on_delete_action in foreign_key_specs:
            constraint_name = f"fk_{child_table}_{fk_col}"
            conn.execute(text(f"""
                DO $$
                DECLARE
                    r RECORD;
                    has_matching_rule BOOLEAN := FALSE;
                BEGIN
                    FOR r IN (
                        SELECT tc.constraint_name, rc.delete_rule
                        FROM information_schema.table_constraints tc
                        JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name
                        JOIN information_schema.referential_constraints rc ON tc.constraint_name = rc.constraint_name
                        WHERE tc.constraint_type = 'FOREIGN KEY'
                          AND tc.table_schema = 'public'
                          AND tc.table_name = '{child_table}'
                          AND kcu.column_name = '{fk_col}'
                    ) LOOP
                        IF r.delete_rule = '{on_delete_action}' THEN
                            has_matching_rule := TRUE;
                        ELSE
                            EXECUTE 'ALTER TABLE {child_table} DROP CONSTRAINT ' || quote_ident(r.constraint_name);
                        END IF;
                    END LOOP;

                    IF NOT has_matching_rule THEN
                        BEGIN
                            ALTER TABLE {child_table} ADD CONSTRAINT {constraint_name}
                                FOREIGN KEY ({fk_col}) REFERENCES {ref_table}({ref_col}) ON DELETE {on_delete_action};
                        EXCEPTION WHEN duplicate_object THEN
                            NULL;
                        END;
                    END IF;
                END $$;
            """))
        conn.commit()

    with SessionLocal() as db:
        seed_data(db)

    _db_initialized = True


if __name__ == "__main__":
    print("Initializing Neon PostgreSQL database schema...")
    init_db()
    print("Database initialization complete.")
