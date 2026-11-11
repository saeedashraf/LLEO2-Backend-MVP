"""
Video Animation Processor – Earth Engine monthly-median timelapse.

Builds a satellite timelapse GIF from Sentinel-2 surface reflectance imagery
via Google Earth Engine. For each calendar month in the requested range we
take the per-pixel MEDIAN of all clear scenes (after SCL cloud masking),
producing a smooth jitter-free animation.

Each frame is downloaded as a PNG, annotated locally with PIL (yellow
timestamp top-right, coordinates bottom-left, frame counter bottom-right),
then stitched into a GIF. A per-analysis ``summary.json`` is also written
out with scene counts per month.

Migration history:
    Sentinel Hub (deprecated 2026-03)
    → Planet SDK v3 (paid, ~3 m, replaced 2026-03-26)
    → Earth Engine Sentinel-2 (free, 10 m, current — 2026-04)

Area cap:
    Bbox is hard-capped at MAX_HALF_SIZE_M (50 × 50 km) to prevent
    continental-scale requests that would blow the GEE quota.
"""

from __future__ import annotations

import calendar
import io
import json
import math
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import ee
from PIL import Image, ImageDraw, ImageFont

from lleo.core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from lleo.core.config import LLEOConfig
from lleo.core.exceptions import ProcessorException, ValidationException
from lleo.routing.analysis_planner import (
    RESOLUTIONS, S2_REVISIT_DAYS, auto_resolution, choose_resolution,
    generate_periods, resolve_window,
)


# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_DIMENSION = 768                # per-frame PNG edge in pixels (was 1024:
                                       # ~45 % fewer pixels → faster download,
                                       # quantise, encode and upload)
MIN_DIMENSION = 512
MAX_DIMENSION = 1024

# Frame budget. Above this many months we composite several months per
# frame (e.g. 5 years → 30 bi-monthly frames instead of 60) which keeps
# render time and GIF size bounded.
MAX_FRAMES = 36

DOWNLOAD_RETRIES = 4                   # GEE thumb fetch retries (503/5xx/timeouts)
DOWNLOAD_TIMEOUT_S = 90
UPLOAD_RETRIES = 3                     # GCS upload attempts before failing the job
GIF_COLORS = 256

DEFAULT_HALF_SIZE_M = 5_000            # default 10 × 10 km box
MIN_HALF_SIZE_M = 500                  # 1 × 1 km
MAX_HALF_SIZE_M = 25_000               # 50 × 50 km — covers any city / airport /
                                       # glacier / deforestation patch. Prevents
                                       # "land-use change in a continent" requests.

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
S2_VIS_BANDS = ["B4", "B3", "B2"]      # red, green, blue
S2_VIS_MIN = 0
S2_VIS_MAX = 3000                      # consistent stretch across all frames

PARALLEL_DOWNLOADS = 12                # concurrent frame downloads


# ── Geometry helpers ─────────────────────────────────────────────────────────

def _bbox_around(lat: float, lon: float, half_size_m: int) -> List[float]:
    """Return [west, south, east, north] in degrees around centre point."""
    R = 6_378_137
    d_lat = math.degrees(half_size_m / R)
    d_lon = math.degrees(half_size_m / (R * math.cos(math.radians(lat))))
    return [lon - d_lon, lat - d_lat, lon + d_lon, lat + d_lat]


def _clamp_half_size(requested_m: int) -> int:
    return max(MIN_HALF_SIZE_M, min(MAX_HALF_SIZE_M, int(requested_m)))


def _mask_s2_clouds(img: ee.Image) -> ee.Image:
    """Pixel-level cloud/shadow/cirrus mask via the SCL band."""
    scl = img.select("SCL")
    mask = (
        scl.neq(3)        # cloud shadow
        .And(scl.neq(8))  # cloud medium
        .And(scl.neq(9))  # cloud high
        .And(scl.neq(10)) # thin cirrus
    )
    return img.updateMask(mask)


def _month_label(year: int, month: int) -> str:
    return f"{calendar.month_abbr[month]} {year}"


def _add_months(year: int, month: int, n: int) -> Tuple[int, int]:
    """Return (year, month) that is ``n`` months after (year, month)."""
    idx = (year * 12 + (month - 1)) + n
    return idx // 12, idx % 12 + 1


def _period_label(year: int, month: int, months: int) -> str:
    """Label for a frame spanning ``months`` months starting at year/month."""
    if months <= 1:
        return _month_label(year, month)
    ey, em = _add_months(year, month, months - 1)
    if ey == year:
        return f"{calendar.month_abbr[month]}–{calendar.month_abbr[em]} {year}"
    return f"{_month_label(year, month)} – {_month_label(ey, em)}"


