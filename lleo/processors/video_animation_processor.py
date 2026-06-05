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
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple

import ee
from PIL import Image, ImageDraw, ImageFont

from lleo.core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from lleo.core.config import LLEOConfig
from lleo.core.exceptions import ProcessorException, ValidationException


# ── Constants ────────────────────────────────────────────────────────────────

DEFAULT_DIMENSION = 1024               # per-frame PNG edge in pixels
MIN_DIMENSION = 512

DEFAULT_HALF_SIZE_M = 5_000            # default 10 × 10 km box
MIN_HALF_SIZE_M = 500                  # 1 × 1 km
MAX_HALF_SIZE_M = 25_000               # 50 × 50 km — covers any city / airport /
                                       # glacier / deforestation patch. Prevents
                                       # "land-use change in a continent" requests.

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
S2_VIS_BANDS = ["B4", "B3", "B2"]      # red, green, blue
S2_VIS_MIN = 0
S2_VIS_MAX = 3000                      # consistent stretch across all frames

PARALLEL_DOWNLOADS = 8                 # concurrent frame downloads


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
    return max(MIN_DIMENSION, min(int(requested), 2048))


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
            pil_frames: List[Image.Image] = [None] * num_frames  # type: ignore[list-item]
            per_frame_summary: List[Dict[str, Any]] = [None] * num_frames  # type: ignore[list-item]

            def _fetch_one(idx: int) -> None:
                spec = frame_specs[idx]
                count = int(scene_counts[idx])
                label = _month_label(spec["year"], spec["month"])
                if count == 0:
                    # No clear scenes — make a black placeholder rather than failing
                    img = Image.new("RGB", (dimension, dimension), (0, 0, 0))
                else:
                    url = spec["image"].getThumbURL({
                        "dimensions": dimension,
                        "region": aoi,
                        "format": "png",
                        "crs": "EPSG:3857",
                    })
                    with urllib.request.urlopen(url, timeout=120) as r:
                        raw = r.read()
                    img = Image.open(io.BytesIO(raw)).convert("RGB")

                annotated = _annotate_frame(
                    img, label, frame_bbox, idx + 1, num_frames,
                )
                pil_frames[idx] = annotated
                per_frame_summary[idx] = {
                    "frame": idx + 1,
                    "year": spec["year"],
                    "month": spec["month"],
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
            pil_frames[0].save(
                gif_path,
                save_all=True,
                append_images=pil_frames[1:],
                duration=int(1000 / fps),
                loop=0,
                optimize=True,
            )
            pil_frames[-1].save(preview_path)
            self.log_info(
                f"GIF: {os.path.getsize(gif_path) / 1024 / 1024:.1f} MB",
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
                "compositing_method": "monthly median + SCL cloud/shadow/cirrus mask",
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
            outputs: Dict[str, str] = {}
            if hasattr(self, "file_manager") and self.file_manager:
                try:
                    outputs["animation"] = self.file_manager.save_file_to_cloud(
                        gif_path, f"analysis_results/{sid}/animation.gif",
                    )
                    outputs["preview"] = self.file_manager.save_file_to_cloud(
                        preview_path, f"analysis_results/{sid}/preview.png",
                    )
                    outputs["charts"] = self.file_manager.save_file_to_cloud(
                        preview_path, f"analysis_results/{sid}/first_frame.png",
                    )
                    outputs["summary"] = self.file_manager.save_file_to_cloud(
                        summary_path, f"analysis_results/{sid}/summary.json",
                    )
                    self.log_info("Uploaded animation + preview + summary to GCS", session_id=sid)
                except Exception as exc:
                    self.log_error(f"GCS upload failed: {exc}", session_id=sid)
                    outputs["animation"] = gif_path
                    outputs["preview"] = preview_path
                    outputs["summary"] = summary_path
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

    # ── GEE pipeline ─────────────────────────────────────────────────────────

    def _build_monthly_specs(
        self,
        aoi: ee.Geometry,
        start_year: int,
        end_year: int,
        start_month: int,
        end_month: int,
    ) -> List[Dict[str, Any]]:
        """Build a {year, month, image, count} spec per calendar month."""
        base = (
            ee.ImageCollection(S2_COLLECTION)
            .filterBounds(aoi)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 80))
            .map(_mask_s2_clouds)
        )

        specs: List[Dict[str, Any]] = []
        for year in range(start_year, end_year + 1):
            m_start = start_month if year == start_year else 1
            m_end = end_month if year == end_year else 12
            for month in range(m_start, m_end + 1):
                period_start = ee.Date.fromYMD(year, month, 1)
                period_end = period_start.advance(1, "month")
                monthly = base.filterDate(period_start, period_end)
                composite = ee.Image(0).blend(monthly.median())
                rgb = composite.visualize(
                    bands=S2_VIS_BANDS, min=S2_VIS_MIN, max=S2_VIS_MAX,
                )
                specs.append({
                    "year": year,
                    "month": month,
                    "image": rgb,
                    "count": monthly.size(),  # ee.Number, evaluated later in batch
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
            "resolution": "10 m native, up to 1024 px rendered",
            "data_provider": "Copernicus / Google Earth Engine",
            "min_year": 2015,
            "max_time_range_years": 5,
            "max_area_km": MAX_HALF_SIZE_M * 2 / 1000,
            "temporal_resolution": "Monthly (median composite)",
            "cost": "free",
            "outputs": [
                "Animated GIF timelapse (locally stitched, annotated)",
                "Preview frame (most recent month)",
                "summary.json with scenes composited per month/year",
            ],
        }
