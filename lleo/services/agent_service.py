"""
LLEO v2 — Agent loop:  plan → act → observe → re-plan.

The processors in ``lleo.processors`` are the agent's *tools*.  They stay
untouched: each one takes an ``AnalysisRequest`` and returns an
``AnalysisResult`` or raises.  This module wraps a single tool call in a
bounded loop so a bad first attempt (no cloud-free imagery, empty search
radius, time-out on a 7-year timelapse, …) can be corrected automatically
instead of surfacing as "Analysis failed".

    PLAN      the router's RoutingDecision → AnalysisRequest  (unchanged)
    ACT       processor.process(request)                     (the tool)
    OBSERVE   Observation(status, error, metrics, quality)   (structured)
    RE-PLAN   rules first, Gemini only when rules have no answer
              → accept | retry(changes) | give_up

Guard-rails are deterministic and always win over the model:
  * at most ``max_attempts`` tool calls per analysis (default 3)
  * only whitelisted knobs may change (years, months, cloud threshold,
    radius, animation box/dimension)
  * every retry is clamped to satellite data availability and span limits
  * a retry that would repeat an earlier attempt is rejected
  * the best *completed* result is kept even if a later retry fails

The loop is fully injectable (``run_fn``, ``replan_fn``) so it can be
unit-tested without Earth Engine or Gemini.
"""

from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..core.base_processor import AnalysisRequest, AnalysisResult

# ── Configuration ────────────────────────────────────────────────────────────

AGENT_LOOP_ENABLED = os.getenv("LLEO_AGENT_LOOP", "0") == "1"
MAX_ATTEMPTS = int(os.getenv("LLEO_AGENT_MAX_ATTEMPTS", "3"))

# Earliest year each tool has data for (mirrors the clamp in routers/chat.py).
EARLIEST_YEAR = {
    "vegetation_index": 2015,
    "water_quality": 2015,
    "video_animation": 2015,
    "air_quality": 2018,
}
MAX_SPAN_YEARS = 5

# Knobs the planner may turn, per tool, with hard bounds.
#   time_period keys: start_year, end_year, start_month, end_month
#   parameter keys:   everything else
KNOBS: Dict[str, Dict[str, Dict[str, Any]]] = {
    "vegetation_index": {
        "start_year": {"min": 2015},
        "end_year": {"max": "now"},
        "cloud_cover_threshold": {"min": 10, "max": 80},
    },
    "water_quality": {
        "start_year": {"min": 2015},
        "end_year": {"max": "now"},
    },
    "air_quality": {
        "start_year": {"min": 2018},
        "end_year": {"max": "now"},
    },
    "video_animation": {
        "start_year": {"min": 2015},
        "end_year": {"max": "now"},
        "start_month": {"min": 1, "max": 12},
        "end_month": {"min": 1, "max": 12},
        "distance": {"min": 1000, "max": 25000},
        "dimension": {"min": 512, "max": 1024},
    },
    "business_location": {
        "radius": {"min": 500, "max": 10000},
    },
}
TIME_KEYS = ("start_year", "end_year", "start_month", "end_month")


# ── Data structures ──────────────────────────────────────────────────────────

@dataclass
class Observation:
    """What the agent learned from one tool call."""
    status: str                     # completed | failed
    quality: str                    # ok | low | none
    error: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed" and self.quality == "ok"


@dataclass
class Decision:
    """What to do next."""
    action: str                      # accept | retry | give_up
    changes: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    user_message: str = ""
    source: str = "rule"             # rule | llm | default


@dataclass
class Attempt:
    n: int
    request: Dict[str, Any]
    observation: Observation
    decision: Optional[Decision]
    seconds: float


