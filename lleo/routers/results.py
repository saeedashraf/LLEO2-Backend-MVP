"""
LLEO v2 — Analysis + results endpoints.

POST /analyze                          — legacy direct analysis
GET  /status/{session_id}              — poll progress
GET  /gallery/{user_id}               — list past analyses
GET  /results/{session_id}/preview     — thumbnail image
GET  /results/{session_id}/map         — interactive map HTML
GET  /results/{session_id}/chart       — chart image
GET  /results/{session_id}/metadata    — JSON metadata
GET  /results/{session_id}/download    — ZIP archive
DELETE /results/{session_id}           — delete an analysis
GET  /files/{session_id}/{filename}    — serve GCS artefact
"""

from __future__ import annotations

import io
import os
import secrets as _secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import services
from ..database.models import AnalysisRecord, get_db
from ..models.routing import RoutingDecision
from ..services.router_service import route_query
from ..services.geocoding_service import build_location_from_decision, flatten_location
from ..services.analysis_service import (
    build_analysis_request,
    create_processor,
    process_analysis_background,
)

router = APIRouter(tags=["results"])


# ── Response models ──────────────────────────────────────────────────────────

class AnalysisResponse(BaseModel):
    session_id: str
    user_id: str
    analysis_type: str
    analysis: Dict[str, Any]
    files: Dict[str, str]
    created_at: datetime
    status: str


class GalleryItem(BaseModel):
    session_id: str
    analysis_type: str
    location_name: str
    query: str
    start_year: Optional[int] = None
    end_year: Optional[int] = None
    created_at: datetime
    thumbnail_url: str
    map_url: str
    chart_url: str
    download_url: str


# ── POST /analyze  (legacy one-shot) ─────────────────────────────────────────

