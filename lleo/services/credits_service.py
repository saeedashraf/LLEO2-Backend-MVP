"""
Credits / subscription / payment persistence in Cloud SQL.

This module replaces the Supabase tables `user_credits`, `credit_transactions`,
`subscriptions`, `payment_history` and the `add_credits` / `deduct_credits`
RPC functions.  Supabase is now used for authentication only.

All functions open their own short-lived SQLAlchemy session unless a session
is passed in, so they can be called from Stripe webhooks, background tasks
and FastAPI endpoints alike.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from sqlalchemy.orm import Session

from ..database import models as db_models
from ..database.models import (
    SIGNUP_CREDITS,
    CreditTransaction,
    PaymentHistory,
    Subscription,
    UserCredits,
)


# ---------------------------------------------------------------------------
# Session helper
# ---------------------------------------------------------------------------
@contextmanager
def _session(db: Optional[Session] = None) -> Iterator[Session]:
    """Yield the given session, or a fresh one that is committed/closed here."""
    if db is not None:
        yield db
        return
    if db_models.SessionLocal is None:
        raise RuntimeError("Database not initialised")
    s = db_models.SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_ts(value) -> Optional[datetime]:
    """Accept datetime, ISO string or unix timestamp (int/float)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Serialisers (shape matches what the frontend already expects)
