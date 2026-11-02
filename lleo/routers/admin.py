"""
Admin endpoints (guarded by the ``X-Admin-Key`` header == ``ADMIN_KEY`` env).

    POST /api/admin/migrate-supabase           one-shot Supabase → Cloud SQL data copy
    GET  /api/admin/stats                      row counts per table (sanity check)
    POST /api/admin/prune-expired-artifacts    flag rows whose GCS files are gone
    POST /api/admin/restore-expired-artifacts  un-flag / rebuild previews
    POST /api/admin/regenerate-thumbnails      rebuild preview.png from stored location
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta
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
from ..services import preview_service, supabase_migration

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
            "expired_analyses": s.query(func.count()).select_from(AnalysisRecord)
                .filter(AnalysisRecord.artifacts_expired.is_(True)).scalar() or 0,
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
#
# Durability contract: previews must never "expire".
#   * The bucket has NO lifecycle/delete rule (and must never get one).
#   * ``artifacts_expired`` is only ever set by the explicit prune endpoint
#     below, which now refuses to flag a row unless it could *positively*
#     verify (via a working GCS client) that every artefact is gone.
#   * Flagged rows are not final: ``/restore-expired-artifacts`` re-checks
#     them and, if the preview is really gone, rebuilds it from the stored
#     location (see ``services.preview_service``).  The ``/results/{id}/preview``
#     endpoint does the same lazily on demand.

_artifact_paths = preview_service.artifact_paths
_location_for_thumbnail = preview_service.location_for_thumbnail

# Rows younger than this are never flagged: the background job may still be
# uploading their artefacts.
_PRUNE_MIN_AGE = timedelta(hours=6)


def _prune_expired_sync(dry_run: bool, only_shared: bool, limit: int) -> dict:
    """Mark rows whose GCS artefacts are gone as ``artifacts_expired``.

    Conservative by design: a row is flagged only when *all* of its artefact
    paths were positively verified as missing. Rows with no artefact paths,
    rows younger than ``_PRUNE_MIN_AGE`` and rows whose check hit a GCS error
    are skipped (reported under ``skipped``), never flagged.
    """
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

        cutoff = datetime.utcnow() - _PRUNE_MIN_AGE
        expired: List[str] = []
        kept: List[str] = []
        skipped: List[str] = []
        errors: List[str] = []
        for rec in rows:
            if rec.created_at and rec.created_at > cutoff:
                skipped.append(rec.session_id)
                continue
            try:
                alive = preview_service.any_artifact_alive(fm, rec)
            except Exception as exc:
                print(f"⚠️  prune: could not verify {rec.session_id}: {exc}")
                errors.append(rec.session_id)
                continue
            if alive is None:          # no artefact paths → nothing to verify
                skipped.append(rec.session_id)
            elif alive:
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
            "skipped": len(skipped),
            "errors": len(errors),
            "dry_run": dry_run,
            "expired_ids": expired,
            "error_ids": errors,
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
    My Analyses. Run with ``dry_run=false`` to persist.  Use
    ``/restore-expired-artifacts`` to undo / rebuild.
    """
    if db_models.SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    return await run_in_threadpool(_prune_expired_sync, dry_run, only_shared, max(1, min(limit, 5000)))


def _restore_expired_sync(dry_run: bool, regenerate: bool, session_ids: Optional[List[str]], limit: int) -> dict:
    """Re-check rows flagged ``artifacts_expired`` and bring them back.

    * artefacts exist again (false positive) → flag cleared;
    * preview missing and ``regenerate`` → ``preview.png`` rebuilt from the
      stored location, uploaded, flag cleared;
    * otherwise left flagged (reported under ``still_missing``).
    """
    from ..config import services

    fm = services.file_manager
    if fm is None:
        raise HTTPException(status_code=503, detail="File manager not initialised")

    s = db_models.SessionLocal()
    try:
        q = s.query(AnalysisRecord).filter(AnalysisRecord.artifacts_expired.is_(True))
        if session_ids:
            q = q.filter(AnalysisRecord.session_id.in_(session_ids))
        rows = q.order_by(AnalysisRecord.created_at.desc()).limit(limit).all()

        unflagged: List[str] = []
        regenerated: List[str] = []
        still_missing: List[str] = []
        errors: List[str] = []
        for rec in rows:
            try:
                alive = preview_service.any_artifact_alive(fm, rec)
            except Exception as exc:
                print(f"⚠️  restore: could not verify {rec.session_id}: {exc}")
                errors.append(rec.session_id)
                continue
            preview_ok = False
            if rec.thumbnail_path:
                try:
                    preview_ok = preview_service.blob_exists(fm, rec.thumbnail_path)
                except Exception:
                    preview_ok = False
            if alive and preview_ok:
                unflagged.append(rec.session_id)
                if not dry_run:
                    rec.artifacts_expired = False
                continue
            if regenerate and not dry_run:
                try:
                    if preview_service.regenerate_preview(rec, fm):
                        regenerated.append(rec.session_id)
                        continue
                except Exception as exc:
                    print(f"⚠️  restore: regeneration failed for {rec.session_id}: {exc}")
            elif regenerate and dry_run and preview_service.location_for_thumbnail(rec):
                regenerated.append(rec.session_id)      # would be rebuilt
                continue
            if alive:
                # Map/charts survive but the preview is gone and could not be
                # rebuilt: still clear the flag so the row returns to Community
                # with the generic card placeholder instead of vanishing.
                unflagged.append(rec.session_id)
                if not dry_run:
                    rec.artifacts_expired = False
            else:
                still_missing.append(rec.session_id)
        if not dry_run:
            s.commit()
        return {
            "checked": len(rows),
            "unflagged": len(unflagged),
            "regenerated": len(regenerated),
            "still_missing": len(still_missing),
            "errors": len(errors),
            "dry_run": dry_run,
            "unflagged_ids": unflagged,
            "regenerated_ids": regenerated,
            "still_missing_ids": still_missing,
            "error_ids": errors,
        }
    finally:
        s.close()


@router.post("/restore-expired-artifacts", dependencies=[Depends(require_admin)])
async def restore_expired_artifacts(
    dry_run: bool = Body(default=True, embed=True),
    regenerate: bool = Body(default=True, embed=True),
    session_ids: Optional[List[str]] = Body(default=None, embed=True),
    limit: int = Body(default=1000, embed=True),
):
    """Undo ``artifacts_expired``: clear false positives and rebuild missing
    previews from the location stored in Cloud SQL."""
    if db_models.SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database not initialised")
    return await run_in_threadpool(
        _restore_expired_sync, dry_run, regenerate, session_ids, max(1, min(limit, 5000))
    )


def _regen_thumbnails_sync(session_ids: Optional[List[str]], only_missing: bool, limit: int) -> dict:
    from ..config import services

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
        for rec in rows:
            if only_missing and rec.thumbnail_path and fm.file_exists(rec.thumbnail_path):
                skipped.append(rec.session_id)
                continue
            try:
                if preview_service.regenerate_preview(rec, fm):
                    done.append(rec.session_id)
                else:
                    failed.append(rec.session_id)
            except Exception as exc:
                print(f"⚠️  thumbnail regeneration failed for {rec.session_id}: {exc}")
                failed.append(rec.session_id)
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
