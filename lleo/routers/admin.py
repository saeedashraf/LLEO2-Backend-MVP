"""
Admin endpoints (guarded by the ``X-Admin-Key`` header == ``ADMIN_KEY`` env).

    POST /api/admin/migrate-supabase   one-shot Supabase → Cloud SQL data copy
    GET  /api/admin/stats              row counts per table (sanity check)
"""
from __future__ import annotations

import os
import secrets
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func

from ..database import models as db_models
from ..database.models import (
    AnalysisLike,
    AnalysisRecord,
    ConversationMessage,
    ConversationStateRecord,
    CreditTransaction,
    PaymentHistory,
    Subscription,
    UserCredits,
)
from ..services import supabase_migration

router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(x_admin_key: Optional[str] = Header(default=None)) -> None:
    # Secret Manager values often carry a trailing newline; headers never can.
    expected = os.getenv("ADMIN_KEY", "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="ADMIN_KEY not configured on server")
    if not x_admin_key or not secrets.compare_digest(x_admin_key.strip(), expected):
        raise HTTPException(status_code=401, detail="Invalid admin key")


@router.get("/stats", dependencies=[Depends(require_admin)])
def stats():
    if db_models.SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    s = db_models.SessionLocal()
    try:
        def count(model):
            return s.query(func.count()).select_from(model).scalar() or 0

        return {
            "analyses": count(AnalysisRecord),
            "shared_analyses": s.query(func.count()).select_from(AnalysisRecord)
                .filter(AnalysisRecord.is_shared.is_(True)).scalar() or 0,
            "analysis_likes": count(AnalysisLike),
            "conversation_messages": count(ConversationMessage),
            "conversation_states": count(ConversationStateRecord),
            "user_credits": count(UserCredits),
            "credit_transactions": count(CreditTransaction),
            "subscriptions": count(Subscription),
            "payment_history": count(PaymentHistory),
        }
    finally:
        s.close()


@router.post("/migrate-supabase", dependencies=[Depends(require_admin)])
async def migrate_supabase(tables: Optional[List[str]] = Body(default=None, embed=True)):
    """Copy Supabase data tables into Cloud SQL (idempotent, safe to re-run)."""
    from ..supabase_client import supabase_client  # lazy: optional dependency

    if supabase_client.client is None:
        raise HTTPException(
            status_code=503,
            detail="Supabase client not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)",
        )
    try:
        report = await run_in_threadpool(
            supabase_migration.run_migration, supabase_client.client, tables
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Migration failed: {exc}")
    return {"success": True, "report": report}