@dataclass
class AgentOutcome:
    result: Optional[AnalysisResult]
    request: AnalysisRequest         # the request that produced ``result``
    attempts: List[Attempt]
    user_message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.result is not None

    def trace(self) -> Dict[str, Any]:
        """JSON-safe record stored in result.metadata['agent']."""
        return {
            "attempts": [
                {
                    "n": a.n,
                    "time_period": a.request.get("time_period"),
                    "parameters": a.request.get("parameters"),
                    "status": a.observation.status,
                    "quality": a.observation.quality,
                    "error": a.observation.error,
                    "metrics": a.observation.metrics,
                    "decision": asdict(a.decision) if a.decision else None,
                    "seconds": round(a.seconds, 1),
                }
                for a in self.attempts
            ],
            "total_attempts": len(self.attempts),
            "auto_corrected": len(self.attempts) > 1 and self.result is not None,
        }


# ── OBSERVE ──────────────────────────────────────────────────────────────────

def observe(request: AnalysisRequest, result: Optional[AnalysisResult],
            exc: Optional[BaseException]) -> Observation:
    """Normalise a tool outcome into something a planner can reason about."""
    if exc is not None or result is None:
        msg = str(exc) if exc else "processor returned no result"
        return Observation(status="failed", quality="none", error=msg[:500],
                           summary=f"Tool failed: {msg[:200]}")

    meta = result.metadata or {}
    metrics: Dict[str, Any] = {}
    quality = "ok"
    atype = request.analysis_type

    if atype == "video_animation":
        frames = meta.get("frames") or []
        total = int(meta.get("total_frames") or len(frames) or 0)
        with_data = sum(1 for f in frames if f.get("had_data")) if frames else total
        metrics = {
            "total_frames": total,
            "frames_with_data": with_data,
            "scenes": meta.get("total_scenes_composited"),
        }
        if total == 0 or with_data == 0:
            quality = "none"
        elif with_data / max(total, 1) < 0.5:
            quality = "low"

    elif atype == "business_location":
        n = int(meta.get("total_businesses") or 0)
        metrics = {"businesses": n, "radius": (request.parameters or {}).get("radius")}
        if n == 0:
            quality = "none"
        elif n < 3:
            quality = "low"

    elif atype == "air_quality":
        counts: List[int] = []
        nested = meta.get("pollutants") or meta.get("results") or {}
        if isinstance(nested, dict):
            for v in nested.values():
                if isinstance(v, dict) and "image_count" in v:
                    counts.append(int(v["image_count"] or 0))
        metrics = {"image_counts": counts}
        if counts and sum(counts) == 0:
            quality = "none"

    elif atype == "vegetation_index":
        stats = meta.get("statistics") or {}
        metrics = {"has_statistics": bool(stats),
                   "cloud_cover_threshold": meta.get("cloud_cover_threshold")}
        if not stats:
            quality = "low"

    if result.status not in ("completed", "partial"):
        quality = "none"

    return Observation(
        status="completed", quality=quality, metrics=metrics,
        summary=f"Tool completed (quality={quality}) metrics={metrics}",
    )


# ── RE-PLAN: deterministic rules (cheap, predictable) ────────────────────────

_NO_DATA = re.compile(
    r"no .*?(images?|scenes?|data) (found|available)|empty collection|"
    r"at least 2 frames|no (clear|valid) (imagery|scenes)|zero results|0 results",
    re.I,
)
_TIMEOUT = re.compile(
    r"timeout|timed out|deadline|503|502|memory|too large|user memory limit|"
    r"computation timed out|exceeded",
    re.I,
)


