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


# ── Artefact housekeeping ────────────────────────────────────────────────────

def _artifact_paths(rec: AnalysisRecord) -> List[str]:
    """gs:// paths that should exist for this analysis (preview first)."""
    paths: List[str] = []
    if rec.thumbnail_path:
        paths.append(rec.thumbnail_path)
    for key in ("preview", "map", "charts", "chart", "animation"):
        p = (rec.files or {}).get(key)
        if p and p not in paths:
            paths.append(p)
    return paths


def _prune_expired_sync(dry_run: bool, only_shared: bool, limit: int) -> dict:
    """Mark rows whose GCS artefacts are gone as ``artifacts_expired``."""
    from ..config import services

    fm = services.file_manager
    if fm is None:
        raise HTTPException(status_code=503, detail="File manager not initialised")

    s = db_models.SessionLocal()
    try:
        q = s.query(AnalysisRecord).filter(AnalysisRecord.artifacts_expired.is_(False))
        if only_shared:
            q = q.filter(AnalysisRecord.is_shared.is_(True))
        rows = q.order_by(AnalysisRecord.created_at.desc()).limit(limit).all()

        expired: List[str] = []
        kept: List[str] = []
        for rec in rows:
            paths = _artifact_paths(rec)
            # A row with no artefact paths at all (legacy / failed) is expired too.
            alive = any(fm.file_exists(p) for p in paths) if paths else False
            if alive:
                kept.append(rec.session_id)
            else:
                expired.append(rec.session_id)
                if not dry_run:
                    rec.artifacts_expired = True
        if not dry_run:
            s.commit()
        return {
            "checked": len(rows),
            "expired": len(expired),
            "kept": len(kept),
            "dry_run": dry_run,
            "expired_ids": expired,
        }
    finally:
        s.close()


@router.post("/prune-expired-artifacts", dependencies=[Depends(require_admin)])
async def prune_expired_artifacts(
    dry_run: bool = Body(default=True, embed=True),
    only_shared: bool = Body(default=False, embed=True),
    limit: int = Body(default=1000, embed=True),
):
    """Scan analyses and flag those whose GCS files no longer exist.

    Flagged rows are hidden from Community and shown with a placeholder in
    My Analyses. Run with ``dry_run=false`` to persist.
    """
    if db_models.SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    return await run_in_threadpool(_prune_expired_sync, dry_run, only_shared, max(1, min(limit, 5000)))


def _location_for_thumbnail(rec: AnalysisRecord) -> Optional[dict]:
    """Rebuild a thumbnail-friendly location dict from what the row stores."""
    coords = rec.coordinates if isinstance(rec.coordinates, dict) else {}
    lat = rec.location_lat
    lng = rec.location_lng
    if lat is None or lng is None:
        lat = coords.get("latitude", coords.get("lat"))
        lng = coords.get("longitude", coords.get("lon", coords.get("lng")))
        if isinstance(lat, dict):
            lat = lat.get("center")
        if isinstance(lng, dict):
            lng = lng.get("center")
    if lat is None or lng is None:
        return None
    loc: dict = {"name": rec.location_name or "Unknown", "latitude": float(lat), "longitude": float(lng)}
    b = coords.get("bounds") if isinstance(coords.get("bounds"), dict) else None
    if b and all(k in b for k in ("lat_min", "lat_max", "lng_min", "lng_max")):
        loc["bounds"] = b
    elif isinstance(coords.get("latitude"), dict) and isinstance(coords.get("longitude"), dict):
        la, lo = coords["latitude"], coords["longitude"]
        if "min" in la and "min" in lo:
            loc["bounds"] = {"lat_min": la["min"], "lat_max": la["max"], "lng_min": lo["min"], "lng_max": lo["max"]}
    return loc


