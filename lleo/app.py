"""
LLEO v2 — FastAPI application.

Slim entry point: CORS, startup/shutdown events, include routers.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import services
from .routers import chat, results, billing, health


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: init services. Shutdown: cleanup."""
    await services.initialise()
    print("✅ LLEO v2 ready")
    yield
    print("👋 LLEO v2 shutting down")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="LLEO — Large Language Models for Earth Observation",
    description="Satellite imagery analysis powered by Gemini + Earth Engine",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS — allow any origin (frontend is on a different domain)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────

app.include_router(health.router)    # GET /, /health, /processors, /api/logs
app.include_router(chat.router)      # POST /chat/start, /chat/continue, /chat/analyze
app.include_router(results.router)   # POST /analyze, GET /status/*, /results/*, /gallery/*
app.include_router(billing.router)   # POST /api/stripe/webhook, /api/billing/*