@router.post("/analyze", response_model=AnalysisResponse)
async def analyze_query(
    background_tasks: BackgroundTasks,
    query: str = Form(...),
    user_id: str = Form(...),
    credentials_file: Optional[UploadFile] = File(None),
    download_data: bool = Form(True),
    db: Session = Depends(get_db),
):
    """Legacy direct analysis — routes, geocodes, launches background task."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session_id = f"{timestamp}_{_secrets.token_urlsafe(8)}"

    try:
        # Single-call routing
        decision: RoutingDecision = await route_query(query)

        if decision.needs_clarification:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{decision.clarification_message} "
                    "Please use /chat/start for interactive clarification."
                ),
            )

        nested_loc = build_location_from_decision(decision)
        if nested_loc:
            loc = flatten_location(nested_loc, fallback_name=decision.location_name or "Unknown")
        else:
            loc = {
                "name": decision.location_name or "Unknown",
                "latitude": decision.latitude or 0,
                "longitude": decision.longitude or 0,
            }

        time_period: dict = {}
        if decision.start_year:
            time_period["start_year"] = decision.start_year
        if decision.end_year:
            time_period["end_year"] = decision.end_year
        if decision.start_month:
            time_period["start_month"] = decision.start_month
        if decision.end_month:
            time_period["end_month"] = decision.end_month

        # ── Satellite data-availability guard ────────────────────────
        _EARLIEST = {
            "vegetation_index": 2015,
            "water_quality":    2015,
            "video_animation":  2015,
            "air_quality":      2018,
        }
        intent_val = decision.intent.value
        earliest = _EARLIEST.get(intent_val)
        if earliest and time_period.get("start_year") and time_period["start_year"] < earliest:
            time_period["start_year"] = earliest
        cur_yr = datetime.now().year
        if time_period.get("end_year") and time_period["end_year"] > cur_yr:
            time_period["end_year"] = cur_yr

        # ── Time-span guard: cap excessively long ranges ─────────────
        _MAX_SPAN = 5
        if (
            earliest
            and time_period.get("start_year")
            and time_period.get("end_year")
        ):
            span = time_period["end_year"] - time_period["start_year"]
            if span > _MAX_SPAN:
                time_period["start_year"] = time_period["end_year"] - _MAX_SPAN
        # ─────────────────────────────────────────────────────────────

        params: dict = {}
        if decision.pollutants:
            params["pollutants"] = decision.pollutants
        if decision.water_indicators:
            params["water_indicators"] = decision.water_indicators
        if decision.vegetation_index_type:
            params["vegetation_index_type"] = decision.vegetation_index_type
        if decision.business_type:
            params["business_type"] = decision.business_type
        if decision.keyword:
            params["keyword"] = decision.keyword
        if decision.radius:
            params["radius"] = decision.radius

        req = build_analysis_request(
            session_id=session_id,
            user_id=user_id,
            query=query,
            analysis_type=decision.intent.value,
            location=loc,
            time_period=time_period,
            parameters=params,
        )

        # Postgres processing record
        db_record = AnalysisRecord(
            session_id=session_id,
            user_id=user_id,
            query=query,
            analysis_type=decision.intent.value,
            location_name=loc.get("name", "Unknown"),
            coordinates=loc,
            status="processing",
            results_data={"progress": "Initializing analysis..."},
        )
        db.add(db_record)
        db.commit()

        # In-memory tracker
        services.analysis_sessions[session_id] = {
            "status": "processing",
            "progress": 0,
            "message": "Queued…",
            "user_id": user_id,
            "analysis_type": decision.intent.value,
            "location": loc.get("name"),
            "created_at": datetime.now().isoformat(),
        }

        processor = create_processor(decision.intent.value, services)

        background_tasks.add_task(
            process_analysis_background,
            processor,
            req,
            services.analysis_sessions,
            services.file_manager,
        )

        print(f"✅ Analysis queued: {session_id}")

        return AnalysisResponse(
            session_id=session_id,
            user_id=user_id,
            analysis_type=decision.intent.value,
            analysis=loc,
            files=_file_urls(session_id),
            created_at=datetime.now(),
            status="processing",
        )

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}")


# ── GET /status/{session_id} ─────────────────────────────────────────────────

@router.get("/status/{session_id}")
async def get_analysis_status(session_id: str, db: Session = Depends(get_db)):
    """Poll analysis progress — checks in-memory first, then DB."""
    # 1. In-memory session
    mem = services.analysis_sessions.get(session_id)
    if mem:
        resp = {
            "session_id": session_id,
            "status": mem.get("status", "processing"),
            "progress": mem.get("progress", 0),
            "message": mem.get("message", ""),
            "analysis_type": mem.get("analysis_type"),
            "location": mem.get("location"),
        }
        if mem.get("status") == "completed":
            resp["files"] = _file_urls(session_id)
            resp["result"] = mem.get("result")
        if mem.get("status") == "failed":
            resp["error"] = mem.get("error")
        return resp

    # 2. Postgres record
    analysis = (
        db.query(AnalysisRecord)
        .filter(AnalysisRecord.session_id == session_id)
        .first()
    )
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found")

    resp = {
        "session_id": session_id,
        "status": analysis.status,
        "analysis_type": analysis.analysis_type,
        "location": analysis.location_name,
        "created_at": analysis.created_at.isoformat() if analysis.created_at else None,
    }
    if analysis.status == "processing" and analysis.results_data:
        resp["progress"] = analysis.results_data.get("progress", "Processing...")
    if analysis.status == "completed":
        resp["files"] = _file_urls(session_id)
    if analysis.status == "failed" and analysis.results_data:
        resp["error"] = analysis.results_data.get("error", "Unknown error")
    return resp


# ── GET /gallery/{user_id} ───────────────────────────────────────────────────

@router.get("/gallery/{user_id}", response_model=List[GalleryItem])
async def get_user_gallery(
    user_id: str,
    limit: int = Query(50, le=500),
    offset: int = Query(0),
    db: Session = Depends(get_db),
):
    analyses = (
        db.query(AnalysisRecord)
        .filter(AnalysisRecord.user_id == user_id)
        .order_by(AnalysisRecord.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        GalleryItem(
            session_id=a.session_id,
            analysis_type=a.analysis_type or "ndvi",
            location_name=a.location_name or "Unknown Location",
            query=a.query or "",
            start_year=a.start_year,
            end_year=a.end_year,
            created_at=a.created_at,
            thumbnail_url=f"/results/{a.session_id}/preview",
            map_url=f"/results/{a.session_id}/map",
            chart_url=f"/results/{a.session_id}/chart",
            download_url=f"/results/{a.session_id}/download",
        )
        for a in analyses
    ]


# ── GET /results/{session_id}/preview ────────────────────────────────────────

@router.get("/results/{session_id}/preview")
async def get_preview(session_id: str, db: Session = Depends(get_db)):
    analysis = _get_record(session_id, db)
    if not analysis.thumbnail_path:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return _serve_cloud_or_local(analysis.thumbnail_path, "image/png")


# ── GET /results/{session_id}/map ────────────────────────────────────────────

@router.get("/results/{session_id}/map")
async def get_map(session_id: str, db: Session = Depends(get_db)):
    analysis = _get_record(session_id, db)
    path = (analysis.files or {}).get("map")
    if not path:
        raise HTTPException(status_code=404, detail="Map not found")
    return _serve_cloud_or_local(path, "text/html")


# ── GET /results/{session_id}/chart ──────────────────────────────────────────

@router.get("/results/{session_id}/chart")
async def get_chart(session_id: str, db: Session = Depends(get_db)):
    analysis = _get_record(session_id, db)
    path = (analysis.files or {}).get("chart") or (analysis.files or {}).get("charts")
    if not path:
        raise HTTPException(status_code=404, detail="Chart not found")
    return _serve_cloud_or_local(path, "image/png")


# ── GET /results/{session_id}/animation ──────────────────────────────────────

@router.get("/results/{session_id}/animation")
async def get_animation(session_id: str, db: Session = Depends(get_db)):
    analysis = _get_record(session_id, db)
    path = (analysis.files or {}).get("animation")
    if not path:
        raise HTTPException(status_code=404, detail="Animation not found")
    return _serve_cloud_or_local(path, "image/gif")


# ── GET /results/{session_id}/metadata ───────────────────────────────────────

@router.get("/results/{session_id}/metadata")
async def get_metadata(session_id: str, db: Session = Depends(get_db)):
    analysis = _get_record(session_id, db)
    return {
        "session_id": analysis.session_id,
        "user_id": analysis.user_id,
        "query": analysis.query,
        "analysis_type": analysis.analysis_type,
        "location_name": analysis.location_name,
        "coordinates": analysis.coordinates,
        "start_year": analysis.start_year,
        "end_year": analysis.end_year,
        "created_at": analysis.created_at,
        "status": analysis.status,
        "results_data": analysis.results_data,
    }


# ── GET /results/{session_id}/download ───────────────────────────────────────

@router.get("/results/{session_id}/download")
async def download_zip(session_id: str, db: Session = Depends(get_db)):
    """Download all analysis artefacts as a ZIP.

    Works with GCS-backed files (Cloud Run) and local files alike.
    Iterates every path stored in analysis.files, downloads the bytes,
    and streams them back inside a single ZIP archive.
    """
    import zipfile

    analysis = _get_record(session_id, db)
    files_dict: dict = analysis.files or {}

    if not files_dict:
        raise HTTPException(status_code=404, detail="No files found for this analysis")

    fm = services.file_manager
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, path in files_dict.items():
            if not path or not isinstance(path, str):
                continue
            # Skip metadata keys that are not file paths
            if key in ("output_folder",):
                continue

            # Derive a human-friendly filename
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else "bin"
            arc_name = f"{key}.{ext}"

            # --- GCS path ---
            if fm and fm.use_cloud and path.startswith("gs://"):
                try:
                    data = fm.get_file_bytes_from_cloud(path)
                    if data:
                        zf.writestr(arc_name, data)
                except Exception as exc:
                    print(f"⚠️ download_zip: could not fetch {key} from GCS: {exc}")
            # --- local path fallback ---
            elif os.path.exists(path):
                zf.write(path, arcname=arc_name)
            else:
                print(f"⚠️ download_zip: file not found for key '{key}': {path}")

    if buf.tell() == 0:
        raise HTTPException(status_code=404, detail="Could not retrieve any analysis files")

    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="lleo_analysis_{session_id}.zip"',
        },
    )


# ── DELETE /results/{session_id} ─────────────────────────────────────────────

@router.delete("/results/{session_id}")
async def delete_analysis(
    session_id: str,
    user_id: str = Query(...),
    db: Session = Depends(get_db),
):
    analysis = (
        db.query(AnalysisRecord)
        .filter(
            AnalysisRecord.session_id == session_id,
            AnalysisRecord.user_id == user_id,
        )
        .first()
    )
    if not analysis:
        raise HTTPException(status_code=404, detail="Analysis not found or access denied")

    fm = services.file_manager
    if analysis.files:
        for fp in analysis.files.values():
            if fp:
                try:
                    fm.delete_file(fp) if hasattr(fm, "delete_file") else None
                except Exception:
                    pass
    if analysis.thumbnail_path:
        try:
            fm.delete_file(analysis.thumbnail_path) if hasattr(fm, "delete_file") else None
        except Exception:
            pass

    db.delete(analysis)
    db.commit()
    return {"message": "Analysis deleted successfully"}


# ── GET /files/{session_id}/{filename} ───────────────────────────────────────

@router.get("/files/{session_id}/{filename}")
async def serve_file(session_id: str, filename: str):
    """Serve any artefact from GCS or local disk."""
    fm = services.file_manager
    cloud_path = f"analysis_results/{session_id}/{filename}"

    if fm and fm.use_cloud:
        try:
            data = fm.get_file_bytes_from_cloud(f"gs://{fm.bucket_name}/{cloud_path}")
            mime = _guess_mime(filename)
            return StreamingResponse(
                io.BytesIO(data),
                media_type=mime,
                headers={"Cache-Control": "public, max-age=3600"},
            )
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"File not found: {exc}")

    local = os.path.join("analysis_results", session_id, filename)
    if os.path.exists(local):
        return FileResponse(local, media_type=_guess_mime(filename))
    raise HTTPException(status_code=404, detail="File not found")


# ── Private helpers ──────────────────────────────────────────────────────────

def _file_urls(sid: str) -> dict:
    return {
        "preview_url": f"/results/{sid}/preview",
        "map_url": f"/results/{sid}/map",
        "chart_url": f"/results/{sid}/chart",
        "download_url": f"/results/{sid}/download",
        "metadata_url": f"/results/{sid}/metadata",
    }


def _get_record(session_id: str, db: Session) -> AnalysisRecord:
    rec = (
        db.query(AnalysisRecord)
        .filter(AnalysisRecord.session_id == session_id)
        .first()
    )
    if not rec:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return rec


def _serve_cloud_or_local(path: str, media_type: str):
    """Return a streaming response from GCS or fall back to local file."""
    fm = services.file_manager
    if fm and fm.use_cloud and path.startswith("gs://"):
        try:
            data = fm.get_file_bytes_from_cloud(path)
            if media_type == "text/html":
                return StreamingResponse(
                    io.StringIO(data.decode("utf-8")),
                    media_type=media_type,
                    headers={"Cache-Control": "public, max-age=3600"},
                )
            return StreamingResponse(
                io.BytesIO(data),
                media_type=media_type,
                headers={"Cache-Control": "public, max-age=3600"},
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Error serving file: {exc}")

    if os.path.exists(path):
        return FileResponse(path, media_type=media_type, headers={"Cache-Control": "public, max-age=3600"})
    raise HTTPException(status_code=404, detail="File not found")


def _guess_mime(filename: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "html": "text/html",
        "json": "application/json",
        "zip": "application/zip",
        "csv": "text/csv",
        "geojson": "application/geo+json",
    }.get(ext, "application/octet-stream")
