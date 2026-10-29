"""
LLEO v2 — Analysis orchestration service.

Background processing, GCS saves, Cloud SQL persistence, credit deduction.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

from ..core.base_processor import AnalysisRequest, AnalysisResult
from ..processors import get_processor
from ..database.models import AnalysisRecord, get_db
from ..utils.thumbnail import create_satellite_thumbnail
from . import credits_service

ANALYSIS_CREDIT_COST = int(os.getenv("LLEO_ANALYSIS_CREDIT_COST", "1"))


# ── Worker pool for analyses ─────────────────────────────────────────────────
#
# Every processor does blocking I/O and CPU work (Earth Engine getInfo(),
# rasterio, Gemini generate_content, GCS uploads, SQLAlchemy).  If that runs
# on the event loop — directly or inside an `async def` that never truly
# yields — the whole API freezes for the duration of the job (measured:
# /health took 82 s while a water-quality analysis ran).
#
# Analyses are therefore executed on a dedicated thread pool.  A dedicated
# pool (rather than the loop's default executor) keeps heavy jobs from
# starving the threads FastAPI uses for sync dependencies such as get_db().

_ANALYSIS_WORKERS = int(os.getenv("LLEO_ANALYSIS_WORKERS", "4"))
_analysis_executor = ThreadPoolExecutor(
    max_workers=_ANALYSIS_WORKERS, thread_name_prefix="lleo-analysis"
)


def _run_processor_sync(processor, request: AnalysisRequest) -> AnalysisResult:
    """Run a processor to completion on the *current* (worker) thread.

    Sync processors are called directly.  Async processors get their own
    short-lived event loop in this thread; their internal awaits are all
    on blocking code anyway, so nothing is lost and the main loop is spared.
    """
    if inspect.iscoroutinefunction(processor.process):
        return asyncio.run(processor.process(request))
    return processor.process(request)


# ── File URL helper (shared by routers) ──────────────────────────────────────

def build_file_urls(session_id: str, outputs: Optional[dict] = None) -> dict:
    """Return the `/results/...` URLs that actually exist for an analysis.

    `outputs` is the processor's result.outputs dict ({map, charts, preview,
    animation, ...}).  Only artefacts present in it are advertised, so a
    video_animation job no longer claims a `map_url` that 404s.

    When `outputs` is None (job still running) only the endpoints that are
    valid for every analysis type are returned.
    """
    urls = {
        "preview_url": f"/results/{session_id}/preview",
        "download_url": f"/results/{session_id}/download",
        "metadata_url": f"/results/{session_id}/metadata",
    }
    if not outputs:
        return urls
    if outputs.get("map"):
        urls["map_url"] = f"/results/{session_id}/map"
    if outputs.get("charts") or outputs.get("chart"):
        urls["chart_url"] = f"/results/{session_id}/chart"
    if outputs.get("animation"):
        urls["animation_url"] = f"/results/{session_id}/animation"
    return urls


# ── Public helpers ───────────────────────────────────────────────────────────

def build_analysis_request(
    session_id: str,
    user_id: str,
    query: str,
    analysis_type: str,
    location: dict,
    time_period: dict,
    parameters: dict,
    conversation_session_id: Optional[str] = None,
) -> AnalysisRequest:
    """Create an AnalysisRequest and optionally link a conversation."""
    req = AnalysisRequest(
        session_id=session_id,
        user_id=user_id,
        query=query,
        analysis_type=analysis_type,
        location=location,
        time_period=time_period,
        parameters=parameters,
    )
    if conversation_session_id:
        req.conversation_session_id = conversation_session_id  # type: ignore[attr-defined]
    return req


def create_processor(analysis_type: str, svc):
    """Instantiate the right processor and wire shared services."""
    ProcessorClass = get_processor(analysis_type)
    proc = ProcessorClass(svc.config)
    proc.file_manager = svc.file_manager
    proc.model = svc.gemini_model
    proc.get_secret = svc.get_secret
    return proc


# ── Background task ──────────────────────────────────────────────────────────

async def process_analysis_background(
    processor,
    request: AnalysisRequest,
    sessions: dict,
    file_manager,
) -> None:
    """Background entry point — off-loads the whole job to the worker pool.

    Keeps the event loop free so /health, /status, /chat etc. stay
    responsive while analyses run.
    """
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        _analysis_executor,
        _process_analysis_sync,
        processor,
        request,
        sessions,
        file_manager,
    )


def _process_analysis_sync(
    processor,
    request: AnalysisRequest,
    sessions: dict,
    file_manager,
) -> None:
    """Run the processor and persist results everywhere (worker thread)."""
    sid = request.session_id

    try:
        print(f"🔄 Background processing: {sid}")
        sessions[sid]["progress"] = 10
        sessions[sid]["message"] = "Processing data…"

        result: AnalysisResult = _run_processor_sync(processor, request)

        # The processor is finished, but the artefacts are not yet servable:
        # /results/{sid}/* reads from the Postgres record, which is written
        # below. Keep the session in "processing" until persistence is done
        # so clients never see "completed" before the files are retrievable.
        sessions[sid].update({
            "progress": 90,
            "message": "Saving results…",
            "result": {
                "outputs": result.outputs,
                "metadata": result.metadata,
                "processing_time": result.processing_time,
            },
        })

        # GCS artefacts
        _save_json_to_gcs(file_manager, sid, "results.json", {
            "session_id": sid,
            "query": request.query,
            "analysis_type": request.analysis_type,
            "location": request.location,
            "results": result.metadata,
            "timestamp": datetime.now().isoformat(),
        })
        _save_json_to_gcs(file_manager, sid, "metadata.json", {
            "session_id": sid,
            "user_id": request.user_id,
            "query": request.query,
            "analysis_type": request.analysis_type,
            "location_data": request.location,
            "results": result.metadata,
            "created_at": datetime.now().isoformat(),
        })

        _normalise_preview(file_manager, sid, result)
        _save_to_postgres(file_manager, sid, request, result, processor)

        # Only now are the artefact endpoints guaranteed to resolve.
        sessions[sid].update({
            "status": result.status,
            "progress": 100,
            "message": "Analysis completed",
            "completed_at": datetime.now().isoformat(),
        })

        # Charge the user (server-side, idempotent per session id)
        credits_left = _deduct_analysis_credit(request)
        if credits_left is not None:
            sessions[sid]["credits_remaining"] = credits_left

        # Save completion message to conversation timeline
        _save_completion_message(request, result)

        print(f"✅ Done: {sid}  ({result.processing_time:.1f}s)")

    except Exception as exc:
        import traceback
        traceback.print_exc()
        sessions[sid].update({
            "status": "failed",
            "progress": 0,
            "message": f"Error: {exc}",
            "error": str(exc),
            "failed_at": datetime.now().isoformat(),
        })


# ── Private helpers ──────────────────────────────────────────────────────────

def _save_completion_message(request: AnalysisRequest, result: AnalysisResult) -> None:
    """Save an 'analysis finished' message to the conversation timeline."""
    try:
        conv_sid = getattr(request, "conversation_session_id", None)
        if not conv_sid:
            return
        from .conversation_service import save_message
        user_id = request.user_id
        loc = request.location.get("name", "")
        atype = request.analysis_type.replace("_", " ")
        t = f"{result.processing_time:.0f}" if result.processing_time else "?"
        msg = (
            f"Your {atype} analysis{f' for {loc}' if loc else ''} "
            f"is complete ({t}s). Results are ready — check the Map and Charts tabs!"
        )
        save_message(conv_sid, user_id, "assistant", msg)
    except Exception as exc:
        print(f"⚠️  Could not save completion message: {exc}")


def _save_json_to_gcs(fm, sid: str, fname: str, data: dict) -> None:
    try:
        tmp = f"/tmp/lleo/{sid}_{fname}"
        os.makedirs("/tmp/lleo", exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
        fm.save_file_to_cloud(tmp, f"analysis_results/{sid}/{fname}")
        print(f"📄 {fname} → GCS")
    except Exception as exc:
        print(f"⚠️  GCS save ({fname}): {exc}")


def _normalise_preview(fm, sid: str, result: AnalysisResult) -> None:
    try:
        preview = result.outputs.get("preview", "")
        if "charts_preview.png" in preview:
            tmp = f"/tmp/lleo/{sid}_preview_temp.png"
            fm.get_file_from_cloud(preview, tmp)
            new_url = fm.save_file_to_cloud(
                tmp, f"analysis_results/{sid}/preview.png"
            )
            result.outputs["preview"] = new_url
    except Exception as exc:
        print(f"⚠️  Preview rename: {exc}")


def _gs_to_public_url(url: str, bucket: str) -> str:
    """Convert gs://bucket/path to https://storage.googleapis.com/bucket/path for direct CDN serving."""
    if url and url.startswith("gs://"):
        path = url.replace(f"gs://{bucket}/", "")
        return f"https://storage.googleapis.com/{bucket}/{path}"
    return url


