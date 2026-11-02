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
from typing import Any, Dict, List, Optional, Tuple

import ee
from PIL import Image, ImageDraw, ImageFont

from lleo.core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from lleo.core.config import LLEOConfig
from lleo.core.exceptions import ProcessorException, ValidationException


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

            # ── Build monthly composites + scene counts ────────────────────
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
            scene_counts = ee.List([s["count"] for s in frame_specs]).getInfo()

            # ── Download + annotate each frame in parallel ──────────────────
            tmp_dir = os.path.join("/tmp/lleo", sid)
            os.makedirs(tmp_dir, exist_ok=True)
            frame_bbox = (west, south, east, north)

            self.log_info(
                f"Downloading {num_frames} frames at {dimension}px (parallel x{PARALLEL_DOWNLOADS})…",
                session_id=sid,
            )
            # Frames are stored already quantised to a 256-colour palette so
            # that the (slow, single-threaded) palette search happens inside
            # the worker threads instead of serially during GIF encoding.
            pil_frames: List[Image.Image] = [None] * num_frames  # type: ignore[list-item]
            rgb_last: List[Optional[Image.Image]] = [None]
            per_frame_summary: List[Dict[str, Any]] = [None] * num_frames  # type: ignore[list-item]
            thumb_params = {
                "dimensions": dimension,
                "region": aoi,
                "format": "png",
                "crs": "EPSG:3857",
            }

            def _fetch_one(idx: int) -> None:
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
                    img, label, frame_bbox, idx + 1, num_frames,
                )
                if idx == num_frames - 1:
                    rgb_last[0] = annotated
                pil_frames[idx] = annotated.quantize(
                    colors=GIF_COLORS, method=Image.Quantize.MEDIANCUT,
                )
                per_frame_summary[idx] = {
                    "frame": idx + 1,
                    "year": spec["year"],
                    "month": spec["month"],
                    "months_composited": spec["months"],
                    "label": label,
                    "scenes_composited": count,
                    "had_data": count > 0,
                }

            with ThreadPoolExecutor(max_workers=PARALLEL_DOWNLOADS) as ex:
                futures = [ex.submit(_fetch_one, i) for i in range(num_frames)]
                for done, fut in enumerate(as_completed(futures), 1):
                    fut.result()  # raise any error
                    if done % 6 == 0 or done == num_frames:
                        self.log_info(
                            f"  …{done}/{num_frames} frames ready",
                            session_id=sid,
                        )

            # ── Stitch GIF locally ──────────────────────────────────────────
            gif_path = os.path.join(tmp_dir, "animation.gif")
            preview_path = os.path.join(tmp_dir, "preview.png")
            self.log_info("Stitching GIF…", session_id=sid)
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

            summary = {
                "location_name": location_name,
                "coordinates": {"lat": lat, "lon": lon},
                "bbox": [west, south, east, north],
                "area_km2": round((half_size_m * 2 / 1000) ** 2, 1),
                "satellite": "Sentinel-2 SR Harmonized (COPERNICUS/S2_SR_HARMONIZED)",
                "resolution_native_m": 10,
                "rendered_dimension_px": dimension,
                "months_per_frame": frame_specs[0]["months"],
                "compositing_method": (
                    "monthly median + SCL cloud/shadow/cirrus mask"
                    if frame_specs[0]["months"] == 1 else
                    f"{frame_specs[0]['months']}-month median + SCL cloud/shadow/cirrus mask"
                ),
                "date_range": (
                    f"{start_year}-{start_month:02d} to "
                    f"{end_year}-{end_month:02d}"
                ),
                "total_frames": num_frames,
                "total_scenes_composited": sum(scene_counts),
                "frames_per_year": frames_per_year,
                "scenes_per_year": scenes_per_year,
                "frames_per_second": fps,
                "frames": per_frame_summary,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            }
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

    # ── GEE pipeline ─────────────────────────────────────────────────────────

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