def _plan_periods(
    start_year: int,
    end_year: int,
    start_month: int,
    end_month: int,
    max_frames: int = MAX_FRAMES,
) -> List[Dict[str, int]]:
    """Split the requested range into frame periods.

    Returns a list of ``{year, month, months}`` dicts where ``months`` is the
    number of calendar months composited into that frame.  If the range has
    more than ``max_frames`` months, consecutive months are grouped so the
    total frame count stays ≤ ``max_frames``.
    """
    total = (end_year * 12 + end_month) - (start_year * 12 + start_month) + 1
    if total <= 0:
        return []
    stride = max(1, math.ceil(total / max_frames))
    periods: List[Dict[str, int]] = []
    y, m = start_year, start_month
    remaining = total
    while remaining > 0:
        span = min(stride, remaining)
        periods.append({"year": y, "month": m, "months": span})
        y, m = _add_months(y, m, span)
        remaining -= span
    return periods


# ── Planner-driven frames (temporal resolution awareness) ───────────────────

def _iso(d: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(d)[:10]) if d else None
    except ValueError:
        return None


def _frame_label(period: Dict[str, Any]) -> str:
    """Human label for a planner period (weekly gets explicit dates)."""
    res = period.get("resolution")
    s = _iso((period.get("date_range") or {}).get("start"))
    last = _iso(period.get("last_day")) or s
    if res == "weekly" and s and last:
        if s.year == last.year:
            return f"{s:%d %b} - {last:%d %b %Y}"
        return f"{s:%d %b %Y} - {last:%d %b %Y}"
    return str(period.get("label") or (s.isoformat() if s else "?"))


def plan_frames(time_period: Dict[str, Any], query: str = "",
                today: Optional[date] = None,
                max_frames: int = MAX_FRAMES) -> Optional[Dict[str, Any]]:
    """Decide the frame cadence from the router's time fields.

    Returns ``None`` when the legacy monthly path should be used (year-scale
    requests without exact dates, an event or a requested cadence).  Otherwise
    a plan with ``resolution`` (daily = one frame per Sentinel-2 acquisition,
    resolved later against the archive), ``periods`` (empty for daily),
    ``window``, ``event_date`` and ``reasoning``.
    """
    today = today or date.today()
    tp = dict(time_period or {})
    requested = tp.get("temporal_resolution")
    explicit = requested in RESOLUTIONS
    auto = None if explicit else auto_resolution(query, tp, today)
    has_dates = any(tp.get(k) for k in ("start_date", "end_date", "event_date"))
    if not explicit and auto is None and not has_dates:
        return None

    start, end = resolve_window(tp, today)
    if explicit:
        requested_res, reason = requested, (
            f"The user asked for {requested} frames over {start.isoformat()} to {end.isoformat()}."
        )
    elif auto is not None:
        requested_res, reason = auto
    else:
        requested_res = "monthly"
        reason = (f"Exact dates were given without a cadence; monthly frames cover "
                  f"{start.isoformat()} to {end.isoformat()}.")

    effective, notes = choose_resolution(start, end, requested_res, max_periods=max_frames)
    event = _iso(tp.get("event_date"))
    reasoning = [reason]
    if effective == "daily":
        reasoning.append(
            f"Each frame is one Sentinel-2 acquisition date (revisit about {S2_REVISIT_DAYS} days); "
            f"at most {max_frames} frames are rendered.")
    else:
        reasoning.append(
            f"Each frame is a cloud-masked median of the Sentinel-2 scenes in that "
            f"{effective.rstrip('ly') if effective != 'daily' else 'day'} window; windows without a clear scene are skipped.")
    if event:
        reasoning.append(f"Frames after the {event.isoformat()} event are marked in red.")
    reasoning.extend(notes)

    periods: List[Dict[str, Any]] = []
    if effective != "daily":
        for p in generate_periods(start, end, effective):
            s = _iso(p["date_range"]["start"])
            periods.append({
                "label": _frame_label(p),
                "start": p["date_range"]["start"],
                "end": p["date_range"]["end"],          # exclusive
                "after_event": bool(event and s and s >= event),
            })
    return {
        "resolution": effective,
        "requested_resolution": requested_res,
        "per_acquisition": effective == "daily",
        "planner": "deterministic" if explicit else "auto",
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "event_date": event.isoformat() if event else None,
        "reasoning": " ".join(reasoning),
        "periods": periods,
    }


# ── Font loading (best-effort cross-platform) ────────────────────────────────

_FONT_PATHS = {
    "bold": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
    ],
    "regular": [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
    ],
}
_FONT_CACHE: Dict[Tuple[str, int], Any] = {}