def _gs_to_backend_url(url: str, bucket: str) -> str:
    if url and url.startswith("gs://"):
        parts = url.replace(
            f"gs://{bucket}/analysis_results/", ""
        ).split("/", 1)
        if len(parts) == 2:
            be = os.getenv(
                "BACKEND_URL",
                "https://ndvi6-analysis-api-258557095482.us-central1.run.app",
            )
            return f"{be}/files/{parts[0]}/{parts[1]}"
    return url


def _clean_metadata(meta) -> dict:
    """Recursively strip non-serialisable objects (ee.Image, etc.) from metadata."""
    if meta is None:
        return {}
    if isinstance(meta, dict):
        return {
            k: _clean_metadata(v)
            for k, v in meta.items()
            if not _is_unserializable(v)
        }
    if isinstance(meta, (list, tuple)):
        return [_clean_metadata(item) for item in meta if not _is_unserializable(item)]
    return meta


def _is_unserializable(obj) -> bool:
    """Return True for objects that cannot be JSON-serialised (ee.Image, etc.)."""
    if obj is None:
        return False
    cls_name = type(obj).__name__
    return cls_name in ("Image", "ComputedObject", "Element")


def _deduct_analysis_credit(req: AnalysisRequest) -> Optional[int]:
    """Charge ANALYSIS_CREDIT_COST credits for a finished analysis.

    Idempotent per session id (retried callbacks never double-charge).
    Returns the remaining balance, or None if the deduction could not run.
    """
    if ANALYSIS_CREDIT_COST <= 0:
        return None
    try:
        loc = req.location.get("name", "") if isinstance(req.location, dict) else ""
        res = credits_service.deduct_credits(
            req.user_id,
            ANALYSIS_CREDIT_COST,
            analysis_id=req.session_id,
            description=f"{req.analysis_type.replace('_', ' ')} analysis"
                        + (f" — {loc}" if loc else ""),
        )
        if not res.get("success"):
            print(f"⚠️  Credit deduction failed for {req.session_id}: {res.get('error')}")
        return res.get("credits")
    except Exception as exc:
        print(f"⚠️  Credit deduction error: {exc}")
        return None