def rule_replan(req: AnalysisRequest, obs: Observation, attempt: int) -> Optional[Decision]:
    """Fast path.  Returns None when the rules have no opinion."""
    tp = dict(req.time_period or {})
    pr = dict(req.parameters or {})
    atype = req.analysis_type
    sy, ey = tp.get("start_year"), tp.get("end_year")
    span = (ey - sy) if (sy and ey) else 0
    now = datetime.now().year

    if obs.ok:
        return Decision("accept", reason="first attempt is good", source="rule")

    err = obs.error or ""
    no_data = obs.status == "failed" and _NO_DATA.search(err) is not None
    timeout = obs.status == "failed" and _TIMEOUT.search(err) is not None
    thin = obs.status == "completed" and obs.quality in ("low", "none")

    # ── vegetation: no cloud-free scenes → relax cloud mask, then narrow range
    if atype == "vegetation_index" and (no_data or thin):
        cc = int(pr.get("cloud_cover_threshold", 20))
        if cc < 60:
            return Decision("retry", {"cloud_cover_threshold": min(60, cc + 20)},
                            reason=f"no usable Sentinel-2 scenes at <{cc}% cloud; relaxing mask",
                            user_message=f"No cloud-free imagery at <{cc}% cloud cover — retrying with a more lenient {min(60, cc + 20)}% threshold.")
        if sy and ey and span >= 1 and sy < now - 1:
            return Decision("retry", {"start_year": sy + 1},
                            reason="still no data; moving the start year later",
                            user_message=f"Still no usable imagery for {sy} — comparing {sy + 1} → {ey} instead.")

    # ── timelapse: too long / too heavy → shrink; too few frames → widen months
    if atype == "video_animation":
        if timeout and sy and ey and span > 2:
            return Decision("retry", {"start_year": ey - 2},
                            reason="timelapse too heavy for the request budget; shrinking to 3 years",
                            user_message=f"A {span + 1}-year timelapse timed out — retrying with {ey - 2} → {ey}.")
        if timeout and int(pr.get("dimension", 1024)) > 512:
            return Decision("retry", {"dimension": 512},
                            reason="still timing out; lowering frame resolution",
                            user_message="Retrying at a lower frame resolution to fit the processing budget.")
        if no_data and (tp.get("start_month") or tp.get("end_month")):
            return Decision("retry", {"start_month": 1, "end_month": 12},
                            reason="month window too narrow for 2 frames; using full years",
                            user_message="The month window was too narrow — retrying with full calendar years.")
        if (no_data or thin) and sy and ey and ey < now:
            return Decision("retry", {"end_year": min(now, ey + 1), "start_year": min(sy + 1, min(now, ey + 1) - 1)},
                            reason="few frames had data; shifting the window one year later",
                            user_message="Most frames had no clear scenes — shifting the window one year later.")

    # ── business search: nothing found → widen radius
    if atype == "business_location" and (no_data or thin):
        r = int(pr.get("radius", 2000))
        if r < 10000:
            nr = min(10000, int(r * 2.5))
            return Decision("retry", {"radius": nr},
                            reason=f"{obs.metrics.get('businesses', 0)} results within {r} m; widening",
                            user_message=f"Only {obs.metrics.get('businesses', 0)} places within {r / 1000:g} km — widening the search to {nr / 1000:g} km.")

    # ── air quality: S5P starts mid-2018
    if atype == "air_quality" and (no_data or thin) and sy and sy < 2019:
        return Decision("retry", {"start_year": 2019},
                        reason="Sentinel-5P coverage is sparse before 2019",
                        user_message="Satellite air-quality data is sparse before 2019 — retrying from 2019.")

    # ── generic: a thin-but-completed result is acceptable on the last try
    if thin and attempt >= MAX_ATTEMPTS:
        return Decision("accept", reason="result is thin but no retries left", source="rule")

    return None


# ── RE-PLAN: Gemini (only when rules have no answer) ─────────────────────────

_REPLAN_PROMPT = """\
You are LLEO's analysis supervisor. A geospatial tool was run and you must decide
what to do next. Reply with ONE JSON object and nothing else:

{{"action": "accept" | "retry" | "give_up",
  "changes": {{<knob>: <new value>, ...}},      // only for retry; only knobs listed below
  "reason": "<one sentence, technical>",
  "user_message": "<one friendly sentence for the end user>"}}

Rules:
- "accept" if the result is usable for the user's question.
- "retry" ONLY if a change to the listed knobs is likely to fix the problem.
- "give_up" if no knob can fix it (e.g. the location is ocean for a vegetation query).
- Never widen the year span beyond {max_span} years; never go before the earliest year.
- Do not repeat a previous attempt.

User asked: "{query}"
Tool: {tool}
Earliest year with data: {earliest}
Allowed knobs and bounds: {knobs}

Attempts so far:
{history}

Latest observation:
{observation}
"""


