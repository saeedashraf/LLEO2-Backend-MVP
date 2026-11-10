"""
Unit tests for temporal-resolution awareness in lleo.routing.analysis_planner.

No Earth Engine or Gemini: everything here is deterministic date arithmetic.
"""

from __future__ import annotations

from datetime import date

import pytest

from lleo.routing import analysis_planner as ap
from lleo.models.routing import RoutingDecision


TODAY = date(2026, 11, 10)


# ── resolve_window ───────────────────────────────────────────────────────────

def test_window_from_years_and_months():
    s, e = ap.resolve_window({"start_year": 2026, "end_year": 2026, "start_month": 8, "end_month": 11}, TODAY)
    assert s == date(2026, 8, 1)
    assert e == TODAY  # Nov 30 clamped to today


def test_window_exact_dates_win_over_years():
    s, e = ap.resolve_window({"start_year": 2025, "end_year": 2026,
                              "start_date": "2026-03-10", "end_date": "2026-04-02"}, TODAY)
    assert (s, e) == (date(2026, 3, 10), date(2026, 4, 2))


def test_window_event_adds_baseline_before_event():
    s, e = ap.resolve_window({"start_year": 2026, "end_year": 2026, "start_month": 8, "end_month": 11,
                              "event_date": "2026-08-26"}, TODAY)
    assert s <= date(2026, 8, 1)  # already earlier than event - 28 days
    s2, _ = ap.resolve_window({"start_year": 2026, "end_year": 2026, "start_month": 9,
                               "event_date": "2026-08-26"}, TODAY)
    assert s2 == date(2026, 7, 29)


def test_window_clamped_to_sentinel2_archive():
    s, _ = ap.resolve_window({"start_year": 2010, "end_year": 2016}, TODAY)
    assert s == ap.S2_START


# ── generate_periods ─────────────────────────────────────────────────────────

def test_weekly_periods_are_monday_aligned_and_exclusive_end():
    periods = ap.generate_periods(date(2026, 8, 26), date(2026, 9, 15), "weekly")
    assert periods[0]["date_range"]["start"] == "2026-08-26"      # clipped to window start
    assert periods[1]["date_range"]["start"] == "2026-08-31"      # a Monday
    assert periods[-1]["date_range"]["end"] == "2026-09-16"       # exclusive end = last day + 1
    assert len(periods) == 4
    assert all(p["resolution"] == "weekly" for p in periods)


def test_monthly_periods_labels():
    periods = ap.generate_periods(date(2026, 1, 15), date(2026, 3, 3), "monthly")
    assert [p["label"] for p in periods] == ["Jan 2026", "Feb 2026", "Mar 2026"]
    assert periods[1]["date_range"] == {"start": "2026-02-01", "end": "2026-03-01"}


def test_seasonal_periods_cross_year():
    periods = ap.generate_periods(date(2025, 11, 1), date(2026, 4, 1), "seasonal")
    assert [p["label"] for p in periods] == ["Autumn 2025", "Winter 2025/26", "Spring 2026"]


def test_annual_periods():
    periods = ap.generate_periods(date(2022, 6, 1), date(2024, 2, 1), "annual")
    assert [p["label"] for p in periods] == ["2022", "2023", "2024"]


def test_daily_handled_by_processor():
    assert ap.generate_periods(date(2026, 8, 1), date(2026, 8, 20), "daily") == []


# ── sanity caps ──────────────────────────────────────────────────────────────

def test_daily_over_long_span_coarsens_to_weekly():
    res, notes = ap.choose_resolution(date(2026, 1, 1), date(2026, 8, 1), "daily")
    assert res == "weekly"
    assert notes and "weekly" in notes[0]


def test_weekly_over_two_years_coarsens_to_monthly():
    res, notes = ap.choose_resolution(date(2024, 1, 1), date(2026, 1, 1), "weekly")
    assert res == "monthly"
    assert len(ap.generate_periods(date(2024, 1, 1), date(2026, 1, 1), res)) <= ap.MAX_PERIODS


def test_short_daily_window_stays_daily():
    res, notes = ap.choose_resolution(date(2026, 8, 1), date(2026, 9, 30), "daily")
    assert res == "daily" and notes == []


# ── build_resolution_plan ────────────────────────────────────────────────────