def _save_to_postgres(fm, sid, req, result, processor) -> None:
    """Persist the analysis row in Cloud SQL (single source of truth)."""
    db = None
    try:
        db = next(get_db())
        thumb = _generate_thumbnail(fm, sid, req, result, processor)

        loc = req.location
        loc_name = loc.get("name", str(loc)) if isinstance(loc, dict) else str(loc)
        loc_lat = loc.get("latitude") if isinstance(loc, dict) else None
        loc_lng = loc.get("longitude") if isinstance(loc, dict) else None

        conv_history: list = []
        conv_sid = getattr(req, "conversation_session_id", None)
        if conv_sid:
            try:
                from ..routing.conversation_manager import conversation_manager
                cs = conversation_manager.get_conversation(conv_sid)
                if cs and cs.messages:
                    conv_history = cs.messages
            except Exception:
                pass

        conversation_id = None
        if conv_sid:
            conversation_id = conv_sid[5:] if conv_sid.startswith("chat_") else conv_sid

        bn = getattr(fm, "bucket_name", None) or ""
        preview = result.outputs.get("preview", "")
        thumbnail_url = thumb or (_gs_to_public_url(preview, bn) if preview else None)

        values = dict(
            user_id=req.user_id,
            user_email=getattr(req, "user_email", None) or None,
            query=req.query,
            analysis_type=req.analysis_type,
            location_name=loc_name,
            location_lat=loc_lat,
            location_lng=loc_lng,
            coordinates=result.metadata.get("coordinates") if isinstance(result.metadata, dict) else None,
            start_year=(req.time_period or {}).get("start_year"),
            end_year=(req.time_period or {}).get("end_year"),
            parameters=req.parameters or {},
            files=result.outputs,
            status="completed",
            thumbnail_path=thumb,
            thumbnail_url=thumbnail_url,
            map_url=result.outputs.get("map"),
            chart_url=result.outputs.get("charts"),
            results_data=_clean_metadata(result.metadata),
            conversation_history=conv_history,
            conversation_id=conversation_id,
            processing_time_seconds=result.processing_time,
        )

        record = db.get(AnalysisRecord, sid)
        if record is None:
            record = AnalysisRecord(session_id=sid, **values)
            db.add(record)
        else:
            for k, v in values.items():
                setattr(record, k, v)
        db.commit()
        print(f"💾 Cloud SQL: {sid}")
    except Exception as exc:
        import traceback
        print(f"⚠️  Cloud SQL save: {exc}")
        traceback.print_exc()
        if db is not None:
            try:
                db.rollback()
            except Exception:
                pass
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass


def _generate_thumbnail(fm, sid, req, result, processor) -> Optional[str]:
    try:
        os.makedirs("/tmp/lleo", exist_ok=True)
        thumb = f"/tmp/lleo/{sid}_preview.png"
        ok = create_satellite_thumbnail(
            location=req.location,
            analysis_type=req.analysis_type,
            thumbnail_path=thumb,
            start_year=(req.time_period or {}).get("start_year"),
            end_year=(req.time_period or {}).get("end_year"),
        )
        if ok and hasattr(processor, "file_manager") and processor.file_manager:
            return processor.file_manager.save_file_to_cloud(
                thumb, f"analysis_results/{sid}/preview.png"
            )
        return thumb if ok else (result.outputs.get("charts") or result.outputs.get("map"))
    except Exception:
        return result.outputs.get("charts") or result.outputs.get("map")
