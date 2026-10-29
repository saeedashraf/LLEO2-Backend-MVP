"""
One-shot migration: copy application data from Supabase tables into Cloud SQL.

Run once (via ``POST /api/admin/migrate-supabase``) after deploying the
Cloud-SQL-only backend.  Every step is idempotent — rows that already exist in
Cloud SQL are updated (analyses) or skipped (likes, messages, transactions,
payments), so the endpoint can be re-run safely.

Tables migrated
---------------
analyses            → analyses            (keyed by session_id)
analysis_likes      → analysis_likes      (numeric analysis_id → session_id)
conversation_messages → conversation_messages
                      (uuid conversation_id → clean conversation session id
                       when it can be resolved through analyses.conversation_id)
user_credits        → user_credits
credit_transactions → credit_transactions
subscriptions       → subscriptions
payment_history     → payment_history
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from ..database import models as db_models
from ..database.models import (
    AnalysisLike,
    AnalysisRecord,
    ConversationMessage,
    CreditTransaction,
    PaymentHistory,
    Subscription,
    UserCredits,
)

PAGE = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ts(value) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return None


def _num(value, cast=float):
    try:
        return cast(value) if value is not None and value != "" else None
    except Exception:
        return None


_ORDER_COL = {"user_credits": "user_id", "subscriptions": "user_id"}


def _fetch_all(client, table: str) -> List[Dict[str, Any]]:
    """Page through an entire Supabase table."""
    rows: List[Dict[str, Any]] = []
    start = 0
    order = _ORDER_COL.get(table, "id")
    while True:
        q = client.table(table).select("*").order(order)
        res = q.range(start, start + PAGE - 1).execute()
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < PAGE:
            break
        start += PAGE
    return rows


def _strip_chat(cid: Optional[str]) -> Optional[str]:
    if not cid:
        return None
    return cid[5:] if cid.startswith("chat_") else cid


# ---------------------------------------------------------------------------
# Individual steps
# ---------------------------------------------------------------------------
def _migrate_analyses(s, rows: Iterable[Dict[str, Any]], report: Dict[str, Any]):
    """Returns numeric-id → session_id and uuid-conversation → conversation id maps."""
    id_map: Dict[Any, str] = {}
    conv_map: Dict[str, str] = {}
    created = updated = skipped = 0

    for r in rows:
        sid = r.get("session_id") or r.get("analysis_session_id")
        if not sid:
            skipped += 1
            continue
        id_map[r.get("id")] = sid

        meta = r.get("metadata") or {}
        if not isinstance(meta, dict):
            meta = {}
        conv_id = _strip_chat(
            meta.get("conversation_session_id")
            or meta.get("conversation_id")
            or r.get("conversation_session_id")
        )
        sb_conv_uuid = r.get("conversation_id")
        if sb_conv_uuid:
            # Supabase stored uuid5(NAMESPACE_URL, conversation session id).
            # Record the mapping when we can reverse it, otherwise keep uuid.
            if conv_id and str(uuid.uuid5(uuid.NAMESPACE_URL, conv_id)) == str(sb_conv_uuid):
                conv_map[str(sb_conv_uuid)] = conv_id
            elif conv_id is None:
                conv_id = str(sb_conv_uuid)
                conv_map[str(sb_conv_uuid)] = conv_id
            else:
                conv_map.setdefault(str(sb_conv_uuid), conv_id)

        rec = s.get(AnalysisRecord, sid)
        is_new = rec is None
        if is_new:
            rec = AnalysisRecord(session_id=sid)
            s.add(rec)

        rec.user_id = r.get("user_id") or rec.user_id or "unknown"
        rec.user_email = r.get("user_email") or rec.user_email
        rec.query = r.get("query") or rec.query
        rec.analysis_type = r.get("analysis_type") or rec.analysis_type
        rec.location_name = r.get("location") or r.get("location_name") or rec.location_name
        rec.location_lat = _num(r.get("location_lat")) if r.get("location_lat") is not None else rec.location_lat
        rec.location_lng = _num(r.get("location_lng")) if r.get("location_lng") is not None else rec.location_lng
        if rec.location_lat is not None and rec.location_lng is not None and not rec.coordinates:
            rec.coordinates = {"latitude": rec.location_lat, "longitude": rec.location_lng}
        rec.start_year = _num(r.get("start_year"), int) or rec.start_year
        rec.end_year = _num(r.get("end_year"), int) or rec.end_year
        rec.parameters = r.get("parameters") if r.get("parameters") is not None else rec.parameters
        rec.created_at = _ts(r.get("created_at")) or rec.created_at or datetime.utcnow()
        rec.status = r.get("status") or rec.status or "completed"
        rec.thumbnail_url = r.get("thumbnail_url") or rec.thumbnail_url
        rec.map_url = r.get("map_url") or rec.map_url
        rec.chart_url = r.get("chart_url") or rec.chart_url
        rec.results_data = rec.results_data or meta or None
        rec.conversation_history = (
            r.get("conversation_history")
            if r.get("conversation_history") is not None
            else rec.conversation_history
        )
        rec.conversation_id = conv_id or rec.conversation_id
        rec.processing_time_seconds = _num(r.get("processing_time_seconds")) or rec.processing_time_seconds
        rec.is_shared = bool(r.get("is_shared")) or bool(rec.is_shared)
        rec.shared_at = _ts(r.get("shared_at")) or rec.shared_at
        rec.likes = max(int(r.get("likes") or 0), int(rec.likes or 0))

        if not rec.files:
            files = {}
            for key in ("map_url", "chart_url", "thumbnail_url", "animation_url"):
                if r.get(key):
                    files[key] = r[key]
            rec.files = files or None

        if is_new:
            created += 1
        else:
            updated += 1

    s.flush()
    report["analyses"] = {"created": created, "updated": updated, "skipped": skipped}
    return id_map, conv_map


def _migrate_likes(s, rows, id_map, report):
    created = skipped = 0
    existing = {(l.session_id, l.user_id) for l in s.query(AnalysisLike).all()}
    for r in rows:
        sid = id_map.get(r.get("analysis_id"))
        uid = r.get("user_id")
        if not sid or not uid or (sid, uid) in existing:
            skipped += 1
            continue
        s.add(AnalysisLike(session_id=sid, user_id=uid, created_at=_ts(r.get("created_at")) or datetime.utcnow()))
        existing.add((sid, uid))
        created += 1
    s.flush()
    # Re-sync like counters
    for sid in {sid for sid, _ in existing}:
        rec = s.get(AnalysisRecord, sid)
        if rec is not None:
            rec.likes = s.query(AnalysisLike).filter(AnalysisLike.session_id == sid).count()
    report["analysis_likes"] = {"created": created, "skipped": skipped}


def _migrate_messages(s, rows, conv_map, report):
    created = skipped = 0
    existing = {
        (m.conversation_id, m.role, m.content, m.created_at)
        for m in s.query(ConversationMessage).all()
    }
    for r in rows:
        raw_cid = str(r.get("conversation_id") or "")
        cid = conv_map.get(raw_cid, raw_cid)
        if not cid or not r.get("content"):
            skipped += 1
            continue
        key = (cid, r.get("role") or "user", r.get("content"), _ts(r.get("created_at")))
        if key in existing:
            skipped += 1
            continue
        s.add(
            ConversationMessage(
                conversation_id=cid,
                user_id=r.get("user_id") or "unknown",
                role=r.get("role") or "user",
                content=r.get("content"),
                message_type=r.get("message_type") or "message",
                message_metadata=r.get("metadata"),
                created_at=_ts(r.get("created_at")) or datetime.utcnow(),
            )
        )
        existing.add(key)
        created += 1
    s.flush()
    report["conversation_messages"] = {"created": created, "skipped": skipped}


def _migrate_credits(s, rows, report):
    created = updated = 0
    for r in rows:
        uid = r.get("user_id")
        if not uid:
            continue
        rec = s.get(UserCredits, uid)
        if rec is None:
            rec = UserCredits(user_id=uid)
            s.add(rec)
            created += 1
        else:
            updated += 1
        rec.credits = int(r.get("credits") or 0)
        rec.total_used = int(r.get("total_used") or 0)
        rec.created_at = _ts(r.get("created_at")) or rec.created_at
        rec.updated_at = _ts(r.get("updated_at")) or rec.updated_at
    s.flush()
    report["user_credits"] = {"created": created, "updated": updated}


def _migrate_transactions(s, rows, id_map, report):
    created = skipped = 0
    existing = {
        (t.user_id, t.amount, t.transaction_type, t.created_at)
        for t in s.query(CreditTransaction).all()
    }
    for r in rows:
        uid = r.get("user_id")
        if not uid:
            skipped += 1
            continue
        key = (uid, int(r.get("amount") or 0), r.get("transaction_type") or "manual_adjustment", _ts(r.get("created_at")))
        if key in existing:
            skipped += 1
            continue
        aid = r.get("analysis_id")
        s.add(
            CreditTransaction(
                user_id=uid,
                amount=int(r.get("amount") or 0),
                transaction_type=r.get("transaction_type") or "manual_adjustment",
                description=r.get("description"),
                analysis_id=id_map.get(aid, str(aid) if aid is not None else None),
                balance_after=int(r.get("balance_after") or 0),
                created_at=_ts(r.get("created_at")) or datetime.utcnow(),
            )
        )
        existing.add(key)
        created += 1
    s.flush()
    report["credit_transactions"] = {"created": created, "skipped": skipped}


def _migrate_subscriptions(s, rows, report):
    created = updated = 0
    for r in rows:
        uid = r.get("user_id")
        if not uid:
            continue
        rec = s.get(Subscription, uid)
        if rec is None:
            rec = Subscription(user_id=uid)
            s.add(rec)
            created += 1
        else:
            updated += 1
        rec.user_email = r.get("user_email") or rec.user_email
        rec.stripe_customer_id = r.get("stripe_customer_id") or rec.stripe_customer_id
        rec.stripe_subscription_id = r.get("stripe_subscription_id") or rec.stripe_subscription_id
        rec.stripe_price_id = r.get("stripe_price_id") or rec.stripe_price_id
        rec.status = r.get("status") or rec.status or "inactive"
        rec.plan_type = r.get("plan_type") or rec.plan_type or "free"
        rec.current_period_start = _ts(r.get("current_period_start")) or rec.current_period_start
        rec.current_period_end = _ts(r.get("current_period_end")) or rec.current_period_end
        rec.cancel_at_period_end = bool(r.get("cancel_at_period_end"))
        rec.created_at = _ts(r.get("created_at")) or rec.created_at
    s.flush()
    report["subscriptions"] = {"created": created, "updated": updated}


def _migrate_payments(s, rows, report):
    created = skipped = 0
    existing_inv = {p.stripe_invoice_id for p in s.query(PaymentHistory).all() if p.stripe_invoice_id}
    existing_pi = {p.stripe_payment_intent_id for p in s.query(PaymentHistory).all() if p.stripe_payment_intent_id}
    for r in rows:
        uid = r.get("user_id")
        inv = r.get("stripe_invoice_id")
        pi = r.get("stripe_payment_intent_id")
        if not uid or (inv and inv in existing_inv) or (pi and pi in existing_pi):
            skipped += 1
            continue
        s.add(
            PaymentHistory(
                user_id=uid,
                stripe_payment_intent_id=pi,
                stripe_invoice_id=inv,
                amount=int(r.get("amount") or 0),
                currency=r.get("currency") or "usd",
                status=r.get("status") or "succeeded",
                description=r.get("description"),
                created_at=_ts(r.get("created_at")) or datetime.utcnow(),
            )
        )
        if inv:
            existing_inv.add(inv)
        if pi:
            existing_pi.add(pi)
        created += 1
    s.flush()
    report["payment_history"] = {"created": created, "skipped": skipped}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def run_migration(client, tables: Optional[List[str]] = None) -> Dict[str, Any]:
    """Copy data from Supabase (``client``) into Cloud SQL. Returns a report."""
    if db_models.SessionLocal is None:
        raise RuntimeError("Database not initialised")
    if client is None:
        raise RuntimeError("Supabase client not configured (SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY)")

    wanted = set(tables or [
        "analyses", "analysis_likes", "conversation_messages",
        "user_credits", "credit_transactions", "subscriptions", "payment_history",
    ])
    report: Dict[str, Any] = {"started_at": datetime.utcnow().isoformat(), "errors": {}}

    def fetch(table):
        try:
            rows = _fetch_all(client, table)
            report.setdefault("fetched", {})[table] = len(rows)
            return rows
        except Exception as exc:
            report["errors"][table] = str(exc)
            return []

    s = db_models.SessionLocal()
    try:
        # analyses always run first — the other tables need its id maps.
        analyses_rows = fetch("analyses")
        id_map, conv_map = _migrate_analyses(s, analyses_rows, report) if analyses_rows else ({}, {})

        if "analysis_likes" in wanted:
            _migrate_likes(s, fetch("analysis_likes"), id_map, report)
        if "conversation_messages" in wanted:
            _migrate_messages(s, fetch("conversation_messages"), conv_map, report)
        if "user_credits" in wanted:
            _migrate_credits(s, fetch("user_credits"), report)
        if "credit_transactions" in wanted:
            _migrate_transactions(s, fetch("credit_transactions"), id_map, report)
        if "subscriptions" in wanted:
            _migrate_subscriptions(s, fetch("subscriptions"), report)
        if "payment_history" in wanted:
            _migrate_payments(s, fetch("payment_history"), report)
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()

    report["finished_at"] = datetime.utcnow().isoformat()
    return report
