"""
LLEO v2 — Health, info & debugging endpoints.

GET /             — service info
GET /health       — health check
GET /processors   — list available processors
GET /processors/{type} — details for one processor
GET /api/logs     — recent Cloud Run logs
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Query

from ..config import services
from ..processors import PROCESSORS

router = APIRouter(tags=["health"])


# ── GET / ─────────────────────────────────────────────────────────────────────

@router.get("/")
async def root():
    pid = services.config.gcp_project_id if services.config else "unknown"
    bkt = services.config.storage_bucket if services.config else "unknown"
    return {
        "service": "LLEO v2 — Large Language Models for Earth Observation",
        "version": "2.0.0",
        "project_id": pid,
        "storage_bucket": bkt,
        "region": "us-central1",
        "features": [
            "Vegetation Index — NDVI, deforestation, crop monitoring",
            "Air Quality — Sentinel-5P NO2/CO/SO2/O3 analysis",
            "Water Quality — Sentinel-2 turbidity/chlorophyll/NDWI",
            "Weather Forecast — OpenWeather multi-day forecast",
            "Business Location — Google Places POI discovery",
        ],
        "endpoints": {
            "chat_start": "POST /chat/start",
            "chat_continue": "POST /chat/continue",
            "chat_analyze": "POST /chat/analyze",
            "analyze": "POST /analyze",
            "gallery": "GET /gallery/{user_id}",
            "results": {
                "map": "GET /results/{session_id}/map",
                "chart": "GET /results/{session_id}/chart",
                "preview": "GET /results/{session_id}/preview",
                "download": "GET /results/{session_id}/download",
                "metadata": "GET /results/{session_id}/metadata",
            },
        },
    }


# ── GET /health ───────────────────────────────────────────────────────────────

@router.get("/health")
async def health_check():
    """Deep health check — database, storage, Vertex AI, Earth Engine."""
    db_status = "healthy"
    try:
        from ..database.models import engine
        from sqlalchemy import text

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        db_status = f"unhealthy: {exc}"

    storage_ok = services.file_manager is not None
    gemini_ok = services.gemini_model is not None

    ee_status = "healthy"
    try:
        import ee

        ee.Number(1).getInfo()
    except Exception as exc:
        ee_status = f"unhealthy: {exc}"

    overall = "healthy" if (db_status == "healthy" and ee_status == "healthy") else "degraded"

    return {
        "status": overall,
        "project_id": services.config.gcp_project_id if services.config else None,
        "analyzer_initialized": True,
        "gemini_model_initialized": gemini_ok,
        "components": {
            "database": db_status,
            "cloud_storage": "healthy" if storage_ok else "unavailable",
            "vertex_ai": gemini_ok,
            "earth_engine": ee_status,
        },
        "timestamp": datetime.now().isoformat(),
    }


# ── GET /processors ──────────────────────────────────────────────────────────

@router.get("/processors")
async def list_processors():
    return {
        "processors": list(PROCESSORS.keys()),
        "count": len(PROCESSORS),
    }


# ── GET /processors/{type} ───────────────────────────────────────────────────

@router.get("/processors/{proc_type}")
async def processor_detail(proc_type: str):
    cls = PROCESSORS.get(proc_type)
    if not cls:
        return {"error": f"Unknown processor: {proc_type}", "available": list(PROCESSORS.keys())}
    return {
        "type": proc_type,
        "class": cls.__name__,
        "module": cls.__module__,
    }


# ── GET /api/logs ─────────────────────────────────────────────────────────────

@router.get("/api/logs")
async def get_recent_logs(limit: int = Query(100, le=500)):
    """Fetch recent Cloud Run logs from Cloud Logging."""
    try:
        from google.cloud import logging as cloud_logging

        pid = services.config.gcp_project_id if services.config else os.getenv("GCP_PROJECT_ID")
        client = cloud_logging.Client(project=pid)

        time_ago = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filt = (
            'resource.type="cloud_run_revision" '
            'resource.labels.service_name="ndvi4-analysis-api" '
            f'timestamp>="{time_ago}"'
        )
        entries = client.list_entries(filter_=filt, order_by=cloud_logging.DESCENDING, max_results=limit)

        logs = [
            {
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                "severity": e.severity,
                "message": e.payload if isinstance(e.payload, str) else str(e.payload),
                "trace": e.trace,
                "labels": e.labels,
            }
            for e in entries
        ]
        return {"success": True, "count": len(logs), "logs": logs}

    except Exception as exc:
        print(f"❌ Error fetching logs: {exc}")
        return {"success": False, "error": str(exc), "logs": []}
