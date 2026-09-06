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


class ApprovalStatus(str, Enum):
    PENDING = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"
    EXPIRED = "EXPIRED"


class RecommendationStatus(str, Enum):
    PENDING = "PENDING_APPROVAL"
    SUPERSEDED = "SUPERSEDED"
    CONVERTED = "CONVERTED"


class VerificationStatus(str, Enum):
    MATCHED = "MATCHED"
    NOT_FOUND = "NOT_FOUND"
    PARTIAL = "PARTIAL"
    PENDING = "PENDING"
    ERROR = "ERROR"


class ActionType(str, Enum):
    WAIT = "WAIT"
    VERIFY = "VERIFY"
    RECOVER = "RECOVER"
    ESCALATE = "ESCALATE"
    STOP = "STOP"


class Merchant(Base):
    __tablename__ = "merchants"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    slug: Mapped[str] = mapped_column(String(96), unique=True, index=True)
    environment: Mapped[str] = mapped_column(String(16), default="test")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")
    brand_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    users: Mapped[list["User"]] = relationship(back_populates="merchant")
    cases: Mapped[list["RecoveryCase"]] = relationship(back_populates="merchant")
    customers: Mapped[list["Customer"]] = relationship(back_populates="merchant")
    policies: Mapped[list["RecoveryPolicy"]] = relationship(back_populates="merchant")
    payment_events: Mapped[list["PaymentEvent"]] = relationship(back_populates="merchant")
    integrations: Mapped[list["IntegrationCredential"]] = relationship(back_populates="merchant")
    recommendations: Mapped[list["AIRecommendation"]] = relationship(back_populates="merchant")
    approvals: Mapped[list["ApprovalRequest"]] = relationship(back_populates="merchant")
    verifications: Mapped[list["ProviderVerification"]] = relationship(back_populates="merchant")
    outcomes: Mapped[list["RecoveryOutcome"]] = relationship(back_populates="merchant")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="SET NULL"), nullable=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default=Role.VIEWER.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    merchant: Mapped[Optional[Merchant]] = relationship(back_populates="users")


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (Index("ix_customers_email_created", "email", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="SET NULL"), nullable=True, index=True)
    external_customer_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    email: Mapped[str] = mapped_column(String(255), index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    language_preference: Mapped[str] = mapped_column(String(16), default="en")
    communication_opt_out: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    cases: Mapped[list["RecoveryCase"]] = relationship(back_populates="customer")
    merchant: Mapped[Optional[Merchant]] = relationship(back_populates="customers")


class RecoveryPolicy(Base):
    __tablename__ = "recovery_policies"
    __table_args__ = (Index("ix_recovery_policies_merchant_active", "merchant_id", "active", "id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    version: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    merchant: Mapped[Optional[Merchant]] = relationship(back_populates="policies")


class PaymentEvent(Base):
    __tablename__ = "payment_events"
    __table_args__ = (Index("ix_payment_events_merchant_created", "merchant_id", "created_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    external_event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    case_reference: Mapped[str] = mapped_column(String(128), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    processing_status: Mapped[str] = mapped_column(String(32), default="PROCESSED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    merchant: Mapped[Optional[Merchant]] = relationship(back_populates="payment_events")


class RecoveryCase(Base):
    __tablename__ = "recovery_cases"
    __table_args__ = (
        Index("ix_recovery_cases_state_updated", "state", "updated_at"),
        Index("ix_recovery_cases_merchant_state_updated", "merchant_id", "state", "updated_at"),
        Index("ix_recovery_cases_merchant_customer_updated", "merchant_id", "customer_id_ref", "updated_at"),
        Index("ix_recovery_cases_merchant_created", "merchant_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    case_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_id_ref: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
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
    merchant: Mapped[Optional[Merchant]] = relationship(back_populates="cases")
    subscription: Mapped[Optional["SubscriptionContext"]] = relationship(back_populates="case", cascade="all, delete-orphan", uselist=False)
    invoice: Mapped[Optional["InvoiceContext"]] = relationship(back_populates="case", cascade="all, delete-orphan", uselist=False)
    recommendations: Mapped[list["AIRecommendation"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    approvals: Mapped[list["ApprovalRequest"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    verifications: Mapped[list["ProviderVerification"]] = relationship(back_populates="case", cascade="all, delete-orphan")
    outcomes: Mapped[list["RecoveryOutcome"]] = relationship(back_populates="case", cascade="all, delete-orphan")


class CaseEvent(Base):
    __tablename__ = "case_events"
    __table_args__ = (Index("ix_case_events_case_created", "case_id_ref", "created_at"),)
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
        Index("ix_decision_ledger_created", "created_at"),
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
        Index("ix_action_records_status_created", "status", "created_at"),
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
    __table_args__ = (Index("ix_notifications_case_sent", "case_id_ref", "sent_at"),)
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
        Index("ix_payment_promises_status_promised", "status", "promised_at"),
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
    __table_args__ = (
        Index("ix_scheduled_tasks_status_scheduled_at", "status", "scheduled_at"),
        Index("ix_scheduled_tasks_case_status", "case_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("recovery_cases.case_id", ondelete="CASCADE"), index=True)
    task_type: Mapped[str] = mapped_column(String(64))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)


class SystemHealthEvent(Base):
    __tablename__ = "system_health_events"
    __table_args__ = (Index("ix_system_health_service_created", "service_name", "created_at"),)
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


class IntegrationCredential(Base):
    __tablename__ = "integration_credentials"
    __table_args__ = (Index("ix_integration_credentials_provider_environment", "merchant_id", "provider", "environment"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[int] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="razorpay")
    environment: Mapped[str] = mapped_column(String(16), default="test")
    key_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    secret_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    webhook_secret_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    api_key_hash: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="PENDING")
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    extra_data: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    merchant: Mapped[Merchant] = relationship(back_populates="integrations")


class SubscriptionContext(Base):
    __tablename__ = "subscription_contexts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), default="razorpay")
    external_subscription_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    plan_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    plan_name: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    billing_period: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    billing_cycle: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    payment_method: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    subscription_status: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    current_period_end: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_period_ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    churn_risk: Mapped[float] = mapped_column(Float, default=0.0)
    customer_lifetime_value: Mapped[int] = mapped_column(Integer, default=0)
    extra_data: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    case: Mapped[RecoveryCase] = relationship(back_populates="subscription")


class InvoiceContext(Base):
    __tablename__ = "invoice_contexts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), unique=True, index=True)
    external_invoice_id: Mapped[Optional[str]] = mapped_column(String(128), index=True, nullable=True)
    invoice_number: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    amount_due: Mapped[int] = mapped_column(Integer, default=0)
    amount_paid: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    status: Mapped[str] = mapped_column(String(32), default="OPEN")
    extra_data: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    case: Mapped[RecoveryCase] = relationship(back_populates="invoice")


class AIRecommendation(Base):
    __tablename__ = "ai_recommendations"
    __table_args__ = (Index("ix_ai_recommendations_status_created", "status", "created_at"), Index("ix_ai_recommendations_merchant_status", "merchant_id", "status"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=RecommendationStatus.PENDING.value, index=True)
    intent: Mapped[str] = mapped_column(String(64), default="UNKNOWN")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    risk_flags: Mapped[list[str]] = mapped_column(JSON, default=list)
    recommended_action: Mapped[str] = mapped_column(String(64))
    recommended_channel: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    recommended_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    message_objective: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tone: Mapped[str] = mapped_column(String(32), default="neutral")
    reason_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    approval_level: Mapped[str] = mapped_column(String(32), default="OPERATOR")
    expected_value: Mapped[int] = mapped_column(Integer, default=0)
    disturbance_cost: Mapped[int] = mapped_column(Integer, default=0)
    policy_version: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(64), default="fallback-rules")
    model_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[RecoveryCase] = relationship(back_populates="recommendations")
    merchant: Mapped[Optional[Merchant]] = relationship(back_populates="recommendations")


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    __table_args__ = (Index("ix_approval_requests_status_created", "status", "created_at"), Index("ix_approval_requests_merchant_status", "merchant_id", "status"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), index=True)
    recommendation_id: Mapped[int] = mapped_column(ForeignKey("ai_recommendations.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(32), default=ApprovalStatus.PENDING.value, index=True)
    final_action: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    final_channel: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    edited_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    actor_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(64), default="")
    decided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[RecoveryCase] = relationship(back_populates="approvals")
    recommendation: Mapped[Optional[AIRecommendation]] = relationship()
    merchant: Mapped[Optional[Merchant]] = relationship(back_populates="approvals")


class ProviderVerification(Base):
    __tablename__ = "provider_verifications"
    __table_args__ = (Index("ix_provider_verifications_case_checked", "case_id_ref", "checked_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(32), default="razorpay")
    verification_type: Mapped[str] = mapped_column(String(64))
    external_reference: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=VerificationStatus.PENDING.value)
    request_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    response_status: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[RecoveryCase] = relationship(back_populates="verifications")
    merchant: Mapped[Optional[Merchant]] = relationship(back_populates="verifications")


class RecoveryOutcome(Base):
    __tablename__ = "recovery_outcomes"
    __table_args__ = (Index("ix_recovery_outcomes_created", "created_at"), Index("ix_recovery_outcomes_merchant_experiment", "merchant_id", "experiment_key"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    case_id_ref: Mapped[int] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), index=True)
    recommendation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("ai_recommendations.id", ondelete="SET NULL"), nullable=True)
    approved_action: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    executed_action: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    payment_outcome: Mapped[str] = mapped_column(String(32), default="PENDING")
    recovered_amount: Mapped[int] = mapped_column(Integer, default=0)
    intervention_cost: Mapped[int] = mapped_column(Integer, default=0)
    time_to_recovery_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    experiment_key: Mapped[Optional[str]] = mapped_column(String(96), nullable=True, index=True)
    cohort: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    holdout: Mapped[bool] = mapped_column(Boolean, default=False)
    extra_data: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    case: Mapped[RecoveryCase] = relationship(back_populates="outcomes")
    merchant: Mapped[Optional[Merchant]] = relationship(back_populates="outcomes")


class SuppressionRule(Base):
    __tablename__ = "suppression_rules"
    __table_args__ = (Index("ix_suppression_rules_active_window", "active", "starts_at", "ends_at"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    customer_id_ref: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    case_id_ref: Mapped[Optional[int]] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=True, index=True)
    rule_type: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
    ends_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    extra_data: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)


class ExperimentAssignment(Base):
    __tablename__ = "experiment_assignments"
    __table_args__ = (Index("ix_experiment_assignments_experiment_variant", "experiment_key", "variant"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    merchant_id: Mapped[Optional[int]] = mapped_column(ForeignKey("merchants.id", ondelete="CASCADE"), nullable=True, index=True)
    customer_id_ref: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    case_id_ref: Mapped[Optional[int]] = mapped_column(ForeignKey("recovery_cases.id", ondelete="CASCADE"), nullable=True, index=True)
    experiment_key: Mapped[str] = mapped_column(String(96), index=True)
    variant: Mapped[str] = mapped_column(String(64))
    holdout: Mapped[bool] = mapped_column(Boolean, default=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc)
