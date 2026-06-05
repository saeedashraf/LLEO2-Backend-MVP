"""
LLEO v2 — Analysis orchestration service.

Background processing, GCS saves, Supabase + Postgres persistence.
"""

from __future__ import annotations

import inspect
import json
import os
import uuid as _uuid
from datetime import datetime
from typing import Optional

from ..core.base_processor import AnalysisRequest, AnalysisResult
from ..processors import get_processor
from ..supabase_client import supabase_client
from ..database.models import AnalysisRecord, get_db
from ..utils.thumbnail import create_satellite_thumbnail


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
    """Run the processor in the background and persist results everywhere."""
    sid = request.session_id

    try:
        print(f"🔄 Background processing: {sid}")
        sessions[sid]["progress"] = 10
        sessions[sid]["message"] = "Processing data…"

        # Run (handles sync & async processors)
        if inspect.iscoroutinefunction(processor.process):
            result: AnalysisResult = await processor.process(request)
        else:
            result = processor.process(request)

        sessions[sid].update({
            "status": result.status,
            "progress": 100,
            "message": "Analysis completed",
            "result": {
                "outputs": result.outputs,
                "metadata": result.metadata,
                "processing_time": result.processing_time,
            },
            "completed_at": datetime.now().isoformat(),
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
        _save_to_supabase(file_manager, sid, request, result)
        _save_to_postgres(file_manager, sid, request, result, processor)

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


def _save_to_supabase(fm, sid, req, result) -> None:
    if not (result.outputs.get("map") or result.outputs.get("charts") or result.outputs.get("animation")):
        print(f"ℹ️  Skip Supabase — no map/charts/animation for {sid}")
        return

    try:
        bn = fm.bucket_name
        be = os.getenv(
            "BACKEND_URL",
            "https://ndvi6-analysis-api-258557095482.us-central1.run.app",
        )

        loc = req.location
        loc_name = loc.get("name", str(loc)) if isinstance(loc, dict) else str(loc)
        loc_lat = loc.get("latitude") if isinstance(loc, dict) else None
        loc_lng = loc.get("longitude") if isinstance(loc, dict) else None

        # ── Collect conversation history ──
        conv_history: list = []
        conv_sid = getattr(req, "conversation_session_id", None)
        if conv_sid:
            try:
                from ..routing.conversation_manager import conversation_manager
                cs = conversation_manager.get_conversation(conv_sid)
                if cs and cs.messages:
                    conv_history = cs.messages
            except Exception as exc:
                print(f"⚠️  Could not load conversation for Supabase: {exc}")

        # Convert conversation_session_id to UUID (must match conversation_messages table)
        db_conv_id = None
        if conv_sid:
            clean_id = conv_sid.replace("chat_", "") if conv_sid.startswith("chat_") else conv_sid
            try:
                _uuid.UUID(clean_id)
                db_conv_id = clean_id
            except ValueError:
                db_conv_id = str(_uuid.uuid5(_uuid.NAMESPACE_DNS, clean_id))

        row = {
            "session_id": sid,
            "analysis_session_id": sid,
            "conversation_id": db_conv_id,
            "user_id": req.user_id,
            "user_email": getattr(req, "user_email", "") or "",
            "query": req.query,
            "analysis_type": req.analysis_type,
            "location": loc_name,
            "location_lat": loc_lat,
            "location_lng": loc_lng,
            "start_year": (req.time_period or {}).get("start_year"),
            "end_year": (req.time_period or {}).get("end_year"),
            "parameters": req.parameters or {},
            "storage_path": f"analysis_results/{sid}/",
            "bucket_name": bn,
            "map_url": _gs_to_backend_url(result.outputs.get("map", ""), bn),
            "chart_url": _gs_to_public_url(result.outputs.get("charts", ""), bn),
            "charts_url": _gs_to_public_url(result.outputs.get("charts", ""), bn),
            "preview_url": _gs_to_public_url(result.outputs.get("preview", ""), bn),
            "thumbnail_url": _gs_to_public_url(result.outputs.get("preview", ""), bn) if result.outputs.get("preview") else f"{be}/results/{sid}/preview",
            "animation_url": _gs_to_public_url(result.outputs.get("animation", ""), bn),
            "results_json_url": f"gs://{bn}/analysis_results/{sid}/results.json",
            "status": "completed",
            "is_shared": False,
            "created_at": datetime.now().isoformat(),
            "completed_at": datetime.now().isoformat(),
            "metadata": _clean_metadata(result.metadata),
            "processing_time_seconds": result.processing_time,
            "conversation_history": conv_history or [],
        }
        try:
            supabase_client.client.table("analyses").insert(row).execute()
        except Exception:
            # Retry without columns that may not exist yet
            row.pop("user_email", None)
            row.pop("animation_url", None)
            supabase_client.client.table("analyses").insert(row).execute()
        print(f"💾 Supabase: {sid}")
    except Exception as exc:
        import traceback
        print(f"⚠️  Supabase save: {exc}")
        traceback.print_exc()


def _save_to_postgres(fm, sid, req, result, processor) -> None:
    try:
        db = next(get_db())
        thumb = _generate_thumbnail(fm, sid, req, result, processor)

        loc_name = (
            req.location.get("name", str(req.location))
            if isinstance(req.location, dict)
            else str(req.location)
        )

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

        record = AnalysisRecord(
            session_id=sid,
            user_id=req.user_id,
            query=req.query,
            analysis_type=req.analysis_type,
            location_name=loc_name,
            coordinates=result.metadata.get("coordinates"),
            start_year=(req.time_period or {}).get("start_year"),
            end_year=(req.time_period or {}).get("end_year"),
            files=result.outputs,
            status="completed",
            thumbnail_path=thumb,
            thumbnail_url=thumb,
            map_url=result.outputs.get("map"),
            chart_url=result.outputs.get("charts"),
            results_data=_clean_metadata(result.metadata),
            conversation_history=conv_history,
        )
        db.add(record)
        db.commit()
        db.close()
        print(f"💾 Postgres: {sid}")
    except Exception as exc:
        import traceback
        print(f"⚠️  Postgres save: {exc}")
        traceback.print_exc()


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