# ---------------------------------------------------------------------------
def credits_to_dict(row: UserCredits) -> Dict[str, Any]:
    return {
        "user_id": row.user_id,
        "credits": int(row.credits or 0),
        "total_used": int(row.total_used or 0),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def subscription_to_dict(row: Subscription) -> Dict[str, Any]:
    return {
        "user_id": row.user_id,
        "user_email": row.user_email,
        "stripe_customer_id": row.stripe_customer_id,
        "stripe_subscription_id": row.stripe_subscription_id,
        "stripe_price_id": row.stripe_price_id,
        "status": row.status,
        "plan_type": row.plan_type,
        "current_period_start": _iso(row.current_period_start),
        "current_period_end": _iso(row.current_period_end),
        "cancel_at_period_end": bool(row.cancel_at_period_end),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def transaction_to_dict(row: CreditTransaction) -> Dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "amount": row.amount,
        "transaction_type": row.transaction_type,
        "description": row.description,
        "analysis_id": row.analysis_id,
        "balance_after": row.balance_after,
        "created_at": _iso(row.created_at),
    }


def payment_to_dict(row: PaymentHistory) -> Dict[str, Any]:
    return {
        "id": row.id,
        "user_id": row.user_id,
        "stripe_payment_intent_id": row.stripe_payment_intent_id,
        "stripe_invoice_id": row.stripe_invoice_id,
        "amount": row.amount,
        "currency": row.currency,
        "status": row.status,
        "description": row.description,
        "created_at": _iso(row.created_at),
    }


# ---------------------------------------------------------------------------
# Credits
# ---------------------------------------------------------------------------
def _lock_credits_row(s: Session, user_id: str) -> Optional[UserCredits]:
    """SELECT ... FOR UPDATE on Postgres; plain select on SQLite."""
    q = s.query(UserCredits).filter(UserCredits.user_id == user_id)
    if s.bind is not None and s.bind.dialect.name == "postgresql":
        q = q.with_for_update()
    return q.first()


def ensure_user_credits(user_id: str, db: Optional[Session] = None) -> Dict[str, Any]:
    """Return the credit row for a user, creating it with the signup bonus if missing."""
    with _session(db) as s:
        row = _lock_credits_row(s, user_id)
        if row is None:
            row = UserCredits(user_id=user_id, credits=SIGNUP_CREDITS, total_used=0)
            s.add(row)
            s.flush()
            if SIGNUP_CREDITS:
                s.add(CreditTransaction(
                    user_id=user_id,
                    amount=SIGNUP_CREDITS,
                    transaction_type="signup_bonus",
                    description="Welcome bonus",
                    balance_after=SIGNUP_CREDITS,
                ))
            s.flush()
        return credits_to_dict(row)


def get_user_credits(user_id: str, db: Optional[Session] = None) -> Optional[Dict[str, Any]]:
    """Read-only lookup (None if the user has never been seen)."""
    with _session(db) as s:
        row = s.query(UserCredits).filter(UserCredits.user_id == user_id).first()
        return credits_to_dict(row) if row else None


def add_credits(
    user_id: str,
    amount: int,
    transaction_type: str = "subscription_renewal",
    description: str = "Monthly credit allocation",
    db: Optional[Session] = None,
) -> bool:
    if amount <= 0:
        return False
    try:
        with _session(db) as s:
            row = _lock_credits_row(s, user_id)
            if row is None:
                row = UserCredits(user_id=user_id, credits=0, total_used=0)
                s.add(row)
                s.flush()
            row.credits = int(row.credits or 0) + amount
            s.add(CreditTransaction(
                user_id=user_id,
                amount=amount,
                transaction_type=transaction_type,
                description=description,
                balance_after=row.credits,
            ))
            s.flush()
            print(f"✅ Added {amount} credits to user {user_id} (balance {row.credits})")
            return True
    except Exception as e:
        print(f"❌ Error adding credits: {e}")
        return False


def deduct_credits(
    user_id: str,
    amount: int = 1,
    analysis_id: Optional[str] = None,
    description: str = "Analysis usage",
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """Atomically deduct credits.

    Returns ``{"success": bool, "credits": balance, "error": str|None}``.
    Never goes negative; if the balance is insufficient nothing is written.
    Deducting twice for the same ``analysis_id`` is a no-op (idempotent for
    retried completion callbacks).
    """
    if amount <= 0:
        return {"success": False, "error": "amount must be positive"}
    try:
        with _session(db) as s:
            if analysis_id:
                dup = (
                    s.query(CreditTransaction)
                    .filter(
                        CreditTransaction.user_id == user_id,
                        CreditTransaction.analysis_id == analysis_id,
                        CreditTransaction.transaction_type == "analysis_usage",
                    )
                    .first()
                )
                if dup is not None:
                    bal = s.query(UserCredits).filter(UserCredits.user_id == user_id).first()
                    return {"success": True, "credits": int(bal.credits) if bal else 0,
                            "error": None, "already_charged": True}

            row = _lock_credits_row(s, user_id)
            if row is None:
                row = UserCredits(user_id=user_id, credits=SIGNUP_CREDITS, total_used=0)
                s.add(row)
                s.flush()

            if int(row.credits or 0) < amount:
                return {"success": False, "credits": int(row.credits or 0),
                        "error": "Insufficient credits"}

            row.credits = int(row.credits) - amount
            row.total_used = int(row.total_used or 0) + amount
            s.add(CreditTransaction(
                user_id=user_id,
                amount=-amount,
                transaction_type="analysis_usage",
                description=description,
                analysis_id=analysis_id,
                balance_after=row.credits,
            ))
            s.flush()
            print(f"✅ Deducted {amount} credit(s) from user {user_id} (balance {row.credits})")
            return {"success": True, "credits": row.credits, "error": None}
    except Exception as e:
        print(f"❌ Error deducting credits: {e}")
        return {"success": False, "error": str(e)}


def list_transactions(user_id: str, limit: int = 10, db: Optional[Session] = None) -> List[Dict[str, Any]]:
    with _session(db) as s:
        rows = (
            s.query(CreditTransaction)
            .filter(CreditTransaction.user_id == user_id)
            .order_by(CreditTransaction.created_at.desc(), CreditTransaction.id.desc())
            .limit(limit)
            .all()
        )
        return [transaction_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Subscriptions
# ---------------------------------------------------------------------------
def update_subscription(
    user_id: str,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    stripe_price_id: Optional[str] = None,
    status: Optional[str] = None,
    plan_type: Optional[str] = None,
    current_period_start=None,
    current_period_end=None,
    cancel_at_period_end: Optional[bool] = None,
    user_email: Optional[str] = None,
    db: Optional[Session] = None,
) -> bool:
    """UPSERT keyed on user_id. Only provided fields are changed."""
    try:
        with _session(db) as s:
            row = s.query(Subscription).filter(Subscription.user_id == user_id).first()
            if row is None:
                row = Subscription(user_id=user_id, status="inactive", plan_type="free")
                s.add(row)
            if stripe_customer_id:
                row.stripe_customer_id = stripe_customer_id
            if stripe_subscription_id:
                row.stripe_subscription_id = stripe_subscription_id
            if stripe_price_id:
                row.stripe_price_id = stripe_price_id
            if status:
                row.status = status
            if plan_type:
                row.plan_type = plan_type
            if current_period_start is not None:
                row.current_period_start = _parse_ts(current_period_start)
            if current_period_end is not None:
                row.current_period_end = _parse_ts(current_period_end)
            if cancel_at_period_end is not None:
                row.cancel_at_period_end = bool(cancel_at_period_end)
            if user_email:
                row.user_email = user_email
            s.flush()
            print(f"✅ Upserted subscription for user {user_id}")
            return True
    except Exception as e:
        print(f"❌ Error upserting subscription: {e}")
        return False


def get_subscription(user_id: str, db: Optional[Session] = None) -> Optional[Dict[str, Any]]:
    with _session(db) as s:
        row = s.query(Subscription).filter(Subscription.user_id == user_id).first()
        return subscription_to_dict(row) if row else None


def get_subscription_by_stripe_id(stripe_subscription_id: str, db: Optional[Session] = None) -> Optional[Dict[str, Any]]:
    with _session(db) as s:
        row = (
            s.query(Subscription)
            .filter(Subscription.stripe_subscription_id == stripe_subscription_id)
            .first()
        )
        return subscription_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------
def create_payment_record(
    user_id: str,
    stripe_payment_intent_id: Optional[str] = None,
    stripe_invoice_id: Optional[str] = None,
    amount: int = 0,
    currency: str = "usd",
    status: str = "succeeded",
    description: Optional[str] = None,
    db: Optional[Session] = None,
) -> bool:
    try:
        with _session(db) as s:
            if stripe_invoice_id:
                exists = (
                    s.query(PaymentHistory)
                    .filter(PaymentHistory.stripe_invoice_id == stripe_invoice_id,
                            PaymentHistory.status == status)
                    .first()
                )
                if exists:
                    return True  # webhook retry — already recorded
            s.add(PaymentHistory(
                user_id=user_id,
                stripe_payment_intent_id=stripe_payment_intent_id,
                stripe_invoice_id=stripe_invoice_id,
                amount=int(amount or 0),
                currency=currency,
                status=status,
                description=description,
            ))
            s.flush()
            print(f"✅ Payment record created for user {user_id}")
            return True
    except Exception as e:
        print(f"❌ Error creating payment record: {e}")
        return False


def list_payments(user_id: str, limit: int = 20, db: Optional[Session] = None) -> List[Dict[str, Any]]:
    with _session(db) as s:
        rows = (
            s.query(PaymentHistory)
            .filter(PaymentHistory.user_id == user_id)
            .order_by(PaymentHistory.created_at.desc())
            .limit(limit)
            .all()
        )
        return [payment_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Aggregate used by the Billing page
# ---------------------------------------------------------------------------
def billing_summary(user_id: str, db: Optional[Session] = None) -> Dict[str, Any]:
    with _session(db) as s:
        credits = ensure_user_credits(user_id, db=s)
        subscription = get_subscription(user_id, db=s)
        transactions = list_transactions(user_id, limit=10, db=s)
        payments = list_payments(user_id, limit=20, db=s)
    return {
        "credits": credits,
        "subscription": subscription,
        "transactions": transactions,
        "payments": payments,
    }
