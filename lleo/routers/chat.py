"""
LLEO v2 — Conversational chat endpoints.

POST /chat/start       — begin a new conversation
POST /chat/continue    — send a follow-up message
POST /chat/analyze     — trigger analysis after conversation is complete
GET  /chat/{session_id} — retrieve conversation state
"""

from __future__ import annotations

import secrets as _secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException

from ..config import services
from ..models.routing import RoutingDecision
from ..services.router_service import route_query
from ..services.geocoding_service import build_location_from_decision, flatten_location
from ..services.conversation_service import (
    conversation_mgr,
    save_message,
    build_conversation_context,
    extract_prior_fields,
    apply_decision_to_state,
)
from ..services.analysis_service import (
    ANALYSIS_CREDIT_COST,
    build_analysis_request,
    build_file_urls,
    create_processor,
    process_analysis_background,
)
from ..services import credits_service

router = APIRouter(prefix="/chat", tags=["chat"])

# Cache user_id → user_email for persisting analyses
_user_emails: dict[str, str] = {}


def _check_credits_or_raise(user_id: str) -> None:
    """Server-side guard: refuse to queue an analysis if the user is out of credits.

    Anonymous / unknown users are allowed through (nothing to charge).
    Any DB error is logged and ignored so that credit bookkeeping never blocks
    an analysis unexpectedly.
    """
    if not user_id or user_id in ("unknown", "anonymous"):
        return
    try:
        row = credits_service.ensure_user_credits(user_id)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"⚠️  Credit pre-check skipped ({exc})")
        return
    if not row:
        return
    credits = int(row.get("credits", 0) or 0)
    if credits < ANALYSIS_CREDIT_COST:
        raise HTTPException(
            status_code=402,
            detail=f"Insufficient credits ({credits} left). Please purchase more credits to continue.",
        )


# ── POST /chat/start ─────────────────────────────────────────────────────────

