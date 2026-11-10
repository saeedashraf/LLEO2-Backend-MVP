"""
Intelligent Analysis Planner

Uses Vertex AI to decide the best analysis strategy based on user query.

Temporal-resolution awareness (2026-11):
  * ``time_period`` may carry ``temporal_resolution`` (daily | weekly | monthly |
    seasonal | annual), exact ``start_date`` / ``end_date`` and an ``event_date``.
  * When the resolution is explicit the periods are generated deterministically
    (no LLM call) by :func:`generate_periods`, with sanity caps that coarsen the
    resolution instead of producing hundreds of Earth Engine windows.
  * When the resolution is not explicit, Gemini may answer with a compact
    ``series`` spec ({resolution, start, end}) which is expanded by the same
    generator, so the model never has to enumerate 40 periods by hand.
  * Every plan is passed through :func:`normalise_plan`: dates are clamped to
    the Sentinel-2 archive and to today, future periods are dropped, the period
    count is capped and ``reasoning`` is extended with what was changed.
  * "daily" means *per acquisition*: Sentinel-2 revisits a site every ~5 days,
    so the processor expands a daily plan into one period per available image
    date (``per_acquisition`` flag) instead of empty calendar days.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

RESOLUTIONS = ("daily", "weekly", "monthly", "seasonal", "annual")
COARSER = {"daily": "weekly", "weekly": "monthly", "monthly": "seasonal",
           "seasonal": "annual", "annual": "annual"}

MAX_PERIODS = 60              # hard cap on Earth Engine windows per analysis
MAX_DAILY_SPAN_DAYS = 92      # "daily" (per acquisition) beyond this becomes weekly
S2_START = date(2015, 6, 23)  # first Sentinel-2A acquisitions
S2_REVISIT_DAYS = 5

_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ────────────────────────────────────────────────────────────────────────────
# Deterministic period generation
# ────────────────────────────────────────────────────────────────────────────

def _parse_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _month_end(d: date) -> date:
    """Last day of the month containing ``d``."""
    nxt = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    return nxt - timedelta(days=1)


def _add_months(d: date, n: int) -> date:
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def resolve_window(time_period: Dict[str, Any], today: Optional[date] = None) -> Tuple[date, date]:
    """Turn the router's time fields into an inclusive [start, end] date window.

    Priority: exact start_date/end_date > years + months > years.
    The window is clamped to the Sentinel-2 archive and to today.
    """
    today = today or date.today()
    sy = time_period.get("start_year")
    ey = time_period.get("end_year") or sy or today.year
    sy = sy or ey
    sm = time_period.get("start_month")
    em = time_period.get("end_month")

    start = _parse_date(time_period.get("start_date"))
    end = _parse_date(time_period.get("end_date"))

    if start is None:
        start = date(int(sy), int(sm) if sm else 1, 1)
    if end is None:
        if em:
            end = _month_end(date(int(ey), int(em), 1))
        else:
            end = date(int(ey), 12, 31)

    # An event date without an explicit start: include a baseline before it.
    event = _parse_date(time_period.get("event_date"))
    if event and time_period.get("start_date") is None:
        start = min(start, event - timedelta(days=28))

    start = max(start, S2_START)
    end = min(end, today)
    if end < start:
        end = start
    return start, end


def _season_of(d: date) -> Tuple[str, date, date]:
    """Meteorological season containing ``d`` → (label, first day, last day)."""
    m = d.month
    if m in (12, 1, 2):
        start_year = d.year if m == 12 else d.year - 1
        s = date(start_year, 12, 1)
        e = _month_end(date(start_year + 1, 2, 1))
        return f"Winter {start_year}/{str(start_year + 1)[2:]}", s, e
    if m in (3, 4, 5):
        return f"Spring {d.year}", date(d.year, 3, 1), date(d.year, 5, 31)
    if m in (6, 7, 8):
        return f"Summer {d.year}", date(d.year, 6, 1), date(d.year, 8, 31)
    return f"Autumn {d.year}", date(d.year, 9, 1), date(d.year, 11, 30)


def generate_periods(start: date, end: date, resolution: str) -> List[Dict[str, Any]]:
    """Expand an inclusive date window into consecutive periods.

    ``date_range.end`` is EXCLUSIVE (what ``ee.ImageCollection.filterDate``
    expects), ``last_day`` is the inclusive end for labels and charts.
    "daily" is handled by the processor (per acquisition) and returns [] here.
    """
    periods: List[Dict[str, Any]] = []
    if end < start or resolution not in RESOLUTIONS or resolution == "daily":
        return periods

    def add(label: str, s: date, e_incl: date) -> None:
        s = max(s, start)
        e_incl = min(e_incl, end)
        if e_incl < s:
            return
        periods.append({
            "label": label,
            "resolution": resolution,
            "date_range": {
                "start": s.isoformat(),
                "end": (e_incl + timedelta(days=1)).isoformat(),
            },
            "last_day": e_incl.isoformat(),
        })

    if resolution == "weekly":
        cur = start - timedelta(days=start.weekday())  # Monday on/before start
        while cur <= end:
            iso = cur.isocalendar()
            add(f"W{iso[1]:02d} {cur.isoformat()}", cur, cur + timedelta(days=6))
            cur += timedelta(days=7)

    elif resolution == "monthly":
        cur = start.replace(day=1)
        while cur <= end:
            add(f"{_MONTH_ABBR[cur.month - 1]} {cur.year}", cur, _month_end(cur))
            cur = _add_months(cur, 1)

    elif resolution == "seasonal":
        cur = start
        while cur <= end:
            label, s, e = _season_of(cur)
            add(label, s, e)
            cur = e + timedelta(days=1)

    elif resolution == "annual":
        for y in range(start.year, end.year + 1):
            add(str(y), date(y, 1, 1), date(y, 12, 31))

    return periods


def choose_resolution(start: date, end: date, requested: str) -> Tuple[str, List[str]]:
    """Apply sanity caps: coarsen until the period count fits MAX_PERIODS.

    Returns (effective_resolution, notes) where notes explain each change.
    """
    notes: List[str] = []
    res = requested if requested in RESOLUTIONS else "monthly"
    span_days = (end - start).days + 1

    if res == "daily" and span_days > MAX_DAILY_SPAN_DAYS:
        notes.append(
            f"A per-acquisition (daily) series over {span_days} days would exceed "
            f"{MAX_DAILY_SPAN_DAYS} days, so the series was coarsened to weekly."
        )
        res = "weekly"

    while res != "daily":
        n = len(generate_periods(start, end, res))
        if n <= MAX_PERIODS or res == "annual":
            break
        nxt = COARSER[res]
        notes.append(
            f"{n} {res} periods would exceed the cap of {MAX_PERIODS}, "
            f"so the series was coarsened to {nxt}."
        )
        res = nxt
    return res, notes


def build_resolution_plan(time_period: Dict[str, Any], resolution: str,
                          today: Optional[date] = None) -> Dict[str, Any]:
    """Deterministic plan for an explicit temporal resolution (no LLM)."""
    today = today or date.today()
    start, end = resolve_window(time_period, today)
    effective, notes = choose_resolution(start, end, resolution)
    periods = generate_periods(start, end, effective)
    event = _parse_date(time_period.get("event_date"))

    reasoning = [
        f"The user asked for a {resolution} series, so the window "
        f"{start.isoformat()} to {end.isoformat()} is split into {effective} steps."
    ]
    if effective == "daily":
        reasoning.append(
            f"Sentinel-2 revisits a site about every {S2_REVISIT_DAYS} days, so 'daily' "
            "means one data point per available acquisition date rather than every "
            "calendar day."
        )
    elif effective == "weekly":
        reasoning.append(
            f"Each week holds at most 1 to 2 Sentinel-2 acquisitions (revisit about "
            f"{S2_REVISIT_DAYS} days); weeks without a cloud-free image are shown as gaps."
        )
    else:
        reasoning.append(
            "Each period is a cloud-filtered composite of the clearest Sentinel-2 "
            "scenes; periods without any clear image are flagged as gaps, not filled in."
        )
    if event:
        reasoning.append(f"The event date {event.isoformat()} is marked on the charts.")
    reasoning.extend(notes)

    plan: Dict[str, Any] = {
        "strategy_type": "per_acquisition_series" if effective == "daily" else f"{effective}_time_series",
        "resolution": effective,
        "requested_resolution": resolution,
        "per_acquisition": effective == "daily",
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "time_periods": periods,
        "comparison": {"type": "time_series", "show_difference": False, "show_trends": True},
        "visualization": {"show_time_series_charts": True, "show_trend_analysis": True},
        "reasoning": " ".join(reasoning),
        "planner": "deterministic",
    }
    if event:
        plan["event_date"] = event.isoformat()
    return plan


# ────────────────────────────────────────────────────────────────────────────
# Plan normalisation (applies to LLM and deterministic plans)
# ────────────────────────────────────────────────────────────────────────────

def normalise_plan(plan: Dict[str, Any], time_period: Dict[str, Any],
                   today: Optional[date] = None) -> Dict[str, Any]:
    """Make any plan safe to execute.

    * expands a compact ``series`` spec into periods
    * clamps every period to [S2_START, today], drops periods that start in the future
    * caps the number of periods (keeps the most recent ones)
    * falls back to a simple plan when nothing usable is left
    """
    today = today or date.today()
    notes: List[str] = []
    plan = dict(plan or {})

    series = plan.get("series")
    if isinstance(series, dict) and not plan.get("time_periods"):
        res = str(series.get("resolution") or "monthly").lower()
        if res not in RESOLUTIONS:
            res = "monthly"
        start = _parse_date(series.get("start"))
        end = _parse_date(series.get("end"))
        w_start, w_end = resolve_window(time_period, today)
        start = start or w_start
        end = end or w_end
        start = max(start, S2_START)
        end = min(end, today)
        if end < start:
            end = start
        effective, c_notes = choose_resolution(start, end, res)
        notes.extend(c_notes)
        plan["resolution"] = effective
        plan["requested_resolution"] = res
        plan["per_acquisition"] = effective == "daily"
        plan["window"] = {"start": start.isoformat(), "end": end.isoformat()}
        plan["time_periods"] = generate_periods(start, end, effective)
        plan.setdefault("comparison", {})["type"] = "time_series"
        plan.setdefault("strategy_type", f"{effective}_time_series")

    cleaned: List[Dict[str, Any]] = []
    dropped_future = 0
    for p in plan.get("time_periods") or []:
        dr = (p or {}).get("date_range") or {}
        s = _parse_date(dr.get("start"))
        e = _parse_date(dr.get("end"))
        if s is None or e is None:
            continue
        if s > today:
            dropped_future += 1
            continue
        s = max(s, S2_START)
        if e > today + timedelta(days=1):
            e = today + timedelta(days=1)
        if e <= s:
            e = s + timedelta(days=1)
        q = dict(p)
        q["date_range"] = {"start": s.isoformat(), "end": e.isoformat()}
        q.setdefault("label", s.isoformat())
        cleaned.append(q)
    if dropped_future:
        notes.append(f"{dropped_future} period(s) starting after today were dropped.")

    if len(cleaned) > MAX_PERIODS:
        notes.append(f"Only the most recent {MAX_PERIODS} of {len(cleaned)} periods are analysed.")
        cleaned = cleaned[-MAX_PERIODS:]

    if not cleaned and not plan.get("per_acquisition"):
        fallback = _create_simple_plan(time_period)
        fallback["reasoning"] = (
            f"{plan.get('reasoning', '')} The planned periods were not usable "
            f"(no dates before today), so a simple plan is used instead."
        ).strip()
        return fallback

    plan["time_periods"] = cleaned
    if plan.get("per_acquisition"):
        plan.setdefault("window", {})
    if notes:
        plan["reasoning"] = f"{plan.get('reasoning', '')} {' '.join(notes)}".strip()
    plan["planner_notes"] = notes

    # Make the event date well-formed or drop it
    ev = _parse_date(plan.get("event_date") or time_period.get("event_date"))
    if ev:
        plan["event_date"] = ev.isoformat()
    else:
        plan.pop("event_date", None)
    return plan


def summarise_plan(plan: Dict[str, Any], period_results: Optional[List[Dict[str, Any]]] = None) -> str:
    """One or two sentences for the chat: what was planned and how much data existed."""
    if not plan:
        return ""
    res = plan.get("resolution")
    ctype = (plan.get("comparison") or {}).get("type", "single_period")
    periods = period_results if period_results is not None else (plan.get("time_periods") or [])
    n = len(periods)
    with_data = sum(1 for p in periods if not p.get("no_data")) if period_results is not None else n
    gaps = n - with_data

    if ctype == "time_series" and plan.get("per_acquisition") and period_results is None:
        head = "Planned a per-acquisition (daily) series; Sentinel-2 acquisition dates are listed from the archive"
    elif ctype == "time_series":
        head = f"Planned a {res or 'time'} series with {n} periods"
    elif ctype == "two_period":
        head = f"Planned a two period comparison"
    else:
        head = "Planned a single period analysis"
    if period_results is not None and n:
        head += f", {with_data} with cloud-free Sentinel-2 data"
        if gaps:
            head += f" and {gaps} skipped for clouds or no acquisition"
    head += "."
    reasoning = (plan.get("reasoning") or "").strip()
    if reasoning:
        return f"{head} Planner reasoning: {reasoning}"
    return head


# ────────────────────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────────────────────

async def plan_analysis_strategy(
    query: str,
    analysis_type: str,
    location: str,
    time_period: Dict[str, Any],
    model
) -> Dict[str, Any]:
    """
    Use Vertex AI to intelligently plan the analysis strategy

    Args:
        query: User's original query
        analysis_type: Type of analysis (water_quality, vegetation_index, etc.)
        location: Location name
        time_period: Basic time period info (years, months, optional
                     temporal_resolution / start_date / end_date / event_date)
        model: Vertex AI Gemini model

    Returns:
        Detailed analysis plan with time periods, comparisons, and visualizations
    """
    resolution = (time_period or {}).get("temporal_resolution")
    if resolution in RESOLUTIONS:
        # Explicit cadence from the router: deterministic, no LLM round-trip.
        plan = build_resolution_plan(time_period, resolution)
        plan = normalise_plan(plan, time_period)
        print(f"📋 ANALYSIS STRATEGY (deterministic): {plan.get('strategy_type')}, "
              f"{len(plan.get('time_periods', []))} periods, resolution={plan.get('resolution')}")
        print(f"   Reasoning: {plan.get('reasoning')}")
        return plan

    if not model:
        # Fallback to simple plan
        return normalise_plan(_create_simple_plan(time_period), time_period)

    today = date.today().isoformat()
    prompt = f"""
    **Role:** You are an intelligent Earth observation analysis planner. Your job is to design the BEST analysis strategy based on what the user actually wants.

    **Today's date:** {today} (never plan periods after today)
    **User Query:** "{query}"
    **Analysis Type:** {analysis_type}
    **Location:** {location}
    **Basic Time Info:** Start Year: {time_period.get('start_year')}, End Year: {time_period.get('end_year')}, Start Month: {time_period.get('start_month', 'not specified')}, End Month: {time_period.get('end_month', 'not specified')}, Start Date: {time_period.get('start_date', 'not specified')}, End Date: {time_period.get('end_date', 'not specified')}, Event Date: {time_period.get('event_date', 'not specified')}

    **CRITICAL — Month constraints:**
    If Start Month and End Month are provided, you MUST limit your time_periods
    to ONLY the months in that range.  For example Start Year 2024, End Year 2025,
    Start Month 11, End Month 3 means Nov 2024, Dec 2024, Jan 2025, Feb 2025, Mar 2025
    — do NOT include any other months.

    **Sensor facts you must reason with:**
    - Data source is Sentinel-2 (10-20 m, revisit about {S2_REVISIT_DAYS} days, archive from {S2_START.isoformat()}).
    - Clouds remove many scenes. A period shorter than ~10 days may have zero clear images;
      such periods are reported as gaps (never invented).
    - "daily" therefore means one point per available acquisition date, not every calendar day.
    - At most {MAX_PERIODS} periods per analysis. Prefer weekly for windows up to ~3 months,
      monthly for up to ~2 years, seasonal or annual beyond that.

    **Your Task:** Design the optimal analysis strategy by deciding:

    1. **Time Periods to Analyze:**
       - Should we analyze specific months? (e.g., "January vs December" → analyze Jan and Dec separately)
       - Should we analyze seasons? (e.g., "winter vs summer" → Dec-Feb vs Jun-Aug)
       - Should we analyze full years? (e.g., "2020 vs 2024" → full year composites)
       - Should we analyze specific date ranges? (e.g., "first half vs second half of 2023")
       - Should we produce a SERIES at a given cadence? Then return a compact
         "series": {{"resolution": "daily|weekly|monthly|seasonal|annual", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}
         instead of listing every period; the system expands it.

    2. **Comparison Strategy:**
       - Should we compare two periods? (e.g., before/after, month1 vs month2)
       - Should we show a time series? (e.g., weekly or monthly progression)
       - Should we show single period analysis? (e.g., just current state)
       - For a dated EVENT (flood, landslide, spill, dam break) prefer a time series
         that starts a few weeks BEFORE the event and runs to today (or the end date),
         at weekly resolution if the window is up to ~3 months, and include "event_date".

    3. **Visualization Strategy:**
       - Should we show difference maps? (e.g., changes between periods)
       - Should we highlight specific indices? (e.g., focus on turbidity)
       - Should we show time series charts?
       - Should we show side-by-side comparisons?

    **Examples:**

    Query: "water quality Lake Zug in 2023 compare January and December"
    Strategy:
    {{
        "strategy_type": "month_comparison",
        "time_periods": [
            {{"year": 2023, "month": 1, "label": "January 2023", "date_range": {{"start": "2023-01-01", "end": "2023-01-31"}}}},
            {{"year": 2023, "month": 12, "label": "December 2023", "date_range": {{"start": "2023-12-01", "end": "2023-12-31"}}}}
        ],
        "comparison": {{
            "type": "two_period",
            "baseline": "January 2023",
            "comparison": "December 2023",
            "show_difference": true
        }},
        "visualization": {{
            "show_difference_maps": true,
            "show_side_by_side": true,
            "show_change_charts": true,
            "highlight_changes": true
        }},
        "reasoning": "User wants to compare two specific months within the same year. We should analyze January and December separately, calculate differences, and visualize changes between these two periods."
    }}

    Query: "deforestation in Amazon from 2020 to 2024"
    Strategy:
    {{
        "strategy_type": "year_comparison",
        "time_periods": [
            {{"year": 2020, "label": "2020", "date_range": {{"start": "2020-01-01", "end": "2020-12-31"}}}},
            {{"year": 2024, "label": "2024", "date_range": {{"start": "2024-01-01", "end": "2024-12-31"}}}}
        ],
        "comparison": {{
            "type": "two_period",
            "baseline": "2020",
            "comparison": "2024",
            "show_difference": true
        }},
        "visualization": {{
            "show_difference_maps": true,
            "show_side_by_side": true,
            "show_change_charts": true,
            "show_vegetation_loss": true
        }},
        "reasoning": "User wants to see deforestation over 4 years. We should compare full year composites from 2020 and 2024 to show vegetation loss."
    }}

    Query: "water quality in Lake Geneva summer 2023"
    Strategy:
    {{
        "strategy_type": "seasonal_analysis",
        "time_periods": [
            {{"year": 2023, "season": "summer", "label": "Summer 2023", "date_range": {{"start": "2023-06-01", "end": "2023-08-31"}}}}
        ],
        "comparison": {{
            "type": "single_period",
            "show_difference": false
        }},
        "visualization": {{
            "show_difference_maps": false,
            "show_side_by_side": false,
            "show_current_state": true,
            "show_distribution_charts": true
        }},
        "reasoning": "User wants current state for a specific season. No comparison needed, just analyze summer months."
    }}

    Query: "vegetation changes every month in 2023"
    Strategy:
    {{
        "strategy_type": "monthly_time_series",
        "series": {{"resolution": "monthly", "start": "2023-01-01", "end": "2023-12-31"}},
        "time_periods": [],
        "comparison": {{
            "type": "time_series",
            "show_difference": false,
            "show_trends": true
        }},
        "visualization": {{
            "show_time_series_charts": true,
            "show_monthly_progression": true,
            "show_trend_analysis": true
        }},
        "reasoning": "User wants to see monthly progression throughout the year. One composite per calendar month, shown as a trend."
    }}

    Query: "turbidity of the Trishuli river after the flood on 26 August 2026" (today {today})
    Strategy:
    {{
        "strategy_type": "event_impact_series",
        "series": {{"resolution": "weekly", "start": "2026-07-29", "end": "{today}"}},
        "event_date": "2026-08-26",
        "time_periods": [],
        "comparison": {{
            "type": "time_series",
            "show_difference": false,
            "show_trends": true
        }},
        "visualization": {{
            "show_time_series_charts": true,
            "highlight_indices": ["turbidity", "tsm"],
            "mark_event": true
        }},
        "reasoning": "The question is about the impact of a dated event. A weekly series from four weeks before the flood to today shows the baseline, the sediment spike and its recovery. Sentinel-2 passes every 5 days, so each week has 1 to 2 scenes; cloudy weeks appear as gaps. Weekly keeps the series under the period cap."
    }}

    **IMPORTANT:**
    - Return ONLY valid JSON
    - Be specific about date ranges (use YYYY-MM-DD format)
    - Always include "reasoning" to explain your strategy, including WHY you chose the temporal resolution
    - For month comparisons, specify exact months (1-12)
    - For seasonal analysis, define the season's date range
    - Never plan periods after today ({today})
    - Consider what the user ACTUALLY wants to see

    **Now plan the strategy for the given query:**
    """

    try:
        response = model.generate_content(prompt, generation_config={
            "max_output_tokens": 8192,
            "temperature": 0.3
        })

        text = response.text.strip()
        print(f"🎯 Analysis Strategy Response:")
        print(text[:500] + "..." if len(text) > 500 else text)

        # Extract JSON
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            json_text = json_match.group(0)
            strategy = json.loads(json_text)
            strategy["planner"] = "gemini"
            strategy = normalise_plan(strategy, time_period)

            print(f"\n📋 ANALYSIS STRATEGY:")
            print(f"   Type: {strategy.get('strategy_type')}")
            print(f"   Resolution: {strategy.get('resolution')}")
            print(f"   Time Periods: {len(strategy.get('time_periods', []))}")
            print(f"   Comparison: {strategy.get('comparison', {}).get('type')}")
            print(f"   Reasoning: {strategy.get('reasoning')}")

            return strategy

    except Exception as e:
        print(f"❌ Analysis planning failed: {e}")
        import traceback
        traceback.print_exc()

    # Fallback
    return normalise_plan(_create_simple_plan(time_period), time_period)


def _create_simple_plan(time_period: Dict[str, Any]) -> Dict[str, Any]:
    """Create a simple fallback plan"""
    start_year = time_period.get('start_year')
    end_year = time_period.get('end_year')

    if start_year == end_year:
        return {
            "strategy_type": "single_year",
            "time_periods": [
                {
                    "year": start_year,
                    "label": str(start_year),
                    "date_range": {
                        "start": f"{start_year}-01-01",
                        "end": f"{start_year}-12-31"
                    }
                }
            ],
            "comparison": {
                "type": "single_period",
                "show_difference": False
            },
            "visualization": {
                "show_current_state": True
            },
            "reasoning": "Simple single year analysis (fallback)",
            "planner": "fallback",
        }
    else:
        return {
            "strategy_type": "year_comparison",
            "time_periods": [
                {
                    "year": start_year,
                    "label": str(start_year),
                    "date_range": {
                        "start": f"{start_year}-01-01",
                        "end": f"{start_year}-12-31"
                    }
                },
                {
                    "year": end_year,
                    "label": str(end_year),
                    "date_range": {
                        "start": f"{end_year}-01-01",
                        "end": f"{end_year}-12-31"
                    }
                }
            ],
            "comparison": {
                "type": "two_period",
                "baseline": str(start_year),
                "comparison": str(end_year),
                "show_difference": True
            },
            "visualization": {
                "show_difference_maps": True,
                "show_side_by_side": True
            },
            "reasoning": "Simple year comparison (fallback)",
            "planner": "fallback",
        }


def extract_month_from_label(label: str) -> Optional[int]:
    """Extract month number from a label like 'January 2023' or 'Jan 2023'"""
    month_map = {
        'january': 1, 'jan': 1,
        'february': 2, 'feb': 2,
        'march': 3, 'mar': 3,
        'april': 4, 'apr': 4,
        'may': 5,
        'june': 6, 'jun': 6,
        'july': 7, 'jul': 7,
        'august': 8, 'aug': 8,
        'september': 9, 'sep': 9, 'sept': 9,
        'october': 10, 'oct': 10,
        'november': 11, 'nov': 11,
        'december': 12, 'dec': 12
    }

    label_lower = label.lower()
    for month_name, month_num in month_map.items():
        if month_name in label_lower:
            return month_num

    return None
