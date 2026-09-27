"""Tenant resolution helpers shared by API routes and background workflows."""

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import Merchant, User


def default_merchant(db: Session) -> Merchant:
    merchant = db.scalar(select(Merchant).order_by(Merchant.id))
    if merchant:
        return merchant
    merchant = Merchant(name="RazorRescue Merchant", slug="default-merchant", environment="test")
    db.add(merchant)
    db.flush()
    return merchant


def merchant_id_for_user(db: Session, user: Optional[User]) -> int:
    if user and user.merchant_id:
        return user.merchant_id
    return default_merchant(db).id


def tenant_filter(model, merchant_id: int):
    """Enforce strict multi-tenant isolation by filtering exclusively by merchant_id."""
    return model.merchant_id == merchant_id


def assign_legacy_tenant(db: Session, merchant_id: int) -> None:
    """Backfill only records that predate tenant support."""
    for model in (User,):
        db.query(model).filter(model.merchant_id.is_(None)).update({"merchant_id": merchant_id})
