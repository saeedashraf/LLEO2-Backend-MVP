"""
Integration tests for the Cloud-SQL-backed data endpoints (lleo.routers.data)
and the credits service, using an in-memory SQLite database.

No Supabase, GCS or Earth Engine involved.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from lleo.database import models as db_models
from lleo.database.models import AnalysisRecord, Base, ConversationMessage
from lleo.routers import data as data_router
from lleo.services import credits_service
from lleo.services.conversation_service import save_message


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    db_models._run_migrations(engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(db_models, "engine", engine)
    monkeypatch.setattr(db_models, "SessionLocal", SessionLocal)
    yield SessionLocal
    engine.dispose()


@pytest.fixture()
def client(db):
    app = FastAPI()
    app.include_router(data_router.router)
    return TestClient(app)


def _seed_analysis(SessionLocal, sid, user_id="u1", shared=False, conv="conv-1", **extra):
    s = SessionLocal()
    rec = AnalysisRecord(
        session_id=sid,
        user_id=user_id,
        user_email=f"{user_id}@example.com",
        query=f"query {sid}",
        analysis_type="vegetation_index",
        location_name="Tehran",
        location_lat=35.7,
        location_lng=51.4,
        start_year=2020,
        end_year=2023,
        files={"map": "/results/%s/map.html" % sid, "charts": "/results/%s/charts.png" % sid},
        conversation_id=conv,
        is_shared=shared,
        shared_at=datetime.utcnow() if shared else None,
        created_at=extra.pop("created_at", datetime.utcnow()),
        **extra,
    )
    s.add(rec)
    s.commit()
    s.close()


# ── analyses / community ─────────────────────────────────────────────────────

def test_list_my_analyses_is_user_scoped_and_paginated(client, db):
    now = datetime.utcnow()
    for i in range(5):
        _seed_analysis(db, f"a{i}", user_id="u1", created_at=now - timedelta(minutes=i))
    _seed_analysis(db, "other", user_id="u2")

    r = client.get("/api/analyses", params={"user_id": "u1", "page": 1, "per_page": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert body["has_more"] is True
    assert [it["id"] for it in body["items"]] == ["a0", "a1", "a2"]  # newest first
    first = body["items"][0]
    assert first["id"] == first["session_id"]            # id is the session_id string
    assert first["location"] == "Tehran"
    assert first["user_email"] == "u1@example.com"
    assert first["is_shared"] is False and first["likes"] == 0

    r2 = client.get("/api/analyses", params={"user_id": "u1", "page": 2, "per_page": 3})
    assert [it["id"] for it in r2.json()["items"]] == ["a3", "a4"]
    assert r2.json()["has_more"] is False


def test_share_analysis_and_community_listing(client, db):
    _seed_analysis(db, "s1", user_id="u1")
    _seed_analysis(db, "s2", user_id="u1")

    assert client.get("/api/community").json()["total"] == 0

    r = client.patch("/api/analyses/s1/share", json={"user_id": "u1", "is_shared": True})
    assert r.status_code == 200
    assert r.json()["is_shared"] is True and r.json()["shared_at"]

    # wrong owner → 404
    r = client.patch("/api/analyses/s2/share", json={"user_id": "hacker", "is_shared": True})
    assert r.status_code == 404

    comm = client.get("/api/community").json()
    assert comm["total"] == 1 and comm["items"][0]["id"] == "s1"

    # unshare
    client.patch("/api/analyses/s1/share", json={"user_id": "u1", "is_shared": False})
    assert client.get("/api/community").json()["total"] == 0


def test_share_whole_conversation(client, db):
    _seed_analysis(db, "c1", conv="conv-A")
    _seed_analysis(db, "c2", conv="conv-A")
    _seed_analysis(db, "c3", conv="conv-B")

    r = client.patch("/api/conversations/chat_conv-A/share", json={"user_id": "u1", "is_shared": True})
    assert r.status_code == 200
    assert r.json() == {"conversation_id": "conv-A", "is_shared": True, "updated": 2}
    ids = {it["id"] for it in client.get("/api/community").json()["items"]}
    assert ids == {"c1", "c2"}


def test_like_toggle_updates_counter_and_liked_flag(client, db):
    _seed_analysis(db, "L1", user_id="u1", shared=True)

    r = client.post("/api/analyses/L1/like", json={"user_id": "u2"})
    assert r.json() == {"session_id": "L1", "liked": True, "likes": 1}
    r = client.post("/api/analyses/L1/like", json={"user_id": "u3"})
    assert r.json()["likes"] == 2

    comm = client.get("/api/community", params={"user_id": "u2"}).json()
    assert comm["items"][0]["likes"] == 2
    assert comm["items"][0]["liked"] is True
    assert comm["liked"] == ["L1"]

    # toggle off
    r = client.post("/api/analyses/L1/like", json={"user_id": "u2"})
    assert r.json() == {"session_id": "L1", "liked": False, "likes": 1}

    assert client.post("/api/analyses/nope/like", json={"user_id": "u2"}).status_code == 404


# ── conversation messages ────────────────────────────────────────────────────

def test_conversation_messages_roundtrip(client, db):
    save_message("chat_conv-X", "u1", "user", "hello", "question")
    save_message("conv-X", "u1", "assistant", "hi there", "answer", {"k": 1})

    r = client.get("/api/conversations/chat_conv-X/messages")
    assert r.status_code == 200
    body = r.json()
    assert body["conversation_id"] == "conv-X"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["metadata"] == {"k": 1}

    s = db()
    assert s.query(ConversationMessage).count() == 2
    s.close()


# ── credits ──────────────────────────────────────────────────────────────────

def test_credits_auto_created_and_deducted_idempotently(client, db):
    r = client.get("/api/credits/newuser")
    assert r.status_code == 200
    assert r.json()["credits"] == db_models.SIGNUP_CREDITS

    r = client.post("/api/credits/deduct", json={"user_id": "newuser", "amount": 1, "analysis_id": "A1"})
    assert r.status_code == 200
    assert r.json()["credits"] == db_models.SIGNUP_CREDITS - 1

    # same analysis again → not charged twice
    r = client.post("/api/credits/deduct", json={"user_id": "newuser", "amount": 1, "analysis_id": "A1"})
    assert r.status_code == 200
    assert r.json()["credits"] == db_models.SIGNUP_CREDITS - 1

    summary = client.get("/api/billing/summary/newuser").json()
    assert summary["credits"]["credits"] == db_models.SIGNUP_CREDITS - 1
    assert summary["credits"]["total_used"] == 1
    assert len(summary["transactions"]) >= 1


def test_deduct_fails_when_insufficient(client, db):
    credits_service.ensure_user_credits("poor")
    res = credits_service.deduct_credits("poor", db_models.SIGNUP_CREDITS)
    assert res["success"] is True and res["credits"] == 0
    r = client.post("/api/credits/deduct", json={"user_id": "poor", "amount": 1})
    assert r.status_code == 402


def test_add_credits_and_subscription_upsert(db):
    credits_service.ensure_user_credits("sub")
    assert credits_service.add_credits("sub", 10, "subscription_renewal", "renewal") is True
    assert credits_service.get_user_credits("sub")["credits"] == db_models.SIGNUP_CREDITS + 10

    credits_service.update_subscription(
        "sub", status="active", plan_type="pro",
        stripe_customer_id="cus_1", stripe_subscription_id="sub_1", user_email="s@x.com",
    )
    sub = credits_service.get_subscription("sub")
    assert sub["status"] == "active" and sub["plan_type"] == "pro"
    assert credits_service.get_subscription_by_stripe_id("sub_1")["user_id"] == "sub"

    credits_service.update_subscription("sub", status="canceled", cancel_at_period_end=True)
    sub = credits_service.get_subscription("sub")
    assert sub["status"] == "canceled" and sub["cancel_at_period_end"] is True