def test_weekly_plan_for_event_query():
    tp = {"start_year": 2026, "end_year": 2026, "start_month": 8, "end_month": 11,
          "temporal_resolution": "weekly", "start_date": "2026-08-26", "event_date": "2026-08-26"}
    plan = ap.build_resolution_plan(tp, "weekly", TODAY)
    assert plan["comparison"]["type"] == "time_series"
    assert plan["resolution"] == "weekly"
    assert plan["event_date"] == "2026-08-26"
    assert plan["window"] == {"start": "2026-08-26", "end": "2026-11-10"}
    assert 11 <= len(plan["time_periods"]) <= 12
    assert "weekly" in plan["reasoning"]
    assert plan["planner"] == "deterministic"


def test_daily_plan_is_per_acquisition():
    tp = {"start_year": 2026, "end_year": 2026, "start_date": "2026-09-01", "end_date": "2026-09-30",
          "temporal_resolution": "daily"}
    plan = ap.build_resolution_plan(tp, "daily", TODAY)
    assert plan["per_acquisition"] is True
    assert plan["time_periods"] == []
    assert "acquisition" in plan["reasoning"]
    # normalise keeps a per-acquisition plan even with no explicit periods
    norm = ap.normalise_plan(plan, tp, TODAY)
    assert norm["per_acquisition"] is True
    assert norm["window"] == {"start": "2026-09-01", "end": "2026-09-30"}


# ── normalise_plan (LLM output hygiene) ──────────────────────────────────────

def test_normalise_expands_series_spec_from_llm():
    llm = {
        "strategy_type": "event_impact_series",
        "series": {"resolution": "weekly", "start": "2026-07-29", "end": "2026-11-10"},
        "event_date": "2026-08-26",
        "time_periods": [],
        "comparison": {"type": "time_series"},
        "reasoning": "weekly after the flood",
    }
    plan = ap.normalise_plan(llm, {"start_year": 2026, "end_year": 2026}, TODAY)
    assert plan["resolution"] == "weekly"
    assert 15 <= len(plan["time_periods"]) <= 16
    assert plan["event_date"] == "2026-08-26"
    assert plan["comparison"]["type"] == "time_series"


def test_normalise_clamps_future_and_drops_future_periods():
    llm = {
        "strategy_type": "event_impact_analysis",
        "time_periods": [
            {"label": "Before", "date_range": {"start": "2026-08-01", "end": "2026-08-25"}},
            {"label": "After", "date_range": {"start": "2026-08-26", "end": "2026-11-30"}},
            {"label": "Future", "date_range": {"start": "2026-12-01", "end": "2026-12-31"}},
        ],
        "comparison": {"type": "two_period"},
        "reasoning": "r",
    }
    plan = ap.normalise_plan(llm, {"start_year": 2026, "end_year": 2026}, TODAY)
    assert [p["label"] for p in plan["time_periods"]] == ["Before", "After"]
    assert plan["time_periods"][1]["date_range"]["end"] == "2026-11-11"
    assert "dropped" in plan["reasoning"]


def test_normalise_caps_period_count():
    many = [{"label": str(i), "date_range": {"start": f"2024-01-{(i % 28) + 1:02d}", "end": "2024-02-01"}}
            for i in range(80)]
    plan = ap.normalise_plan({"time_periods": many, "comparison": {"type": "time_series"}},
                             {"start_year": 2024, "end_year": 2024}, TODAY)
    assert len(plan["time_periods"]) == ap.MAX_PERIODS


def test_normalise_falls_back_when_nothing_usable():
    plan = ap.normalise_plan({"time_periods": [{"label": "x", "date_range": {"start": "2027-01-01", "end": "2027-02-01"}}]},
                             {"start_year": 2024, "end_year": 2025}, TODAY)
    assert plan["strategy_type"] == "year_comparison"
    assert plan["planner"] == "fallback"


# ── summarise_plan ───────────────────────────────────────────────────────────

def test_summary_counts_gaps():
    plan = {"resolution": "weekly", "comparison": {"type": "time_series"}, "reasoning": "why"}
    results = [{"no_data": False}, {"no_data": True}, {"no_data": False}]
    text = ap.summarise_plan(plan, results)
    assert "3 periods" in text and "2 with cloud-free" in text and "1 skipped" in text
    assert text.endswith("why")


# ── RoutingDecision validators ───────────────────────────────────────────────

def test_routing_decision_normalises_resolution_and_dates():
    d = RoutingDecision(intent="water_quality", temporal_resolution="Weeks",
                        start_date="2026-08-26T00:00:00", end_date="not a date", event_date="2026-08-26")
    assert d.temporal_resolution == "weekly"
    assert d.start_date == "2026-08-26"
    assert d.end_date is None
    assert d.event_date == "2026-08-26"
    assert RoutingDecision(intent="water_quality", temporal_resolution="hourly").temporal_resolution is None
