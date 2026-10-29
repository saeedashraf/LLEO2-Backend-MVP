"""Run a curated set of showcase questions as the Sureal.ai user and publish them.

Usage (from satellite-backend-2):
    python scripts/sureal_questions.py run       # start all analyses, poll to completion, write results json
    python scripts/sureal_questions.py verify    # check preview/map/chart bytes for completed sessions
    python scripts/sureal_questions.py publish   # publish completed sessions as "Sureal.ai"

Environment:
    LLEO_API        base URL (default: prod Cloud Run)
    LLEO_ADMIN_KEY  X-Admin-Key for publish step
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

API = os.environ.get("LLEO_API", "https://ndvi6-analysis-api-258557095482.us-central1.run.app").rstrip("/")
ADMIN_KEY = os.environ.get("LLEO_ADMIN_KEY", "").strip()
USER_ID = "sureal-ai"
USER_EMAIL = "team@sureal.ai"
AUTHOR = "Sureal.ai"
STATE_FILE = Path(__file__).with_name("sureal_questions_state.json")

QUESTIONS = [
    # vegetation_index
    "Show NDVI vegetation change in Los Angeles between 2023 and 2024",
    "Analyze deforestation and vegetation loss in Mato Grosso, Brazil from 2019 to 2024",
    "How has vegetation health changed in Bali, Indonesia from 2020 to 2024?",
    # water_quality
    "Analyze water quality and algae blooms in Lake Erie for 2024",
    "Show water quality of Lake Victoria, Africa in 2023",
    # air_quality
    "Analyze NO2 air pollution in Delhi, India for 2024",
    "Show air quality trends in Tehran, Iran between 2022 and 2024",
    # weather_forecast
    "What is the weather forecast for New York City this week?",
    # business_location
    "Find the best locations to open a coffee shop in San Francisco",
    # video_animation
    "Create a satellite timelapse animation of Dubai from 2022 to 2024",
]


def _load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"items": []}


def _save(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _chat_start(query: str) -> dict:
    r = requests.post(
        f"{API}/chat/start",
        data={"query": query, "user_id": USER_ID, "user_email": USER_EMAIL},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def _chat_continue(session_id: str, message: str) -> dict:
    r = requests.post(
        f"{API}/chat/continue",
        data={"session_id": session_id, "message": message, "user_id": USER_ID, "user_email": USER_EMAIL},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def _chat_analyze(session_id: str) -> dict:
    r = requests.post(
        f"{API}/chat/analyze",
        data={"session_id": session_id, "user_id": USER_ID, "user_email": USER_EMAIL},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


def _status(session_id: str) -> dict:
    r = requests.get(f"{API}/status/{session_id}", timeout=60)
    if r.status_code == 404:
        return {"status": "not_found"}
    r.raise_for_status()
    return r.json()


def cmd_run() -> None:
    state = _load()
    done_queries = {it["query"] for it in state["items"]}
    for q in QUESTIONS:
        if q in done_queries:
            continue
        print(f"\n=== {q}")
        try:
            resp = _chat_start(q)
        except Exception as exc:  # noqa: BLE001
            print("  chat/start failed:", exc)
            state["items"].append({"query": q, "error": f"start: {exc}"})
            _save(state)
            continue
        conv_sid = resp.get("session_id")
        status = resp.get("status")
        tries = 0
        # Answer clarifications with a generic "use your best judgement" reply (max 2 rounds)
        while status in ("needs_clarification", "clarification") and tries < 2:
            print("  clarification asked:", (resp.get("message") or "")[:160])
            resp = _chat_continue(conv_sid, "Use your best judgement and reasonable defaults for the whole region.")
            status = resp.get("status")
            tries += 1
        print("  conversation status:", status)
        if status not in ("complete", "ready", "ready_to_analyze"):
            state["items"].append({"query": q, "conv_session": conv_sid, "error": f"conv status {status}", "resp": resp})
            _save(state)
            continue
        try:
            a = _chat_analyze(conv_sid)
        except Exception as exc:  # noqa: BLE001
            print("  chat/analyze failed:", exc)
            state["items"].append({"query": q, "conv_session": conv_sid, "error": f"analyze: {exc}"})
            _save(state)
            continue
        sid = a.get("session_id") or a.get("analysis_session_id")
        print("  analysis session:", sid, "| type:", a.get("analysis_type"))
        state["items"].append({"query": q, "conv_session": conv_sid, "session_id": sid, "analysis_type": a.get("analysis_type")})
        _save(state)

    # Poll
    pending = [it for it in state["items"] if it.get("session_id") and it.get("final_status") not in ("completed", "failed")]
    deadline = time.time() + 60 * 40
    while pending and time.time() < deadline:
        for it in list(pending):
            st = _status(it["session_id"])
            s = st.get("status")
            it["last_status"] = s
            it["progress"] = st.get("progress")
            if s in ("completed", "failed", "error", "not_found"):
                it["final_status"] = "completed" if s == "completed" else "failed"
                it["status_payload"] = st
                print(f"  [{it['final_status']}] {it['query'][:60]} -> {it['session_id']}")
                pending.remove(it)
        _save(state)
        if pending:
            time.sleep(20)
    print("\nDone. Completed:", sum(1 for it in state["items"] if it.get("final_status") == "completed"), "/", len(QUESTIONS))


def cmd_verify() -> None:
    state = _load()
    for it in state["items"]:
        sid = it.get("session_id")
        if not sid or it.get("final_status") != "completed":
            continue
        row = []
        for ep in ("preview", "map", "chart"):
            try:
                r = requests.get(f"{API}/results/{sid}/{ep}", timeout=60)
                row.append(f"{ep}={r.status_code}/{len(r.content)}B")
            except Exception as exc:  # noqa: BLE001
                row.append(f"{ep}=ERR {exc}")
        print(sid, it.get("analysis_type"), " ".join(row))


def cmd_publish() -> None:
    global ADMIN_KEY
    if not ADMIN_KEY:
        key_file = Path(os.environ.get("TEMP", "/tmp")) / "adminkey.txt"
        if key_file.exists():
            ADMIN_KEY = key_file.read_text(encoding="utf-8").strip().lstrip("\ufeff")
    if not ADMIN_KEY:
        sys.exit("LLEO_ADMIN_KEY not set")
    state = _load()
    sids = [it["session_id"] for it in state["items"] if it.get("session_id") and it.get("final_status") == "completed"]
    if not sids:
        sys.exit("nothing to publish")
    r = requests.post(
        f"{API}/api/admin/publish",
        headers={"X-Admin-Key": ADMIN_KEY},
        json={"session_ids": sids, "author_name": AUTHOR, "is_shared": True},
        timeout=120,
    )
    print(r.status_code, r.text[:1000])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    {"run": cmd_run, "verify": cmd_verify, "publish": cmd_publish}[cmd]()