def llm_replan(req: AnalysisRequest, obs: Observation, history: List[Attempt],
               model=None) -> Decision:
    """One small Gemini call → Decision.  Any failure degrades to give_up/accept."""
    if model is None:
        try:
            from ..config import services
            model = services.gemini_model
        except Exception:
            model = None
    fallback = Decision("accept" if obs.status == "completed" else "give_up",
                        reason="planner unavailable", source="default")
    if model is None:
        return fallback

    atype = req.analysis_type
    hist_lines = [
        f"  #{a.n}: time={a.request.get('time_period')} params={a.request.get('parameters')} "
        f"→ {a.observation.status}/{a.observation.quality}"
        + (f" error={a.observation.error[:160]}" if a.observation.error else "")
        for a in history
    ]
    prompt = _REPLAN_PROMPT.format(
        max_span=MAX_SPAN_YEARS,
        query=req.query,
        tool=atype,
        earliest=EARLIEST_YEAR.get(atype, "n/a"),
        knobs=json.dumps(KNOBS.get(atype, {})),
        history="\n".join(hist_lines) or "  (none)",
        observation=json.dumps(asdict(obs), default=str),
    )
    try:
        resp = model.generate_content(
            prompt, generation_config={"max_output_tokens": 512, "temperature": 0.1},
        )
        data = _parse_json(resp.text or "")
        action = str(data.get("action", "")).lower()
        if action not in ("accept", "retry", "give_up"):
            return fallback
        return Decision(
            action=action,
            changes=dict(data.get("changes") or {}),
            reason=str(data.get("reason") or ""),
            user_message=str(data.get("user_message") or ""),
            source="llm",
        )
    except Exception as exc:
        print(f"   ⚠️  agent replan (LLM) failed: {exc}")
        return fallback