def _font(weight: str, size: int):
    key = (weight, size)
    cached = _FONT_CACHE.get(key)
    if cached is not None:
        return cached
    for path in _FONT_PATHS.get(weight, []):
        try:
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = f
            return f
        except OSError:
            continue
    fallback = ImageFont.load_default()
    _FONT_CACHE[key] = fallback
    return fallback


# ── Frame annotation ─────────────────────────────────────────────────────────

def _fmt_lon(lon: float) -> str:
    return f"{abs(lon):.3f}°{'E' if lon >= 0 else 'W'}"


def _fmt_lat(lat: float) -> str:
    return f"{abs(lat):.3f}°{'N' if lat >= 0 else 'S'}"


def _annotate_frame(
    img: Image.Image,
    timestamp: str,
    bbox: Tuple[float, float, float, float],   # (west, south, east, north)
    frame_idx: int,
    total_frames: int,
    n_ticks: int = 3,
    tag: Optional[str] = None,
) -> Image.Image:
    """
    Add a black border around the frame with lat/lon axis labels on all 4 sides
    (X-axis = longitude on top + bottom, Y-axis = latitude on left + right).
    Yellow timestamp goes inside the image at top-right; frame counter at top-left.
    """
    west, south, east, north = bbox
    img = img.convert("RGB")
    iw, ih = img.size

    # Margin sizes (scale with image, with sane minimums)
    M_LEFT = max(80, ih // 13)
    M_RIGHT = max(80, ih // 13)
    M_TOP = max(40, ih // 24)
    M_BOTTOM = max(50, ih // 18)

    canvas_w = iw + M_LEFT + M_RIGHT
    canvas_h = ih + M_TOP + M_BOTTOM
    canvas = Image.new("RGB", (canvas_w, canvas_h), (12, 12, 12))
    canvas.paste(img, (M_LEFT, M_TOP))

    draw = ImageDraw.Draw(canvas)
    big = _font("bold", max(22, ih // 24))
    small = _font("regular", max(14, ih // 42))
    tick = _font("regular", max(12, ih // 52))

    def shadow_text(xy, text, font, fill="white"):
        draw.text((xy[0] + 1, xy[1] + 1), text, font=font, fill="black")
        draw.text(xy, text, font=font, fill=fill)

    # ── Longitude ticks (top + bottom edges) ────────────────────────────
    for i in range(n_ticks):
        frac = i / (n_ticks - 1)
        lon = west + (east - west) * frac
        x = M_LEFT + int(iw * frac)
        label = _fmt_lon(lon)
        lbox = draw.textbbox((0, 0), label, font=tick)
        lw = lbox[2] - lbox[0]
        lh = lbox[3] - lbox[1]
        # Bottom edge — sits in the bottom margin
        draw.text((x - lw // 2, M_TOP + ih + 8), label, font=tick, fill="white")
        # Top edge — sits in the top margin
        draw.text((x - lw // 2, max(2, M_TOP - lh - 8)), label, font=tick, fill="white")
        # Tick marks across the image edge for clarity
        draw.line(
            [(x, M_TOP + ih), (x, M_TOP + ih + 5)], fill="white", width=1,
        )
        draw.line([(x, M_TOP), (x, M_TOP - 5)], fill="white", width=1)

    # ── Latitude ticks (left + right edges) ─────────────────────────────
    for i in range(n_ticks):
        frac = i / (n_ticks - 1)
        # Image y increases downward (north → south)
        lat = north - (north - south) * frac
        y = M_TOP + int(ih * frac)
        label = _fmt_lat(lat)
        lbox = draw.textbbox((0, 0), label, font=tick)
        lw = lbox[2] - lbox[0]
        lh = lbox[3] - lbox[1]
        # Left edge — right-aligned to image
        draw.text((M_LEFT - lw - 8, y - lh // 2), label, font=tick, fill="white")
        # Right edge — left-aligned just outside image
        draw.text((M_LEFT + iw + 8, y - lh // 2), label, font=tick, fill="white")
        # Tick marks
        draw.line(
            [(M_LEFT, y), (M_LEFT - 5, y)], fill="white", width=1,
        )
        draw.line(
            [(M_LEFT + iw, y), (M_LEFT + iw + 5, y)], fill="white", width=1,
        )

    # ── Yellow timestamp top-right (inside the image) ───────────────────
    bbox_t = draw.textbbox((0, 0), timestamp, font=big)
    tw = bbox_t[2] - bbox_t[0]
    pad = 12
    tx = M_LEFT + iw - tw - pad
    ty = M_TOP + pad
    shadow_text((tx, ty), timestamp, big, fill="#FFD700")
    if tag:
        # Red marker under the timestamp (e.g. "after 26 Aug 2026 event")
        bbox_g = draw.textbbox((0, 0), tag, font=small)
        gw = bbox_g[2] - bbox_g[0]
        th = bbox_t[3] - bbox_t[1]
        shadow_text((M_LEFT + iw - gw - pad, ty + th + 10), tag, small, fill="#FF5A5A")

    # ── Frame counter top-left (inside the image) ───────────────────────
    counter = f"{frame_idx}/{total_frames}"
    shadow_text((M_LEFT + pad, M_TOP + pad), counter, small, fill="white")

    return canvas


# ── Pixel-budget helper (kept for backwards-compat info only) ────────────────

def _pick_dimension(num_frames: int, requested: int = DEFAULT_DIMENSION) -> int:
    """We download per-frame so there is no global pixel budget; this just clamps
    the requested edge to a sane range."""
    return max(MIN_DIMENSION, min(int(requested), MAX_DIMENSION))


def _is_retryable_http(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    # URLError (DNS / connection reset), socket timeouts, incomplete reads
    return isinstance(exc, (urllib.error.URLError, TimeoutError, OSError, EOFError))


def _download_with_retry(make_url, attempts: int = DOWNLOAD_RETRIES) -> bytes:
    """Fetch a GEE thumbnail, regenerating the URL and backing off on 5xx/timeouts."""
    delay = 2.0
    last: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            url = make_url()
            with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_S) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001 — classify below
            last = exc
            if attempt == attempts or not _is_retryable_http(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 20.0)
    raise last  # type: ignore[misc]  # pragma: no cover


# ── Processor ────────────────────────────────────────────────────────────────

class VideoAnimationProcessor(BaseProcessor):
    """
    Sentinel-2 timelapse via Earth Engine monthly-median composites.

    Per-frame download + local PIL annotation + GIF stitching means we are
    NOT bound by Earth Engine's 26 M-pixel video budget. Each frame is up
    to 1024 × 1024 px regardless of how many frames we render.
    """

    def __init__(self, config: LLEOConfig):
        super().__init__(config)
        self.log_info("VideoAnimationProcessor initialised (Earth Engine + Sentinel-2)")

    # ── BaseProcessor interface ──────────────────────────────────────────────

    def get_analysis_type(self) -> str:
        return "video_animation"

    def get_required_parameters(self) -> List[str]:
        return ["location", "time_period"]

    def validate_request(self, request: AnalysisRequest) -> bool:
        self.validate_location(request.location)
        self.validate_time_period(request.time_period)

        tp = request.time_period
        sy = tp.get("start_year")
        ey = tp.get("end_year")
        if sy and ey:
            if sy < 2015:
                raise ValidationException(
                    "Sentinel-2 data is available from 2015 onwards."
                )
            if ey - sy > 5:
                raise ValidationException(
                    "Maximum time range for video animation is 5 years."
                )
        return True

    # ── Main entry point ─────────────────────────────────────────────────────

    async def process(self, request: AnalysisRequest) -> AnalysisResult:
        t0 = time.time()
        sid = request.session_id

        try:
            self.log_info(
                "Starting GEE video animation",
                session_id=sid,
                location=request.location.get("name"),
            )
            self.validate_request(request)

            lat = request.location["latitude"]
            lon = request.location["longitude"]
            location_name = request.location.get("name", "")

            start_year = request.time_period["start_year"]
            end_year = request.time_period["end_year"]
            tp_start_month = request.time_period.get("start_month")
            tp_end_month = request.time_period.get("end_month")
            months_specified = (
                tp_start_month is not None and tp_end_month is not None
            )
            start_month = tp_start_month if tp_start_month is not None else 1
            end_month = tp_end_month if tp_end_month is not None else 12

            # If the user gave a single year with NO month range, expand to
            # year-1 → year so we have a proper 24-month timelapse. If months
            # ARE specified (e.g. "March 2025 to June 2025"), honour them.
            if start_year == end_year and not months_specified:
                start_year -= 1
                start_month = 1
                end_month = 12

            params = request.parameters or {}
            requested_half_m = int(params.get("distance", DEFAULT_HALF_SIZE_M))
            half_size_m = _clamp_half_size(requested_half_m)
            if half_size_m != requested_half_m:
                self.log_warning(
                    f"Bbox half-size clamped: {requested_half_m}m → {half_size_m}m. "
                    f"Animations are limited to ≤{MAX_HALF_SIZE_M*2/1000:.0f}km × "
                    f"{MAX_HALF_SIZE_M*2/1000:.0f}km.",
                    session_id=sid,
                )

            fps = int(params.get("fps", 3))
            dimension = _pick_dimension(int(params.get("dimension", DEFAULT_DIMENSION)))

            west, south, east, north = _bbox_around(lat, lon, half_size_m)
            aoi = ee.Geometry.Rectangle([west, south, east, north])

            self.log_info(
                f"AOI bbox: [{west:.4f}, {south:.4f}, {east:.4f}, {north:.4f}]  "
                f"({half_size_m*2/1000:.1f}km × {half_size_m*2/1000:.1f}km)  "
                f"range: {start_year}-{start_month:02d} → {end_year}-{end_month:02d}",
                session_id=sid,
            )

            # ── Plan the frame cadence (planner) or fall back to months ────
            plan = plan_frames(request.time_period, request.query)
            event_tag = None
            if plan is not None:
                self._progress("Planning the animation frames (window and cadence)…")
                base = self._base_collection(aoi)
                if plan["per_acquisition"]:
                    self._progress("Listing Sentinel-2 acquisition dates for per-image frames…")
                    dates = self._acquisition_dates(base, plan["window"]["start"], plan["window"]["end"])
                    ev = _iso(plan.get("event_date"))
                    plan["periods"] = [{
                        "label": f"{d:%d %b %Y}",
                        "start": d.isoformat(),
                        "end": (d + timedelta(days=1)).isoformat(),
                        "after_event": bool(ev and d >= ev),
                    } for d in dates]
                    if not plan["periods"]:
                        raise ProcessorException(
                            f"No clear scenes found between {plan['window']['start']} "
                            f"and {plan['window']['end']}."
                        )
                frame_specs = self._build_period_specs(base, plan["periods"])
                if plan.get("event_date"):
                    ev = _iso(plan["event_date"])
                    event_tag = f"after {ev:%d %b %Y} event" if ev else None
                self._progress(
                    f"Plan: {len(frame_specs)} {plan['resolution']} frames from "
                    f"{plan['window']['start']} to {plan['window']['end']}. {plan['reasoning']}"
                )
            else:
                frame_specs = self._build_monthly_specs(
                    aoi, start_year, end_year, start_month, end_month,
                )
            num_frames = len(frame_specs)
            if num_frames < 2:
                raise ProcessorException(
                    "Could not build at least 2 frames for the requested range."
                )

            # Fetch all scene counts in one round-trip
            self.log_info(
                f"Counting scenes per month for {num_frames} frames…",
                session_id=sid,
            )
            self._progress(f"Counting clear Sentinel-2 scenes for {num_frames} frames in Earth Engine…")
            scene_counts = ee.List([s["count"] for s in frame_specs]).getInfo()

            # Planner path: frames without a clear scene are skipped, not blacked out
            per_frame_summary: List[Dict[str, Any]] = [None] * num_frames  # type: ignore[list-item]
            if plan is not None:
                render = [i for i, c in enumerate(scene_counts) if int(c) > 0]
                gaps = [frame_specs[i]["label"] for i, c in enumerate(scene_counts) if int(c) == 0]
                self._progress(
                    "Clear scenes per frame: " + ", ".join(
                        f"{s['label']}: {int(c)}" for s, c in zip(frame_specs, scene_counts))
                )
                if gaps:
                    self._progress(f"{len(gaps)} of {num_frames} frames have no clear scene and are skipped")
                for i in range(num_frames):
                    if int(scene_counts[i]) == 0:
                        spec = frame_specs[i]
                        per_frame_summary[i] = {
                            "frame": None, "year": spec["year"], "month": spec["month"],
                            "months_composited": spec["months"], "label": spec["label"],
                            "start": spec.get("start"), "end": spec.get("end"),
                            "scenes_composited": 0, "had_data": False,
                            "after_event": spec.get("after_event", False),
                        }
                if len(render) < 2:
                    raise ProcessorException(
                        f"No clear scenes found for {num_frames - len(render)} of {num_frames} "
                        f"planned {plan['resolution']} frames between {plan['window']['start']} "
                        f"and {plan['window']['end']}."
                    )
            else:
                render = list(range(num_frames))
                gaps = []
            n_render = len(render)

            # ── Download + annotate each frame in parallel ──────────────────
            tmp_dir = os.path.join("/tmp/lleo", sid)
            os.makedirs(tmp_dir, exist_ok=True)
            frame_bbox = (west, south, east, north)

            self.log_info(
                f"Downloading {n_render} frames at {dimension}px (parallel x{PARALLEL_DOWNLOADS})…",
                session_id=sid,
            )
            self._progress(f"Rendering {n_render} frames at {dimension}px ({PARALLEL_DOWNLOADS} in parallel)…")
            # Frames are stored already quantised to a 256-colour palette so
            # that the (slow, single-threaded) palette search happens inside
            # the worker threads instead of serially during GIF encoding.
            pil_frames: List[Image.Image] = [None] * n_render  # type: ignore[list-item]
            rgb_last: List[Optional[Image.Image]] = [None]
            thumb_params = {
                "dimensions": dimension,
                "region": aoi,
                "format": "png",
                "crs": "EPSG:3857",
            }

            def _fetch_one(j: int) -> None:
                idx = render[j]
                spec = frame_specs[idx]
                count = int(scene_counts[idx])
                label = spec["label"]
                if count == 0:
                    # No clear scenes — make a black placeholder rather than failing
                    img = Image.new("RGB", (dimension, dimension), (0, 0, 0))
                else:
                    raw = _download_with_retry(
                        lambda: spec["image"].getThumbURL(thumb_params)
                    )
                    img = Image.open(io.BytesIO(raw)).convert("RGB")

                annotated = _annotate_frame(
                    img, label, frame_bbox, j + 1, n_render,
                    tag=event_tag if spec.get("after_event") else None,
                )
                if j == n_render - 1:
                    rgb_last[0] = annotated
                pil_frames[j] = annotated.quantize(
                    colors=GIF_COLORS, method=Image.Quantize.MEDIANCUT,
                )
                per_frame_summary[idx] = {
                    "frame": j + 1,
                    "year": spec["year"],
                    "month": spec["month"],
                    "months_composited": spec["months"],
                    "label": label,
                    "start": spec.get("start"),
                    "end": spec.get("end"),
                    "scenes_composited": count,
                    "had_data": count > 0,
                    "after_event": spec.get("after_event", False),
                }

            with ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS) as ex:
                futures = [ex.submit(_fetch_one, i) for i in range(n_render)]
                for done, fut in enumerate(as_completed(futures), 1):
                    fut.result()  # raise any error
                    if done % 6 == 0 or done == n_render:
                        self.log_info(
                            f"  …{done}/{n_render} frames ready",
                            session_id=sid,
                        )
                        self._progress(f"{done} of {n_render} frames rendered")

            # ── Stitch GIF locally ──────────────────────────────────────────
            gif_path = os.path.join(tmp_dir, "animation.gif")
            preview_path = os.path.join(tmp_dir, "preview.png")
            self.log_info("Stitching GIF…", session_id=sid)
            self._progress("Encoding the GIF…")
            t_enc = time.time()
            pil_frames[0].save(
                gif_path,
                save_all=True,
                append_images=pil_frames[1:],
                duration=int(1000 / fps),
                loop=0,
                optimize=False,   # frames are pre-quantised; optimize=True only
                                  # re-runs the palette search per frame.
            )
            (rgb_last[0] or pil_frames[-1].convert("RGB")).save(preview_path)
            self.log_info(
                f"GIF: {os.path.getsize(gif_path) / 1024 / 1024:.1f} MB "
                f"(encode {time.time() - t_enc:.1f}s)",
                session_id=sid,
            )

            # ── Build summary.json ──────────────────────────────────────────
            frames_per_year: Dict[str, int] = {}
            scenes_per_year: Dict[str, int] = {}
            for entry in per_frame_summary:
                yk = str(entry["year"])
                frames_per_year[yk] = frames_per_year.get(yk, 0) + 1
                scenes_per_year[yk] = (
                    scenes_per_year.get(yk, 0) + entry["scenes_composited"]
                )

            if plan is not None:
                res_word = plan["resolution"]
                compositing = (
                    "single Sentinel-2 acquisition + SCL cloud/shadow/cirrus mask"
                    if res_word == "daily" else
                    f"{res_word} median + SCL cloud/shadow/cirrus mask"
                )
                date_range_str = f"{plan['window']['start']} to {plan['window']['end']}"
                plan_summary = (
                    f"Planned {num_frames} {res_word} frames from {plan['window']['start']} to "
                    f"{plan['window']['end']}, {n_render} with clear Sentinel-2 scenes"
                    + (f" and {len(gaps)} skipped for clouds or no acquisition" if gaps else "")
                    + f". Planner reasoning: {plan['reasoning']}"
                )
            else:
                compositing = (
                    "monthly median + SCL cloud/shadow/cirrus mask"
                    if frame_specs[0]["months"] == 1 else
                    f"{frame_specs[0]['months']}-month median + SCL cloud/shadow/cirrus mask"
                )
                date_range_str = f"{start_year}-{start_month:02d} to {end_year}-{end_month:02d}"
                plan_summary = None

            summary = {
                "location_name": location_name,
                "coordinates": {"lat": lat, "lon": lon},
                "bbox": [west, south, east, north],
                "area_km2": round((half_size_m * 2 / 1000) ** 2, 1),
                "satellite": "Sentinel-2 SR Harmonized (COPERNICUS/S2_SR_HARMONIZED)",
                "resolution_native_m": 10,
                "rendered_dimension_px": dimension,
                "months_per_frame": frame_specs[0]["months"],
                "compositing_method": compositing,
                "date_range": date_range_str,
                "total_frames": num_frames,
                "rendered_frames": n_render,
                "total_scenes_composited": sum(scene_counts),
                "frames_per_year": frames_per_year,
                "scenes_per_year": scenes_per_year,
                "frames_per_second": fps,
                "frames": per_frame_summary,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            }
            if plan is not None:
                summary["plan"] = {k: v for k, v in plan.items() if k != "periods"}
                summary["plan"]["frames_planned"] = num_frames
                summary["plan"]["frames_with_data"] = n_render
                summary["plan"]["gaps"] = gaps
                summary["plan_summary"] = plan_summary
                summary["data_coverage"] = {"periods": num_frames, "with_data": n_render, "gaps": gaps}
                self._progress(plan_summary)
            summary_path = os.path.join(tmp_dir, "summary.json")
            with open(summary_path, "w") as f:
                json.dump(summary, f, indent=2)

            # ── Upload to GCS ───────────────────────────────────────────────
            # Uploads are verified: `save_file_to_cloud` returns the *local*
            # path when the upload fails, and persisting a `/tmp/...` path
            # (which vanishes with the container) is exactly what produced
            # the "animation not found" errors.  We retry, and if the GIF
            # still cannot be stored we fail the job instead of lying.
            outputs: Dict[str, str] = {}
            fm = getattr(self, "file_manager", None)
            if fm:
                t_up = time.time()
                outputs["animation"] = self._upload_verified(
                    fm, gif_path, f"analysis_results/{sid}/animation.gif", sid,
                )
                outputs["preview"] = self._upload_verified(
                    fm, preview_path, f"analysis_results/{sid}/preview.png", sid,
                )
                outputs["charts"] = self._upload_verified(
                    fm, preview_path, f"analysis_results/{sid}/first_frame.png", sid,
                )
                outputs["summary"] = self._upload_verified(
                    fm, summary_path, f"analysis_results/{sid}/summary.json", sid,
                )
                self.log_info(
                    f"Uploaded animation + preview + summary to GCS "
                    f"({time.time() - t_up:.1f}s)",
                    session_id=sid,
                )
            else:
                outputs["animation"] = gif_path
                outputs["preview"] = preview_path
                outputs["summary"] = summary_path

            # ── Metadata returned to caller / persisted to DB ──────────────
            processing_time = time.time() - t0
            metadata = {
                **{k: v for k, v in summary.items() if k != "frames"},
                "frames": per_frame_summary,
                "analysis_date": time.strftime("%Y-%m-%d %H:%M:%S"),
            }

            self.log_info(
                f"GEE animation done in {processing_time:.1f}s "
                f"({num_frames} frames, {dimension}px, "
                f"{sum(scene_counts)} scenes composited)",
                session_id=sid,
            )

            return self.create_result(
                request=request,
                status="completed",
                outputs=outputs,
                metadata=metadata,
                processing_time=processing_time,
            )

        except ValidationException:
            raise
        except ProcessorException:
            raise
        except Exception as exc:
            self.log_error(f"Unexpected error: {exc}", session_id=sid)
            raise ProcessorException(f"Video animation failed: {exc}")

    # ── Storage helper ───────────────────────────────────────────────────────

    def _upload_verified(self, fm, local_path: str, cloud_path: str, sid: str) -> str:
        """Upload via the file manager and *verify* the result.

        In cloud mode a non-``gs://`` return value means the upload failed
        (CloudFileManager swallows the exception).  Retry with back-off and
        raise ProcessorException if it never succeeds.  In local mode the
        local path is the expected result.
        """
        cloud_mode = bool(getattr(fm, "use_cloud", False))
        delay = 3.0
        for attempt in range(1, UPLOAD_RETRIES + 1):
            result = fm.save_file_to_cloud(local_path, cloud_path)
            if not cloud_mode or (isinstance(result, str) and result.startswith("gs://")):
                return result
            self.log_warning(
                f"Upload of {os.path.basename(local_path)} failed "
                f"(attempt {attempt}/{UPLOAD_RETRIES})",
                session_id=sid,
            )
            if attempt < UPLOAD_RETRIES:
                time.sleep(delay)
                delay *= 2
        raise ProcessorException(
            f"Could not store {os.path.basename(local_path)} in cloud storage "
            f"after {UPLOAD_RETRIES} attempts."
        )

    # ── Progress (shown as reasoning steps) ─────────────────────────────────

    def _progress(self, message: str) -> None:
        cb = getattr(self, "progress_cb", None)
        if cb:
            try:
                cb(message)
            except Exception:
                pass

    # ── GEE pipeline ─────────────────────────────────────────────────────────

    def _base_collection(self, aoi: ee.Geometry) -> ee.ImageCollection:
        return (
            ee.ImageCollection(S2_COLLECTION)
            .filterBounds(aoi)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
            .map(_mask_s2_clouds)
        )

    def _acquisition_dates(self, base: ee.ImageCollection, start: str, end: str,
                           max_frames: int = MAX_FRAMES) -> List[date]:
        """Distinct Sentinel-2 acquisition dates in [start, end] (one round-trip)."""
        end_excl = (_iso(end) + timedelta(days=1)).isoformat()
        stamps = base.filterDate(start, end_excl).aggregate_array("system:time_start").getInfo() or []
        days = sorted({date.fromtimestamp(int(t) / 1000).isoformat() for t in stamps})
        if len(days) > max_frames:
            stride = math.ceil(len(days) / max_frames)
            days = days[::stride]
        return [date.fromisoformat(d) for d in days]

    def _build_period_specs(self, base: ee.ImageCollection,
                            periods: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """One median composite per planner period ({label, start, end exclusive})."""
        specs: List[Dict[str, Any]] = []
        for p in periods:
            s = _iso(p["start"])
            subset = base.filterDate(p["start"], p["end"])
            composite = ee.Image(0).blend(subset.median())
            rgb = composite.visualize(bands=S2_VIS_BANDS, min=S2_VIS_MIN, max=S2_VIS_MAX)
            specs.append({
                "year": s.year if s else None,
                "month": s.month if s else None,
                "months": 0,
                "label": p["label"],
                "start": p["start"],
                "end": p["end"],
                "after_event": bool(p.get("after_event")),
                "image": rgb,
                "count": subset.size(),
            })
        return specs

    def _build_monthly_specs(
        self,
        aoi: ee.Geometry,
        start_year: int,
        end_year: int,
        start_month: int,
        end_month: int,
    ) -> List[Dict[str, Any]]:
        """Build a {year, month, months, label, image, count} spec per frame.

        Each frame is a median composite over ``months`` calendar months
        (1 for ranges ≤ MAX_FRAMES months, more for longer ranges).
        """
        base = (
            ee.ImageCollection(S2_COLLECTION)
            .filterBounds(aoi)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
            .map(_mask_s2_clouds)
        )

        specs: List[Dict[str, Any]] = []
        for period in _plan_periods(start_year, end_year, start_month, end_month):
            year, month, months = period["year"], period["month"], period["months"]
            period_start = ee.Date.fromYMD(year, month, 1)
            period_end = period_start.advance(months, "month")
            subset = base.filterDate(period_start, period_end)
            composite = ee.Image(0).blend(subset.median())
            rgb = composite.visualize(
                bands=S2_VIS_BANDS, min=S2_VIS_MIN, max=S2_VIS_MAX,
            )
            specs.append({
                "year": year,
                "month": month,
                "months": months,
                "label": _period_label(year, month, months),
                "image": rgb,
                "count": subset.size(),  # ee.Number, evaluated later in batch
            })
        return specs

    # ── Capabilities ─────────────────────────────────────────────────────────

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "analysis_type": self.get_analysis_type(),
            "name": "Satellite Video Animation (Earth Engine)",
            "description": (
                "Generate satellite timelapse GIFs from Sentinel-2 imagery "
                "via Google Earth Engine. Each frame is a monthly median "
                "composite with pixel-level cloud masking. Frames are "
                "annotated with a yellow timestamp, coordinates, and a "
                "frame counter, and a JSON summary of composited scenes "
                "is included in the download bundle."
            ),
            "satellite": "Sentinel-2 (COPERNICUS/S2_SR_HARMONIZED)",
            "resolution": f"10 m native, up to {MAX_DIMENSION} px rendered",
            "data_provider": "Copernicus / Google Earth Engine",
            "min_year": 2015,
            "max_time_range_years": 5,
            "max_area_km": MAX_HALF_SIZE_M * 2 / 1000,
            "temporal_resolution": (
                f"Monthly (median composite); ranges longer than {MAX_FRAMES} "
                f"months are grouped so at most {MAX_FRAMES} frames are rendered"
            ),
            "cost": "free",
            "outputs": [
                "Animated GIF timelapse (locally stitched, annotated)",
                "Preview frame (most recent month)",
                "summary.json with scenes composited per month/year",
            ],
        }