def _regen_thumbnails_sync(session_ids: Optional[List[str]], only_missing: bool, limit: int) -> dict:
    from ..config import services
    from ..utils.thumbnail import create_satellite_thumbnail

    fm = services.file_manager
    if fm is None:
        raise HTTPException(status_code=503, detail="File manager not initialised")

    s = db_models.SessionLocal()
    try:
        q = s.query(AnalysisRecord)
        if session_ids:
            q = q.filter(AnalysisRecord.session_id.in_(session_ids))
        rows = q.order_by(AnalysisRecord.created_at.desc()).limit(limit).all()

        done: List[str] = []
        skipped: List[str] = []
        failed: List[str] = []
        os.makedirs("/tmp/lleo", exist_ok=True)
        for rec in rows:
            if only_missing and rec.thumbnail_path and fm.file_exists(rec.thumbnail_path):
                skipped.append(rec.session_id)
                continue
            loc = _location_for_thumbnail(rec)
            if not loc:
                failed.append(rec.session_id)
                continue
            local = f"/tmp/lleo/{rec.session_id}_preview.png"
            ok = create_satellite_thumbnail(
                location=loc,
                analysis_type=rec.analysis_type or "analysis",
                thumbnail_path=local,
                start_year=rec.start_year,
                end_year=rec.end_year,
            )
            if not ok:
                failed.append(rec.session_id)
                continue
            try:
                cloud_path = fm.save_file_to_cloud(local, f"analysis_results/{rec.session_id}/preview.png")
                rec.thumbnail_path = cloud_path
                files = dict(rec.files or {})
                files["preview"] = cloud_path
                rec.files = files
                done.append(rec.session_id)
            except Exception as exc:
                print(f"⚠️  thumbnail upload failed for {rec.session_id}: {exc}")
                failed.append(rec.session_id)
            finally:
                try:
                    os.remove(local)
                except OSError:
                    pass
        s.commit()
        return {"checked": len(rows), "regenerated": len(done), "skipped": len(skipped),
                "failed": failed, "regenerated_ids": done}
    finally:
        s.close()


@router.post("/regenerate-thumbnails", dependencies=[Depends(require_admin)])
async def regenerate_thumbnails(
    session_ids: Optional[List[str]] = Body(default=None, embed=True),
    only_missing: bool = Body(default=True, embed=True),
    limit: int = Body(default=200, embed=True),
):
    """Rebuild ``preview.png`` (satellite tiles fitted to the region bbox) from
    the location stored in Cloud SQL — for rows whose GCS preview was deleted.
    """
    if db_models.SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    return await run_in_threadpool(_regen_thumbnails_sync, session_ids, only_missing, max(1, min(limit, 2000)))


# ── Author / publishing helpers ──────────────────────────────────────────────

@router.post("/backfill-user-emails", dependencies=[Depends(require_admin)])
def backfill_user_emails():
    """Fill ``analyses.user_email`` from other rows of the same user_id.

    Older analyses were written without an e-mail (the value lived in a
    per-instance cache). If the same user has *any* row with an e-mail, copy it.
    """
    if db_models.SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    s = db_models.SessionLocal()
    try:
        known = {
            uid: email
            for uid, email in s.query(AnalysisRecord.user_id, AnalysisRecord.user_email)
            .filter(AnalysisRecord.user_email.isnot(None), AnalysisRecord.user_email != "")
            .distinct()
            .all()
        }
        # Also consult subscriptions (they always carry the e-mail)
        for uid, email in s.query(Subscription.user_id, Subscription.user_email).all():
            if email and uid not in known:
                known[uid] = email

        rows = (
            s.query(AnalysisRecord)
            .filter((AnalysisRecord.user_email.is_(None)) | (AnalysisRecord.user_email == ""))
            .all()
        )
        updated = 0
        for rec in rows:
            email = known.get(rec.user_id)
            if email:
                rec.user_email = email
                updated += 1
        s.commit()
        return {"candidates": len(rows), "updated": updated, "still_missing": len(rows) - updated}
    finally:
        s.close()


@router.post("/publish", dependencies=[Depends(require_admin)])
def publish_analyses(
    session_ids: List[str] = Body(..., embed=True),
    author_name: Optional[str] = Body(default=None, embed=True),
    is_shared: bool = Body(default=True, embed=True),
):
    """Share (or un-share) analyses and optionally stamp a public author name."""
    if db_models.SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    from datetime import datetime

    s = db_models.SessionLocal()
    try:
        rows = s.query(AnalysisRecord).filter(AnalysisRecord.session_id.in_(session_ids)).all()
        found = {r.session_id for r in rows}
        now = datetime.utcnow()
        for rec in rows:
            rec.is_shared = is_shared
            rec.shared_at = now if is_shared else None
            if author_name is not None:
                rec.author_name = author_name.strip() or None
        s.commit()
        return {
            "updated": len(rows),
            "missing": [sid for sid in session_ids if sid not in found],
            "is_shared": is_shared,
            "author_name": author_name,
        }
    finally:
        s.close()
