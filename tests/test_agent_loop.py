"""
Unit tests for the plan → act → observe → re-plan loop in
lleo.services.agent_service.

No Earth Engine, GCS or Gemini: the tool (run_fn) and the LLM planner
(replan_fn) are injected.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from lleo.core.base_processor import AnalysisRequest, AnalysisResult
from lleo.services import agent_service as ag
from lleo.services import analysis_service as svc


# ── helpers ──────────────────────────────────────────────────────────────────

def _req(atype="vegetation_index", tp=None, params=None) -> AnalysisRequest:
    return AnalysisRequest(
        session_id="sid",
        user_id="u",
        query="how did vegetation change?",
        analysis_type=atype,
        location={"name": "X", "latitude": 0.0, "longitude": 0.0},
        time_period=tp if tp is not None else {"start_year": 2020, "end_year": 2024},
        parameters=params if params is not None else {},
    )


def _res(atype="vegetation_index", metadata=None, status="completed") -> AnalysisResult:
    return AnalysisResult(
        session_id="sid",
        status=status,
        analysis_type=atype,
        location={},
        time_period={},
        outputs={"map": "gs://b/m.html"},
        metadata=metadata if metadata is not None else {"statistics": {"mean": 0.4}},
        processing_time=1.0,
    )


class _Cfg:
    cloud_cover_threshold = 20


class _Proc:
    """Scripted tool: each call pops the next outcome (result or exception)."""
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.config = _Cfg()

    def run(self, request):
        self.calls.append((dict(request.time_period), dict(request.parameters),
                           self.config.cloud_cover_threshold))
        out = self.outcomes.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def _run(proc, request, **kw):
    return ag.run_agent(proc, request, lambda p, r: p.run(r), **kw)


def _no_llm(*_a, **_k):
    raise AssertionError("LLM planner should not be consulted")


# ── observe ──────────────────────────────────────────────────────────────────

def test_observe_failure_is_structured():
    obs = ag.observe(_req(), None, RuntimeError("No Sentinel-2 images found"))
    assert obs.status == "failed" and obs.quality == "none"
    assert "No Sentinel-2" in obs.error


def test_observe_video_quality():
    frames = [{"had_data": True}, {"had_data": False}, {"had_data": False}]
    r = _res("video_animation", {"total_frames": 3, "frames": frames})
    obs = ag.observe(_req("video_animation"), r, None)
    assert obs.quality == "low"
    assert obs.metrics == {"total_frames": 3, "frames_with_data": 1, "scenes": None}


def test_observe_business_zero():
    r = _res("business_location", {"total_businesses": 0})
    obs = ag.observe(_req("business_location", params={"radius": 2000}), r, None)
    assert obs.quality == "none" and obs.metrics["businesses"] == 0


# ── happy path: unchanged behaviour ──────────────────────────────────────────

def test_first_attempt_ok_runs_tool_once():
    proc = _Proc(_res())
    out = _run(proc, _req(), replan_fn=_no_llm)
    assert out.succeeded
    assert len(proc.calls) == 1
    assert len(out.attempts) == 1
    assert out.attempts[0].decision.action == "accept"
    assert out.result.metadata["agent"]["auto_corrected"] is False


# ── rule fast-paths ──────────────────────────────────────────────────────────

def test_vegetation_no_images_relaxes_cloud_threshold():
    proc = _Proc(
        RuntimeError("No Sentinel-2 images found for 2020-01-01-2020-12-31 with <20% cloud cover"),
        _res(),
    )
    out = _run(proc, _req(), replan_fn=_no_llm)
    assert out.succeeded
    assert len(proc.calls) == 2
    # second call carried the relaxed threshold both in params and in processor.config
    _, params, cfg_cc = proc.calls[1]
    assert params["cloud_cover_threshold"] == 40
    assert cfg_cc == 40
    assert out.request.parameters["cloud_cover_threshold"] == 40
    trace = out.result.metadata["agent"]
    assert trace["auto_corrected"] is True and trace["total_attempts"] == 2
    assert "lenient" in out.attempts[0].decision.user_message


def test_business_zero_results_widens_radius():
    proc = _Proc(
        _res("business_location", {"total_businesses": 0}),
        _res("business_location", {"total_businesses": 7}),
    )
    out = _run(proc, _req("business_location", params={"radius": 2000}), replan_fn=_no_llm)
    assert out.succeeded
    assert proc.calls[1][1]["radius"] == 5000
    assert out.result.metadata["total_businesses"] == 7


def test_video_timeout_shrinks_span():
    proc = _Proc(
        RuntimeError("Computation timed out."),
        _res("video_animation", {"total_frames": 6, "frames": [{"had_data": True}] * 6}),
    )
    out = _run(proc, _req("video_animation", tp={"start_year": 2017, "end_year": 2024}),
               replan_fn=_no_llm)
    assert out.succeeded
    assert proc.calls[1][0] == {"start_year": 2022, "end_year": 2024}


def test_air_quality_before_2019_moves_start():
    proc = _Proc(RuntimeError("No data available in collection"), _res("air_quality", {}))
    out = _run(proc, _req("air_quality", tp={"start_year": 2018, "end_year": 2022}),
               replan_fn=_no_llm)
    assert out.succeeded
    assert proc.calls[1][0]["start_year"] == 2019


# ── guard-rails ──────────────────────────────────────────────────────────────

def test_max_attempts_then_reraises_last_error_with_trace():
    proc = _Proc(RuntimeError("boom 1"), RuntimeError("boom 2"), RuntimeError("boom 3"),
                 RuntimeError("never"))
    planner_calls = []

    def planner(req, obs, history):
        planner_calls.append(len(history))
        return ag.Decision("retry", {"start_year": req.time_period["start_year"] + 1},
                           user_message="nudging")

    with pytest.raises(RuntimeError, match="boom 3") as ei:
        _run(proc, _req(), replan_fn=planner, max_attempts=3)
    assert len(proc.calls) == 3
    outcome = ei.value.agent_outcome
    assert len(outcome.attempts) == 3
    assert outcome.user_message == "nudging"
    # planner consulted for attempts 1 and 2 only (3rd has no retries left)
    assert planner_calls == [0, 1]
    assert outcome.attempts[2].decision.source == "default"


def test_apply_changes_clamps_and_ignores_unknown_knobs():
    now = datetime.now().year
    req = _req("vegetation_index", tp={"start_year": 2020, "end_year": 2024})
    new = ag.apply_changes(req, {
        "start_year": 1990,            # below earliest → 2015, then span cap
        "end_year": now + 5,           # future → now
        "cloud_cover_threshold": 999,  # above max → 80
        "evil_knob": 1,                # ignored
        "radius": 5000,                # not a vegetation knob → ignored
    })
    assert new.time_period["end_year"] == now
    assert new.time_period["start_year"] == now - ag.MAX_SPAN_YEARS
    assert new.parameters["cloud_cover_threshold"] == 80
    assert "evil_knob" not in new.parameters and "radius" not in new.parameters
    # original untouched
    assert req.time_period == {"start_year": 2020, "end_year": 2024}


def test_repeat_proposal_stops_loop():
    proc = _Proc(RuntimeError("boom"), RuntimeError("boom"), _res())

    def planner(req, obs, history):
        # proposes a no-op → identical signature → loop must stop
        return ag.Decision("retry", {"radius": 2000}, user_message="same")

    with pytest.raises(RuntimeError):
        _run(proc, _req("business_location", params={"radius": 2000}), replan_fn=planner)
    assert len(proc.calls) == 1


def test_llm_give_up_stops_and_keeps_message():
    proc = _Proc(RuntimeError("Location is open ocean"), _res())

    def planner(req, obs, history):
        return ag.Decision("give_up", reason="ocean", user_message="That spot is open water.")

    with pytest.raises(RuntimeError) as ei:
        _run(proc, _req("water_quality"), replan_fn=planner)
    assert len(proc.calls) == 1
    assert ei.value.agent_outcome.user_message == "That spot is open water."


def test_best_completed_result_kept_when_retry_fails():
    thin = _res("business_location", {"total_businesses": 1})
    proc = _Proc(thin, RuntimeError("quota"), RuntimeError("quota"))
    out = _run(proc, _req("business_location", params={"radius": 2000}),
               replan_fn=lambda r, o, h: ag.Decision("retry", {"radius": 9000}))
    assert out.succeeded and out.result is thin
    assert out.request.parameters["radius"] == 2000


def test_llm_replan_parses_json_and_falls_back():
    class _Model:
        def __init__(self, text): self.text = text
        def generate_content(self, *_a, **_k):
            class R: pass
            r = R(); r.text = self.text; return r

    d = ag.llm_replan(_req(), ag.Observation("failed", "none", "x"), [],
                      model=_Model('```json\n{"action":"retry","changes":{"start_year":2022},"reason":"r","user_message":"u"}\n```'))
    assert d.action == "retry" and d.changes == {"start_year": 2022} and d.source == "llm"

    d = ag.llm_replan(_req(), ag.Observation("failed", "none", "x"), [], model=_Model("garbage"))
    assert d.action == "give_up" and d.source == "default"


# ── integration with _process_analysis_sync (flag on) ────────────────────────

def test_process_analysis_sync_uses_agent_when_enabled(monkeypatch):
    monkeypatch.setattr(ag, "AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(svc, "_save_json_to_gcs", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_normalise_preview", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_save_to_postgres", lambda *a, **k: None)
    monkeypatch.setattr(svc, "_deduct_analysis_credit", lambda *a, **k: 4)
    monkeypatch.setattr(svc, "_save_completion_message", lambda *a, **k: None)
    saved = []
    monkeypatch.setattr(svc, "_save_agent_message", lambda req, text: saved.append(text))

    class P:
        config = _Cfg()
        n = 0
        def process(self, request):
            P.n += 1
            if P.n == 1:
                raise RuntimeError("No Sentinel-2 images found with <20% cloud cover")
            return _res()

    sessions = {"sid": {"status": "processing", "progress": 0, "message": ""}}
    svc._process_analysis_sync(P(), _req(), sessions, file_manager=None)

    s = sessions["sid"]
    assert s["status"] == "completed" and s["progress"] == 100
    assert s["agent"]["total_attempts"] == 2 and s["agent"]["auto_corrected"] is True
    assert s["credits_remaining"] == 4            # charged once
    assert saved and "adjusted the analysis automatically" in saved[0]


def test_process_analysis_sync_flag_off_is_single_shot(monkeypatch):
    monkeypatch.setattr(ag, "AGENT_LOOP_ENABLED", False)
    calls = []

    class P:
        def process(self, request):
            calls.append(1)
            raise RuntimeError("No Sentinel-2 images found")

    sessions = {"sid": {"status": "processing", "progress": 0, "message": ""}}
    svc._process_analysis_sync(P(), _req(), sessions, file_manager=None)
    assert len(calls) == 1 and sessions["sid"]["status"] == "failed"
    assert "agent" not in sessions["sid"]
