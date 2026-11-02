"""Unit tests for the pure helpers in the video animation processor."""
import urllib.error

import pytest
from PIL import Image

from lleo.core.exceptions import ProcessorException
from lleo.processors import video_animation_processor as vap


# ── frame planning ───────────────────────────────────────────────────────────

def test_plan_periods_monthly_when_within_budget():
    periods = vap._plan_periods(2023, 2024, 1, 12)
    assert len(periods) == 24
    assert periods[0] == {"year": 2023, "month": 1, "months": 1}
    assert periods[-1] == {"year": 2024, "month": 12, "months": 1}


def test_plan_periods_groups_long_ranges():
    # 5 years = 60 months → stride 2 → 30 frames
    periods = vap._plan_periods(2019, 2023, 1, 12)
    assert len(periods) == 30
    assert all(p["months"] == 2 for p in periods)
    assert periods[0] == {"year": 2019, "month": 1, "months": 2}
    assert periods[1] == {"year": 2019, "month": 3, "months": 2}
    assert periods[-1] == {"year": 2023, "month": 11, "months": 2}


def test_plan_periods_handles_remainder_and_month_bounds():
    # Mar 2020 → Jul 2023 = 41 months, stride 2 → 21 frames, last one single
    periods = vap._plan_periods(2020, 2023, 3, 7)
    assert len(periods) == 21
    assert sum(p["months"] for p in periods) == 41
    assert periods[-1] == {"year": 2023, "month": 7, "months": 1}


def test_plan_periods_empty_for_inverted_range():
    assert vap._plan_periods(2024, 2023, 1, 12) == []


def test_period_labels():
    assert vap._period_label(2024, 3, 1) == "Mar 2024"
    assert vap._period_label(2024, 3, 2) == "Mar–Apr 2024"
    assert vap._period_label(2024, 12, 2) == "Dec 2024 – Jan 2025"


def test_pick_dimension_clamps():
    assert vap._pick_dimension(10, 100) == vap.MIN_DIMENSION
    assert vap._pick_dimension(10, 4096) == vap.MAX_DIMENSION
    assert vap._pick_dimension(10) == vap.DEFAULT_DIMENSION


# ── download retry ───────────────────────────────────────────────────────────

def test_download_retries_on_503(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(vap.time, "sleep", lambda s: None)

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"PNG"

    def fake_urlopen(url, timeout):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None)
        return _Resp()

    monkeypatch.setattr(vap.urllib.request, "urlopen", fake_urlopen)
    out = vap._download_with_retry(lambda: "http://x", attempts=4)
    assert out == b"PNG"
    assert calls["n"] == 3


def test_download_does_not_retry_on_4xx(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(vap.time, "sleep", lambda s: None)

    def fake_urlopen(url, timeout):
        calls["n"] += 1
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, None)

    monkeypatch.setattr(vap.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.HTTPError):
        vap._download_with_retry(lambda: "http://x", attempts=4)
    assert calls["n"] == 1


# ── verified upload ──────────────────────────────────────────────────────────

class _FM:
    def __init__(self, results):
        self.use_cloud = True
        self._results = list(results)
        self.calls = 0

    def save_file_to_cloud(self, local, cloud):
        self.calls += 1
        return self._results.pop(0)


def _proc():
    p = vap.VideoAnimationProcessor.__new__(vap.VideoAnimationProcessor)
    import logging
    p.logger = logging.getLogger("test")
    return p


def test_upload_verified_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(vap.time, "sleep", lambda s: None)
    fm = _FM(["/tmp/x/animation.gif", "gs://b/analysis_results/s/animation.gif"])
    out = _proc()._upload_verified(fm, "/tmp/x/animation.gif", "analysis_results/s/animation.gif", "s")
    assert out.startswith("gs://")
    assert fm.calls == 2


def test_upload_verified_fails_instead_of_storing_tmp_path(monkeypatch):
    monkeypatch.setattr(vap.time, "sleep", lambda s: None)
    fm = _FM(["/tmp/x/animation.gif"] * vap.UPLOAD_RETRIES)
    with pytest.raises(ProcessorException):
        _proc()._upload_verified(fm, "/tmp/x/animation.gif", "analysis_results/s/animation.gif", "s")
    assert fm.calls == vap.UPLOAD_RETRIES


def test_upload_verified_local_mode_returns_local_path():
    fm = _FM(["/tmp/x/animation.gif"])
    fm.use_cloud = False
    out = _proc()._upload_verified(fm, "/tmp/x/animation.gif", "analysis_results/s/animation.gif", "s")
    assert out == "/tmp/x/animation.gif"


# ── annotation / quantisation smoke test ─────────────────────────────────────

def test_annotate_and_quantize_roundtrip(tmp_path):
    img = Image.new("RGB", (64, 64), (30, 120, 60))
    annotated = vap._annotate_frame(img, "Jan 2024", (-1.0, 50.0, 1.0, 51.0), 1, 2)
    assert annotated.mode == "RGB"
    q = annotated.quantize(colors=vap.GIF_COLORS, method=Image.Quantize.MEDIANCUT)
    assert q.mode == "P"
    gif = tmp_path / "a.gif"
    q.save(gif, save_all=True, append_images=[q], duration=300, loop=0, optimize=False)
    assert gif.stat().st_size > 0