def _parse_json(raw: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    text = re.sub(r"\s*```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise


# ── Guard-rails ──────────────────────────────────────────────────────────────

def apply_changes(req: AnalysisRequest, changes: Dict[str, Any]) -> AnalysisRequest:
    """Return a new request with ``changes`` applied *and clamped*.

    Unknown knobs are ignored; values are forced into their bounds; the year
    range is re-validated against data availability and the span cap.
    """
    knobs = KNOBS.get(req.analysis_type, {})
    new = copy.deepcopy(req)
    new.time_period = dict(new.time_period or {})
    new.parameters = dict(new.parameters or {})
    now = datetime.now().year

    for k, v in (changes or {}).items():
        if k not in knobs:
            continue
        try:
            v = int(v)
        except (TypeError, ValueError):
            continue
        b = knobs[k]
        lo = b.get("min")
        hi = now if b.get("max") == "now" else b.get("max")
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        (new.time_period if k in TIME_KEYS else new.parameters)[k] = v

    tp = new.time_period
    earliest = EARLIEST_YEAR.get(req.analysis_type)
    if earliest and tp.get("start_year") and tp["start_year"] < earliest:
        tp["start_year"] = earliest
    if tp.get("end_year") and tp["end_year"] > now:
        tp["end_year"] = now
    if tp.get("start_year") and tp.get("end_year"):
        if tp["start_year"] > tp["end_year"]:
            tp["start_year"] = tp["end_year"]
        if tp["end_year"] - tp["start_year"] > MAX_SPAN_YEARS:
            tp["start_year"] = tp["end_year"] - MAX_SPAN_YEARS
    return new


def _signature(req: AnalysisRequest) -> str:
    return json.dumps({"t": req.time_period or {}, "p": req.parameters or {}},
                      sort_keys=True, default=str)


def _snapshot(req: AnalysisRequest) -> Dict[str, Any]:
    return {"time_period": dict(req.time_period or {}),
            "parameters": dict(req.parameters or {})}


def _configure_processor(processor, req: AnalysisRequest) -> None:
    """Push knobs that processors read from ``self.config`` rather than
    ``request.parameters`` (vegetation cloud threshold).  The config object
    is shared between processors, so copy before mutating."""
    cc = (req.parameters or {}).get("cloud_cover_threshold")
    if cc is not None and hasattr(processor, "config"):
        processor.config = copy.copy(processor.config)
        processor.config.cloud_cover_threshold = int(cc)


# ── The loop ─────────────────────────────────────────────────────────────────

def run_agent(
    processor,
    request: AnalysisRequest,
    run_fn: Callable[[Any, AnalysisRequest], AnalysisResult],
    *,
    replan_fn: Optional[Callable[[AnalysisRequest, Observation, List[Attempt]], Decision]] = None,
    max_attempts: int = MAX_ATTEMPTS,
    on_step: Optional[Callable[[int, str], None]] = None,
) -> AgentOutcome:
    """plan → act → observe → re-plan, at most ``max_attempts`` times.

    ``run_fn(processor, request)`` executes the tool (sync or async-aware).
    ``replan_fn`` is consulted only when the rules have no answer; it
    defaults to the Gemini planner.
    """
    replan_fn = replan_fn or (lambda r, o, h: llm_replan(r, o, h))
    attempts: List[Attempt] = []
    seen = {_signature(request)}
    current = request
    best: Optional[tuple] = None
    best_obs: Optional[Observation] = None
    last_exc: Optional[BaseException] = None
    user_msg = ""

    def step(n: int, msg: str) -> None:
        print(f"🤖 agent[{n}/{max_attempts}] {msg}")
        if on_step:
            try:
                on_step(n, msg)
            except Exception:
                pass

    for n in range(1, max_attempts + 1):
        if n > 1:
            step(n, f"retrying with time={current.time_period} params={current.parameters}")
        _configure_processor(processor, current)
        t0 = time.time()
        result: Optional[AnalysisResult] = None
        exc: Optional[BaseException] = None
        try:
            result = run_fn(processor, current)
        except BaseException as e:          # noqa: BLE001 — tool errors are observations
            exc = e
            last_exc = e
        obs = observe(current, result, exc)
        step(n, obs.summary)

        if result is not None and obs.status == "completed":
            if best_obs is None or _better(obs, best_obs):
                best, best_obs = (result, current), obs

        # ── RE-PLAN ──
        decision = rule_replan(current, obs, n)
        if decision is None:
            if n >= max_attempts:
                decision = Decision("accept" if best else "give_up",
                                    reason="no attempts left", source="default")
            else:
                decision = replan_fn(current, obs, attempts)
        attempts.append(Attempt(n, _snapshot(current), obs, decision, time.time() - t0))

        if decision.action == "accept":
            break
        if decision.action == "give_up":
            user_msg = decision.user_message or user_msg
            break
        # retry
        user_msg = decision.user_message or user_msg
        nxt = apply_changes(current, decision.changes)
        sig = _signature(nxt)
        if sig in seen:
            step(n, "planner proposed a repeat attempt — stopping")
            break
        seen.add(sig)
        current = nxt

    if best is not None:
        res, req = best
        res.metadata = dict(res.metadata or {})
        outcome = AgentOutcome(res, req, attempts, user_msg)
        res.metadata["agent"] = outcome.trace()
        return outcome

    outcome = AgentOutcome(None, current, attempts, user_msg)
    if last_exc is not None:
        # Re-raise so the existing failure path (session status, traceback) works.
        last_exc.agent_outcome = outcome  # type: ignore[attr-defined]
        raise last_exc
    return outcome


_QUALITY_RANK = {"ok": 2, "low": 1, "none": 0}


def _better(a: Observation, b: Observation) -> bool:
    return _QUALITY_RANK.get(a.quality, 0) > _QUALITY_RANK.get(b.quality, 0)