@router.post("/start")
async def start_conversation(
    background_tasks: BackgroundTasks,
    query: str = Form(...),
    user_id: str = Form(...),
    user_email: str = Form(""),
):
    """Start a new conversational session — returns routing decision."""
    try:
        session_id = _new_session_id()

        # Store user_email for later use when saving analysis
        if user_email:
            _user_emails[user_id] = user_email

        # Create conversation state in the existing manager
        conversation_mgr.create_conversation(session_id, user_id, query)

        # Single-call routing
        decision: RoutingDecision = await route_query(query)

        # Persist messages
        save_message(session_id, user_id, "user", query)
        reply = decision.clarification_message or decision.general_answer or ""
        if not reply:
            ready = (
                decision.confidence.value == "high"
                and not decision.needs_clarification
                and decision.intent.value != "general_question"
            )
            if ready:
                loc_part = f" for {decision.location_name}" if decision.location_name else ""
                reply = f"Got it! Starting {decision.intent.value.replace('_', ' ')} analysis{loc_part}…"
        if reply:
            save_message(session_id, user_id, "assistant", reply)

        # Update state
        apply_decision_to_state(session_id, decision)

        # Build response — frontend will call /chat/analyze when status is "complete"
        resp = _decision_to_response(session_id, decision)

        return resp

    except HTTPException:
        raise
    except Exception as exc:
        print(f"❌ /chat/start error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /chat/continue ──────────────────────────────────────────────────────

@router.post("/continue")
async def continue_conversation(
    background_tasks: BackgroundTasks,
    conversation_id: str = Form(None),
    session_id: str = Form(None),
    user_response: str = Form(None),
    message: str = Form(None),
):
    """Continue an existing conversation with a follow-up message."""
    try:
        # Accept both old (conversation_id/user_response) and new (session_id/message) field names
        sid = conversation_id or session_id
        msg = user_response or message
        if not sid:
            raise HTTPException(status_code=400, detail="conversation_id or session_id is required")
        if not msg:
            raise HTTPException(status_code=400, detail="user_response or message is required")

        state = conversation_mgr.get_conversation(sid)
        if not state:
            raise HTTPException(status_code=404, detail="Session not found or expired")

        user_id = state.user_id if hasattr(state, "user_id") else "unknown"

        # Merge prior context
        prior = extract_prior_fields(sid)
        ctx = build_conversation_context(sid)
        prior_intent = prior.get("analysis_type")  # e.g. "air_quality"

        print(f"💬 /chat/continue — prior_intent={prior_intent}, "
              f"location={prior.get('location_name')}, "
              f"context_len={len(ctx)}")
        if ctx:
            print(f"   Context preview: {ctx[:300]}")

        decision: RoutingDecision = await route_query(
            msg, conversation_context=ctx,
            prior_intent=prior_intent, prior_fields=prior
        )

        save_message(sid, user_id, "user", msg)
        reply = decision.clarification_message or decision.general_answer or ""
        if not reply:
            # Build the "Got it! Starting…" message so it appears in timeline
            ready = (
                decision.confidence.value == "high"
                and not decision.needs_clarification
                and decision.intent.value != "general_question"
            )
            if ready:
                loc_part = f" for {decision.location_name}" if decision.location_name else ""
                reply = f"Got it! Starting {decision.intent.value.replace('_', ' ')} analysis{loc_part}…"
        if reply:
            save_message(sid, user_id, "assistant", reply)

        apply_decision_to_state(sid, decision)

        resp = _decision_to_response(sid, decision)

        # Frontend will call /chat/analyze when it sees status "complete"
        resp = _decision_to_response(sid, decision)

        return resp

    except HTTPException:
        raise
    except Exception as exc:
        print(f"❌ /chat/continue error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── POST /chat/analyze ───────────────────────────────────────────────────────

@router.post("/analyze")
async def analyze_from_conversation(
    background_tasks: BackgroundTasks,
    conversation_id: str = Form(None),
    session_id: str = Form(None),
):
    """Explicitly trigger analysis for a session that is already complete."""
    try:
        sid = conversation_id or session_id
        if not sid:
            raise HTTPException(status_code=400, detail="conversation_id or session_id is required")

        state = conversation_mgr.get_conversation(sid)
        if not state:
            raise HTTPException(status_code=404, detail="Session not found")

        user_id = state.user_id if hasattr(state, "user_id") else "unknown"

        # ── Reconstruct RoutingDecision from stored state (no 2nd Gemini call) ──
        prior = extract_prior_fields(sid)
        intent_str = prior.get("analysis_type") or prior.get("intent") or "general_question"
        decision = RoutingDecision(
            intent=intent_str,
            confidence="high",
            location_name=prior.get("location_name"),
            latitude=prior.get("latitude"),
            longitude=prior.get("longitude"),
            start_year=prior.get("start_year"),
            end_year=prior.get("end_year"),
            start_month=prior.get("start_month"),
            end_month=prior.get("end_month"),
            business_type=prior.get("business_type"),
            keyword=prior.get("keyword"),
            radius=prior.get("radius") or 2000,
            pollutants=prior.get("pollutants"),
            water_indicators=prior.get("water_indicators"),
            vegetation_index_type=prior.get("vegetation_index_type"),
        )

        # If we couldn't reconstruct intent from state, fall back to a quick Gemini call
        if intent_str == "general_question" and (prior.get("location_name") or prior.get("latitude")):
            ctx = build_conversation_context(sid)
            decision = await route_query(
                "proceed with the analysis",
                conversation_context=ctx,
                prior_intent=intent_str,
                prior_fields=prior,
            )

        orig_query = ""
        if hasattr(state, "messages") and state.messages:
            for m in state.messages:
                if isinstance(m, dict) and m.get("role") == "user":
                    orig_query = m.get("content", "")
                    break
        if not orig_query:
            ctx = build_conversation_context(sid)
            orig_query = ctx.split("User: ")[1].split("\n")[0] if "User: " in ctx else "analysis"

        bg_sid = await _launch_analysis(
            background_tasks, sid, user_id, orig_query, decision
        )

        return {
            # Frontend polls analyzeData.session_id — must be the ANALYSIS sid
            "session_id": bg_sid,
            "conversation_id": sid,
            "analysis_session_id": bg_sid,
            "status": "analyzing",
            "analysis_type": decision.intent.value,
            "location": decision.location_name,
            "message": "Analysis started in background.",
            # Job is still running — only type-independent URLs are known yet.
            # Poll /status/{session_id} for the final per-type file list.
            "files": build_file_urls(bg_sid),
        }

    except HTTPException:
        raise
    except Exception as exc:
        print(f"❌ /chat/analyze error: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))


# ── GET /chat/{session_id} ───────────────────────────────────────────────────

@router.get("/{session_id}")
async def get_session(session_id: str):
    """Return current conversation state."""
    state = conversation_mgr.get_conversation(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session_id,
        "user_id": getattr(state, "user_id", None),
        "messages": getattr(state, "messages", []),
        "analysis_type": getattr(state, "analysis_type", None),
        "location": getattr(state, "location", None),
        "status": getattr(state, "status", "active"),
    }


# ── Private helpers ──────────────────────────────────────────────────────────

def _new_session_id() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{ts}_{_secrets.token_urlsafe(8)}"


def _decision_to_response(session_id: str, d: RoutingDecision) -> dict:
    ready = (
        d.confidence.value == "high"
        and not d.needs_clarification
        and d.intent.value != "general_question"
    )

    # Build a meaningful message (avoid empty string → frontend would show "Processing...")
    msg = d.clarification_message or d.general_answer or ""
    if not msg and ready:
        loc_part = f" for {d.location_name}" if d.location_name else ""
        msg = f"Got it! Starting {d.intent.value.replace('_', ' ')} analysis{loc_part}…"

    return {
        "session_id": session_id,
        "status": "complete" if ready else (
            "general_answer" if d.intent.value == "general_question" else "needs_clarification"
        ),
        "message": msg,
        "intent": d.intent.value,
        "location": d.location_name,
        "latitude": d.latitude,
        "longitude": d.longitude,
        "start_year": d.start_year,
        "end_year": d.end_year,
        "confidence": d.confidence.value,
        "needs_clarification": d.needs_clarification,
        "ready_to_analyze": ready,
        # Compatibility aliases
        "conversation_id": session_id,
        "extracted_location": d.location_name,
        "extracted_time": f"{d.start_year}-{d.end_year}" if d.start_year else None,
        "next_question": d.clarification_message if d.needs_clarification else None,
    }


async def _launch_analysis(
    background_tasks: BackgroundTasks,
    conv_session_id: str,
    user_id: str,
    query: str,
    decision: RoutingDecision,
) -> str:
    """Create analysis request, processor, queue background work, return analysis session_id."""
    _check_credits_or_raise(user_id)
    analysis_sid = _new_session_id()

    # Build location dict (lat/lon from decision, geocode if missing)
    nested_loc = build_location_from_decision(decision)
    if nested_loc:
        loc = flatten_location(nested_loc, fallback_name=decision.location_name or "Unknown")
    else:
        loc = {
            "name": decision.location_name or "Unknown",
            "latitude": decision.latitude or 0,
            "longitude": decision.longitude or 0,
        }

    # Build time dict
    time_period = {}
    if decision.start_year:
        time_period["start_year"] = decision.start_year
    if decision.end_year:
        time_period["end_year"] = decision.end_year
    if decision.start_month:
        time_period["start_month"] = decision.start_month
    if decision.end_month:
        time_period["end_month"] = decision.end_month

    # ── Satellite data-availability guard ────────────────────────────
    # Clamp start_year to the earliest date the satellite actually has data.
    _EARLIEST = {
        "vegetation_index": 2015,
        "water_quality":    2015,
        "video_animation":  2015,
        "air_quality":      2018,
    }
    intent_val = decision.intent.value
    earliest = _EARLIEST.get(intent_val)
    if earliest and time_period.get("start_year") and time_period["start_year"] < earliest:
        old_yr = time_period["start_year"]
        time_period["start_year"] = earliest
        print(
            f"⚠️  Clamped start_year {old_yr} → {earliest} "
            f"(satellite data for {intent_val} not available before {earliest})"
        )
    current_year = datetime.now().year
    if time_period.get("end_year") and time_period["end_year"] > current_year:
        time_period["end_year"] = current_year

    # ── Time-span guard: cap excessively long ranges ─────────────
    _MAX_SPAN = 5  # years
    if (
        earliest
        and time_period.get("start_year")
        and time_period.get("end_year")
    ):
        span = time_period["end_year"] - time_period["start_year"]
        if span > _MAX_SPAN:
            old_start = time_period["start_year"]
            time_period["start_year"] = time_period["end_year"] - _MAX_SPAN
            print(
                f"⚠️  Clamped time span from {span}yr → {_MAX_SPAN}yr "
                f"(start_year {old_start} → {time_period['start_year']})"
            )
    # ─────────────────────────────────────────────────────────────────

    # Build parameters dict
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
        session_id=analysis_sid,
        user_id=user_id,
        query=query,
        analysis_type=decision.intent.value,
        location=loc,
        time_period=time_period,
        parameters=params,
        conversation_session_id=conv_session_id,
    )

    # Attach user_email for Supabase persistence
    req.user_email = _user_emails.get(user_id, "")  # type: ignore[attr-defined]

    # Register in-memory session for status polling
    services.analysis_sessions[analysis_sid] = {
        "status": "processing",
        "progress": 0,
        "message": "Queued…",
        "user_id": user_id,
        "analysis_type": decision.intent.value,
        "location": decision.location_name,
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

    print(f"🚀 Queued analysis {analysis_sid} ({decision.intent.value})")
    return analysis_sid
