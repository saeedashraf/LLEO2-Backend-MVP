"""
LLEO v2 — Application-data endpoints (Cloud SQL).

These replace every direct Supabase table/RPC call the frontend used to make.
Supabase is now authentication only.

GET    /api/analyses?user_id&page&per_page         — My Analyses (paginated)
PATCH  /api/analyses/{session_id}/share            — toggle sharing for one analysis
PATCH  /api/conversations/{conversation_id}/share  — toggle sharing for a whole conversation
GET    /api/community?page&per_page[&user_id]      — shared analyses (+ which ones I liked)
POST   /api/analyses/{session_id}/like             — toggle like
GET    /api/conversations/{conversation_id}/messages
GET    /api/credits/{user_id}                      — balance (row auto-created)
POST   /api/credits/deduct                         — manual deduction (idempotent per analysis_id)
GET    /api/billing/summary/{user_id}              — credits + subscription + transactions + payments
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database.models import AnalysisLike, AnalysisRecord, get_db
from ..services import credits_service
from ..services.analysis_service import build_file_urls
from ..services.conversation_service import list_messages, normalise_conversation_id

router = APIRouter(prefix="/api", tags=["data"])

_BACKEND_URL = os.getenv(
    "BACKEND_URL",
    "https://ndvi6-analysis-api-258557095482.us-central1.run.app",
).rstrip("/")

MAX_PER_PAGE = 100


# ── Serialiser ───────────────────────────────────────────────────────────────

def _abs(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"{_BACKEND_URL}{url}"


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def display_name_for(rec: AnalysisRecord) -> Optional[str]:
    """Public author label: explicit author_name, else the e-mail prefix."""
    name = (rec.author_name or "").strip()
    if name:
        return name
    email = (rec.user_email or "").strip()
    if email and "@" in email:
        return email.split("@", 1)[0]
    return None


def serialize_analysis(rec: AnalysisRecord, liked_ids: Optional[set] = None) -> Dict[str, Any]:
    """AnalysisRecord → the JSON shape the React `Analysis` type expects."""
    expired = bool(getattr(rec, "artifacts_expired", False))
    urls = {} if expired else build_file_urls(rec.session_id, rec.files or {})
    return {
        "id": rec.session_id,
        "session_id": rec.session_id,
        "analysis_session_id": rec.session_id,
        "conversation_id": rec.conversation_id,
        "user_id": rec.user_id,
        "user_email": rec.user_email,
        "user_name": display_name_for(rec),
        "author_name": rec.author_name,
        "artifacts_expired": expired,
        "query": rec.query,
        "analysis_type": rec.analysis_type,
        "location": rec.location_name,
        "location_name": rec.location_name,
        "location_lat": rec.location_lat,
        "location_lng": rec.location_lng,
        "coordinates": rec.coordinates,
        "start_year": rec.start_year,
        "end_year": rec.end_year,
        "parameters": rec.parameters or {},
        "status": rec.status,
        "created_at": _iso(rec.created_at),
        "completed_at": _iso(rec.created_at),
        "map_url": _abs(urls.get("map_url")),
        "chart_url": _abs(urls.get("chart_url")),
        "charts_url": _abs(urls.get("chart_url")),
        "animation_url": _abs(urls.get("animation_url")),
        "preview_url": _abs(urls.get("preview_url")),
        "thumbnail_url": _abs(urls.get("preview_url")),
        "metadata": rec.results_data or {},
        "processing_time_seconds": rec.processing_time_seconds,
        "conversation_history": rec.conversation_history or [],
        "is_shared": bool(rec.is_shared),
        "shared_at": _iso(rec.shared_at),
        "likes": int(rec.likes or 0),
        "liked": (rec.session_id in liked_ids) if liked_ids is not None else None,
    }


def _page_params(page: int, per_page: int):
    page = max(1, page)
    per_page = max(1, min(per_page, MAX_PER_PAGE))
    return page, per_page, (page - 1) * per_page


# ── My Analyses ──────────────────────────────────────────────────────────────

@router.get("/analyses")
def list_my_analyses(
    user_id: str = Query(...),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=MAX_PER_PAGE),
    db: Session = Depends(get_db),
):
    page, per_page, offset = _page_params(page, per_page)
    base = db.query(AnalysisRecord).filter(AnalysisRecord.user_id == user_id)
    total = base.with_entities(func.count(AnalysisRecord.session_id)).scalar() or 0
    rows = (
        base.order_by(AnalysisRecord.created_at.desc())
        .offset(offset)
        .limit(per_page)
        .all()
    )
    return {
        "items": [serialize_analysis(r) for r in rows],
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "has_more": offset + len(rows) < total,
    }


class ShareBody(BaseModel):
    user_id: str
    is_shared: bool


@router.patch("/analyses/{session_id}/share")
def share_analysis(session_id: str, body: ShareBody, db: Session = Depends(get_db)):
    rec = db.get(AnalysisRecord, session_id)
    if rec is None or rec.user_id != body.user_id:
        raise HTTPException(status_code=404, detail="Analysis not found or access denied")
    rec.is_shared = body.is_shared
    rec.shared_at = datetime.utcnow() if body.is_shared else None
    db.commit()
    db.refresh(rec)
    return serialize_analysis(rec)


@router.patch("/conversations/{conversation_id}/share")
def share_conversation(conversation_id: str, body: ShareBody, db: Session = Depends(get_db)):
    cid = normalise_conversation_id(conversation_id)
    rows = (
        db.query(AnalysisRecord)
        .filter(AnalysisRecord.conversation_id == cid, AnalysisRecord.user_id == body.user_id)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Conversation not found or access denied")
    now = datetime.utcnow()
    for rec in rows:
        rec.is_shared = body.is_shared
        rec.shared_at = now if body.is_shared else None
    db.commit()
    return {"conversation_id": cid, "is_shared": body.is_shared, "updated": len(rows)}


# ── Community ────────────────────────────────────────────────────────────────

@router.get("/community")
def list_community(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=MAX_PER_PAGE),
    user_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    page, per_page, offset = _page_params(page, per_page)
    base = db.query(AnalysisRecord).filter(
        AnalysisRecord.is_shared.is_(True),
        AnalysisRecord.artifacts_expired.is_(False),
    )
    total = base.with_entities(func.count(AnalysisRecord.session_id)).scalar() or 0
    rows = (
        base.order_by(AnalysisRecord.shared_at.desc().nullslast(), AnalysisRecord.created_at.desc())
        .offset(offset)
        .limit(per_page)
        .all()
    )
    liked: set = set()
    if user_id and rows:
        ids = [r.session_id for r in rows]
        liked = {
            sid for (sid,) in db.query(AnalysisLike.session_id)
            .filter(AnalysisLike.user_id == user_id, AnalysisLike.session_id.in_(ids))
            .all()
        }
    return {
        "items": [serialize_analysis(r, liked) for r in rows],
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "has_more": offset + len(rows) < total,
        "liked": sorted(liked),
    }


class LikeBody(BaseModel):
    user_id: str


@router.post("/analyses/{session_id}/like")
def toggle_like(session_id: str, body: LikeBody, db: Session = Depends(get_db)):
    rec = db.get(AnalysisRecord, session_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    existing = (
        db.query(AnalysisLike)
        .filter(AnalysisLike.session_id == session_id, AnalysisLike.user_id == body.user_id)
        .first()
    )
    if existing:
        db.delete(existing)
        liked = False
    else:
        db.add(AnalysisLike(session_id=session_id, user_id=body.user_id))
        liked = True
    db.flush()
    rec.likes = (
        db.query(func.count(AnalysisLike.id))
        .filter(AnalysisLike.session_id == session_id)
        .scalar() or 0
    )
    db.commit()
    return {"session_id": session_id, "liked": liked, "likes": int(rec.likes)}


# ── Conversation messages ────────────────────────────────────────────────────

@router.get("/conversations/{conversation_id}/messages")
async def conversation_messages(conversation_id: str):
    msgs = await run_in_threadpool(list_messages, conversation_id)
    return {"conversation_id": normalise_conversation_id(conversation_id), "messages": msgs}


# ── Credits / billing ────────────────────────────────────────────────────────

@router.get("/credits/{user_id}")
async def get_credits(user_id: str):
    return await run_in_threadpool(credits_service.ensure_user_credits, user_id)


class DeductBody(BaseModel):
    user_id: str
    amount: int = Field(1, ge=1, le=100)
    analysis_id: Optional[str] = None
    description: str = "Analysis usage"


@router.post("/credits/deduct")
async def deduct_credits(body: DeductBody):
    res = await run_in_threadpool(
        credits_service.deduct_credits,
        body.user_id, body.amount, body.analysis_id, body.description,
    )
    if not res.get("success"):
        raise HTTPException(status_code=402, detail=res.get("error") or "Insufficient credits")
    return res


@router.get("/billing/summary/{user_id}")
async def billing_summary(user_id: str):
    return await run_in_threadpool(credits_service.billing_summary, user_id)
