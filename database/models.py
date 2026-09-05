from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from sqlalchemy import (
    JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.connection import Base


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, Enum):
    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    VIEWER = "VIEWER"


class CaseState(str, Enum):
    NEW = "NEW"
    VERIFY = "VERIFY"
    WAIT = "WAIT"
    RECOVER = "RECOVER"
    STOP = "STOP"
    ESCALATED = "ESCALATED"
    RECOVERED = "RECOVERED"


class BatchState(str, Enum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"


class ActionType(str, Enum):
    WAIT = "WAIT"
    VERIFY = "VERIFY"
    RECOVER = "RECOVER"
    ESCALATE = "ESCALATE"
    STOP = "STOP"


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default=Role.VIEWER.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_customer_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    language_preference: Mapped[str] = mapped_column(String(16), default="en")
    communication_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    cases: Mapped[list["RecoveryCase"]] = relationship(back_populates="customer")


class RecoveryPolicy(Base):
    __tablename__ = "recovery_policies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    case_reference: Mapped[str] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    processing_status: Mapped[str] = mapped_column(String(32), default="PROCESSED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class RecoveryCase(Base):
    __tablename__ = "recovery_cases"
    __table_args__ = (
        Index("ix_recovery_cases_state_updated", "state", "updated_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id_ref: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True)
    customer_name: Mapped[str] = mapped_column(String(128))
    customer_email: Mapped[str] = mapped_column(String(255))
    external_payment_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    external_subscription_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    amount: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    state: Mapped[str] = mapped_column(String(32), default=CaseState.NEW.value, index=True)
    failure_category: Mapped[str] = mapped_column(String(64), default="UNKNOWN")
    current_action: Mapped[str] = mapped_column(String(64), default="NONE")
    policy_version: Mapped[str] = mapped_column(String(32), default="Default Recovery Policy v1.2")
    strategy_score: Mapped[int] = mapped_column(Integer, default=0)
    total_intervention_cost: Mapped[int] = mapped_column(Integer, default=0)
    net_recovery_value: Mapped[int] = mapped_column(Integer, default=0)
    promise_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    communication_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    ai_status: Mapped[str] = mapped_column(String(64), default="UNUSED")
    recovered_amount: Mapped[int] = mapped_column(Integer, default=0)
    recovered: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, onupdate=now_utc)

    customer: Mapped[Optional[Customer]] = relationship(back_populates="cases")
    events: Mapped[list["CaseEvent"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    decisions: Mapped[list["DecisionLedger"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    actions: Mapped[list["ActionRecord"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    notifications: Mapped[list["Notification"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    promise: Mapped[Optional["PaymentPromise"]] = relationship(back_populates="case", cascade="all, delete-orphan", uselist=False)


class CaseEvent(Base):
    __tablename__ = "case_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[RecoveryCase] = relationship(back_populates="events")


class DecisionLedger(Base):
    __tablename__ = "decision_ledger"
    __table_args__ = (
        Index("ix_decision_ledger_case_created", "case_id_ref", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), index=True)
    policy_version: Mapped[str] = mapped_column(String(64))
    available_actions: Mapped[list[str]] = mapped_column(JSON)
    strategy_scores: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    selected_action: Mapped[str] = mapped_column(String(64))
    deterministic_factors: Mapped[dict[str, Any]] = mapped_column(JSON)
    ai_analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ai_recommendation: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_result: Mapped[str] = mapped_column(String(32))
    policy_reason: Mapped[str] = mapped_column(Text)
    final_outcome: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    llm_provider: Mapped[str] = mapped_column(String(64), default="fallback-rules")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[RecoveryCase] = relationship(back_populates="decisions")


class ActionRecord(Base):
    __tablename__ = "action_records"
    __table_args__ = (
        Index("ix_action_records_case_created", "case_id_ref", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    channel: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    cost: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[RecoveryCase] = relationship(back_populates="actions")


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    recipient: Mapped[str] = mapped_column(String(255))
    message_content: Mapped[str] = mapped_column(Text)
    cost: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="SENT")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[RecoveryCase] = relationship(back_populates="notifications")


class PaymentPromise(Base):
    __tablename__ = "payment_promises"
    __table_args__ = (
        Index("ix_payment_promises_promised_status", "promised_at", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), unique=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(128), default="")
    amount: Mapped[int] = mapped_column(Integer, default=0)
    promise_text: Mapped[str] = mapped_column(Text)
    promised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.9)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    follow_up_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    fulfilled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[Optional["RecoveryCase"]] = relationship(back_populates="promise")


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("recovery_cases.case_id", ondelete="CASCADE"), index=True)
    task_type: Mapped[str] = mapped_column(String(64))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class SystemHealthEvent(Base):
    __tablename__ = "system_health_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_name: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


# Deprecated demo models purged from ORM database schema
SimulationScenario = None
DemoBatch = None


class AIEvaluationRecord(Base):
    __tablename__ = "ai_evaluation_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64))
    customer_message: Mapped[str] = mapped_column(Text)
    expected_intent: Mapped[str] = mapped_column(String(64))
    predicted_intent: Mapped[str] = mapped_column(String(64))
    expected_action: Mapped[str] = mapped_column(String(64))
    suggested_action: Mapped[str] = mapped_column(String(64))
    policy_result: Mapped[str] = mapped_column(String(32))
    confidence: Mapped[float] = mapped_column(Float)
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    llm_provider: Mapped[str] = mapped_column(String(64), default="fallback-rules")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
