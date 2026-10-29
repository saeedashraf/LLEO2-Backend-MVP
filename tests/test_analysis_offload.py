"""
Unit tests for the analysis off-load + file-URL helpers in
lleo.services.analysis_service.

They avoid every external dependency (Earth Engine, GCS, Supabase, Postgres)
by stubbing the persistence helpers, and prove two things:

1. A blocking processor no longer freezes the event loop — the loop keeps
   ticking (a concurrent coroutine completes) while the processor sleeps on
   a worker thread.
2. build_file_urls only advertises artefacts that exist.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from lleo.core.base_processor import AnalysisRequest, AnalysisResult
from lleo.services import analysis_service as svc


# ── helpers ──────────────────────────────────────────────────────────────────

def _request() -> AnalysisRequest:
    return AnalysisRequest(
        session_id="test_sid",
        user_id="u",
        query="q",
        analysis_type="vegetation_index",
        location={"name": "X", "latitude": 0.0, "longitude": 0.0},
        time_period={"start_year": 2023, "end_year": 2023},
        parameters={},
    )


def _result() -> AnalysisResult:
    return AnalysisResult(
        session_id="test_sid",
        status="completed",
        analysis_type="vegetation_index",
        location={"name": "X", "latitude": 0.0, "longitude": 0.0},
        time_period={"start_year": 2023, "end_year": 2023},
        outputs={"map": "gs://b/map.html", "charts": "gs://b/c.png", "preview": "gs://b/p.png"},
        metadata={},
        processing_time=0.5,
    )


class _SyncBlockingProcessor:
    """Simulates Earth Engine / rasterio: blocks the calling thread."""
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.thread_name = None

    def process(self, request):
        self.thread_name = threading.current_thread().name
        time.sleep(self.seconds)
        return _result()


class _AsyncBlockingProcessor:
    """Async signature, but blocks inside (like water_quality_processor)."""
    def __init__(self, seconds: float):
        self.seconds = seconds
        self.thread_name = None

    async def process(self, request):
        self.thread_name = threading.current_thread().name
        time.sleep(self.seconds)
        return _result()


@pytest.fixture(autouse=True)
def _stub_persistence(monkeypatch):
    """No GCS / Cloud SQL / credits during unit tests."""
    for name in (
        "_save_json_to_gcs",
        "_normalise_preview",
        "_save_to_postgres",
        "_save_completion_message",
        "_deduct_analysis_credit",
    ):
        monkeypatch.setattr(svc, name, lambda *a, **k: None)


# ── tests ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("proc_cls", [_SyncBlockingProcessor, _AsyncBlockingProcessor])
def test_event_loop_stays_responsive_during_blocking_processor(proc_cls):
    block_for = 1.0
    proc = proc_cls(block_for)
    sessions = {"test_sid": {"status": "processing"}}

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            # If the loop were blocked this would not run until the job ends.
            while ticks < 5:
                await asyncio.sleep(0.1)
                ticks += 1

        t0 = time.perf_counter()
        await asyncio.gather(
            svc.process_analysis_background(proc, _request(), sessions, None),
            ticker(),
        )
        return ticks, time.perf_counter() - t0

    ticks, elapsed = asyncio.run(main())

    assert ticks == 5
    # Ticker and job overlapped: total ≈ block_for, not block_for + 0.5
    assert elapsed < block_for + 0.4
    assert proc.thread_name.startswith("lleo-analysis")
    assert sessions["test_sid"]["status"] == "completed"
    assert sessions["test_sid"]["progress"] == 100
    assert sessions["test_sid"]["result"]["outputs"]["map"] == "gs://b/map.html"


def test_failure_is_recorded_in_session():
    class Boom:
        def process(self, request):
            raise RuntimeError("kaput")

    sessions = {"test_sid": {"status": "processing"}}
    asyncio.run(svc.process_analysis_background(Boom(), _request(), sessions, None))

    assert sessions["test_sid"]["status"] == "failed"
    assert "kaput" in sessions["test_sid"]["error"]


def test_completed_only_after_persistence(monkeypatch):
    """/status must not report 'completed' before the Postgres row exists,
    otherwise /results/{sid}/* 404s for clients that poll /status."""
    sessions = {"test_sid": {"status": "processing"}}
    seen = {}

    def fake_pg(*a, **k):
        seen["status_during_save"] = sessions["test_sid"]["status"]

    monkeypatch.setattr(svc, "_save_to_postgres", fake_pg)
    asyncio.run(
        svc.process_analysis_background(_SyncBlockingProcessor(0.01), _request(), sessions, None)
    )

    assert seen["status_during_save"] == "processing"
    assert sessions["test_sid"]["status"] == "completed"
    assert sessions["test_sid"]["progress"] == 100


def test_build_file_urls_running_job():
    urls = svc.build_file_urls("s1")
    assert set(urls) == {"preview_url", "download_url", "metadata_url"}


def test_build_file_urls_vegetation():
    urls = svc.build_file_urls("s1", {"map": "gs://x/map.html", "charts": "gs://x/c.png"})
    assert urls["map_url"] == "/results/s1/map"
    assert urls["chart_url"] == "/results/s1/chart"
    assert "animation_url" not in urls


def test_build_file_urls_video_animation_has_no_map():
    outputs = {
        "animation": "gs://x/animation.gif",
        "preview": "gs://x/preview.png",
        "charts": "gs://x/first_frame.png",
        "summary": "gs://x/summary.json",
    }
    urls = svc.build_file_urls("s1", outputs)
    assert "map_url" not in urls
    assert urls["animation_url"] == "/results/s1/animation"
    assert urls["chart_url"] == "/results/s1/chart"
