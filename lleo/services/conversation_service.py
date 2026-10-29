"""
LLEO v2 — Conversation service.

Thin wrapper around the existing ConversationManager and the
Cloud SQL ``conversation_messages`` table.
"""

from __future__ import annotations

from typing import Optional

from ..routing.conversation_manager import (
    ConversationState,
    ConversationManager,
    conversation_manager,        # singleton already instantiated
    parse_parameter_selection,
)


# Re-export the singleton so routers do:
#   from ..services.conversation_service import conversation_mgr
conversation_mgr = conversation_manager


_ROLE_TO_TYPE = {
    "user": "question",
    "assistant": "answer",
    "system": "clarification",
}
_ALLOWED_TYPES = ("question", "answer", "clarification", "analysis_result")


def normalise_conversation_id(conversation_id: str) -> str:
    """``chat_<id>`` and ``<id>`` refer to the same conversation."""
    if not conversation_id:
        return conversation_id
    return conversation_id[5:] if conversation_id.startswith("chat_") else conversation_id


def save_message(
    conversation_id: str,
    user_id: str,
    role: str,
    content: str,
    message_type: str = "message",
    metadata: Optional[dict] = None,
) -> None:
    """
    Persist a message to the Cloud SQL ``conversation_messages`` table
    AND update the ConversationState so that build_conversation_context()
    sees the full history.  Failures are logged but never raised.
    """
    # ── Update conversation state so context stays current ──
    state = conversation_mgr.get_conversation(conversation_id)
    if state:
        state.add_message(role, content)
        conversation_mgr.update_conversation(conversation_id, state)

    db_type = message_type if message_type in _ALLOWED_TYPES else _ROLE_TO_TYPE.get(role, "question")
    clean_id = normalise_conversation_id(conversation_id)

    try:
        from ..database import models as db_models
        if db_models.SessionLocal is None:
            return
        s = db_models.SessionLocal()
        try:
            s.add(db_models.ConversationMessage(
                conversation_id=clean_id,
                user_id=user_id,
                role=role,
                content=content,
                message_type=db_type,
                message_metadata=metadata or None,
            ))
            s.commit()
            print(f"✅ Saved {role} msg → {clean_id} ({db_type})")
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
    except Exception as exc:
        print(f"⚠️  Failed to save conversation message: {exc}")


def list_messages(conversation_id: str, limit: int = 500) -> list:
    """Chronological message list for a conversation (My Analyses timeline)."""
    from ..database import models as db_models
    if db_models.SessionLocal is None:
        return []
    clean_id = normalise_conversation_id(conversation_id)
    s = db_models.SessionLocal()
    try:
        rows = (
            s.query(db_models.ConversationMessage)
            .filter(db_models.ConversationMessage.conversation_id == clean_id)
            .order_by(db_models.ConversationMessage.created_at.asc(),
                      db_models.ConversationMessage.id.asc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "conversation_id": r.conversation_id,
                "user_id": r.user_id,
                "role": r.role,
                "content": r.content,
                "message_type": r.message_type,
                "metadata": r.message_metadata,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    finally:
        s.close()


def build_conversation_context(session_id: str) -> str:
    """Format the last N messages for the router prompt."""
    state = conversation_mgr.get_conversation(session_id)
    if not state or not state.messages:
        return ""
    return "\n".join(
        f"{m['role']}: {m['content']}" for m in state.messages[-6:]
    )


def extract_prior_fields(session_id: str) -> dict:
    """Pull known fields into a flat dict for the router."""
    state = conversation_mgr.get_conversation(session_id)
    if not state:
        return {}
    p = state.parameters or {}

    # Recover start_month / end_month — stored in params or derived from specific_months
    sm = p.get("start_month")
    em = p.get("end_month")
    if sm is None and state.specific_months and len(state.specific_months) >= 1:
        sm = state.specific_months[0]
    if em is None and state.specific_months and len(state.specific_months) >= 2:
        em = state.specific_months[1]

    return {
        "analysis_type": state.analysis_type,
        "location_name": state.location,
        "latitude": getattr(state, "latitude", None) or p.get("latitude"),
        "longitude": getattr(state, "longitude", None) or p.get("longitude"),
        "start_year": state.start_year,
        "end_year": state.end_year,
        "start_month": sm,
        "end_month": em,
        "specific_months": state.specific_months,
        "business_type": p.get("business_type"),
        "keyword": p.get("keyword"),
        "radius": p.get("radius"),
        "pollutants": p.get("pollutants"),
        "water_indicators": p.get("water_indicators"),
        "vegetation_index_type": p.get("index_type"),
    }


def apply_decision_to_state(session_id: str, decision) -> None:
    """Write RoutingDecision fields back into ConversationState."""
    state = conversation_mgr.get_conversation(session_id)
    if not state:
        return
    if decision.intent:
        state.analysis_type = decision.intent.value
    if decision.location_name:
        state.location = decision.location_name
    if decision.latitude is not None:
        params = state.parameters or {}
        params["latitude"] = decision.latitude
        state.parameters = params
    if decision.longitude is not None:
        params = state.parameters or {}
        params["longitude"] = decision.longitude
        state.parameters = params
    if decision.start_year is not None:
        state.start_year = decision.start_year
    if decision.end_year is not None:
        state.end_year = decision.end_year
    if decision.start_month is not None or decision.end_month is not None:
        state.specific_months = [decision.start_month, decision.end_month]
        # Also store individually for reliable round-tripping
        params = state.parameters or {}
        if decision.start_month is not None:
            params["start_month"] = decision.start_month
        if decision.end_month is not None:
            params["end_month"] = decision.end_month
        state.parameters = params

    params = state.parameters or {}
    if decision.business_type:
        params["business_type"] = decision.business_type
    if decision.keyword:
        params["keyword"] = decision.keyword
    if decision.radius and decision.radius != 2000:
        params["radius"] = decision.radius
    if decision.pollutants:
        params["pollutants"] = decision.pollutants
        params["selected_parameters"] = decision.pollutants
    if decision.water_indicators:
        params["water_indicators"] = decision.water_indicators
        params["selected_parameters"] = decision.water_indicators
    if decision.vegetation_index_type:
        params["index_type"] = decision.vegetation_index_type

    state.parameters = params
    state.needs_clarification = decision.needs_clarification or []
    state.is_complete = (
        len(state.needs_clarification) == 0
        and decision.confidence.value in ("high", "medium")
        and decision.intent.value != "general_question"
    )

    # Persist updated state to Supabase so other Cloud Run instances see it
    conversation_mgr.update_conversation(session_id, state)
    print(f"💾 apply_decision_to_state: {session_id} → "
          f"intent={state.analysis_type}, location={state.location}, "
          f"complete={state.is_complete}")
