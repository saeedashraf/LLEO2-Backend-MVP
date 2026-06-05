"""
Video Animation Processor – Satellite Timelapse Generator

Downloads monthly PlanetScope true-colour images via Planet **Data API**
(activate → download), crops each scene locally to the area of interest,
annotates each frame with a date stamp, and stitches them into an
animated GIF uploaded to GCS.

Migrated from Sentinel Hub (deprecated March 2026) to Planet SDK v3.
Uses PlanetScope PSScene ortho_visual asset (~3 m RGB) for high-res
timelapse generation.  Paid Planet subscription required.

Performance note (2026-03-27):
  Replaced Orders API (slow server-side clipping, 5-20 min) with
  direct Data API asset download + local rasterio crop.  Total time
  for ~11 scenes is now ~1-3 minutes instead of 10-20 minutes.
"""

from __future__ import annotations

import calendar
import math
import os
import pathlib
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image

from lleo.core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from lleo.core.config import LLEOConfig
from lleo.core.exceptions import ProcessorException, ValidationException
from lleo.visualization.video_animation_visualization import (
    annotate_and_save_frame,
    create_animation_gif,
    create_preview_grid,
)

import logging

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_bounding_box(
    latitude: float,
    longitude: float,
    distance_m: int = 5000,
) -> tuple:
    """Return (min_lon, min_lat, max_lon, max_lat) around the centre point."""
    R = 6_378_137  # Earth radius in metres
    d_lat = distance_m / R
    d_lon = distance_m / (R * math.cos(math.radians(latitude)))

    return (
        longitude - math.degrees(d_lon),  # min_lon
        latitude  - math.degrees(d_lat),  # min_lat
        longitude + math.degrees(d_lon),  # max_lon
        latitude  + math.degrees(d_lat),  # max_lat
    )


def _bbox_to_geojson(bbox: tuple) -> dict:
    """Convert (min_lon, min_lat, max_lon, max_lat) to a GeoJSON Polygon."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return {
        "type": "Polygon",
        "coordinates": [[
            [min_lon, min_lat],
            [max_lon, min_lat],
            [max_lon, max_lat],
            [min_lon, max_lat],
            [min_lon, min_lat],
        ]],
    }


def _last_day_of_month(year: int, month: int) -> int:
    """Return the last calendar day for a given year/month."""
    return calendar.monthrange(year, month)[1]


def _geotiff_to_rgb_array(tiff_path: str) -> np.ndarray:
    """
    Read a GeoTIFF (or image) and return an RGB uint8 numpy array.
    Tries rasterio first (proper GeoTIFF), falls back to PIL.

    Returns:
        np.ndarray of shape (H, W, 3) dtype uint8
    """
    try:
        import rasterio
        with rasterio.open(tiff_path) as src:
            n_bands = src.count
            if n_bands >= 3:
                r, g, b = src.read(1), src.read(2), src.read(3)
                img_array = np.stack([r, g, b], axis=-1)
            else:
                band = src.read(1)
                img_array = np.stack([band, band, band], axis=-1)

            if img_array.dtype != np.uint8:
                if img_array.max() > 255:
                    img_array = (img_array / img_array.max() * 255).astype(np.uint8)
                else:
                    img_array = img_array.astype(np.uint8)
            return img_array

    except ImportError:
        # PIL fallback — works for most visual GeoTIFFs
        img = Image.open(tiff_path).convert("RGB")
        return np.array(img)


def _crop_geotiff_to_bbox(tiff_path: str, bbox: tuple, out_path: str) -> str:
    """
    Crop a GeoTIFF to the given (min_lon, min_lat, max_lon, max_lat) bbox.

    Planet GeoTIFFs are typically in **UTM** (EPSG:326xx), so we must
    reproject the lon/lat bbox into the scene's native CRS before doing
    a windowed read.  Uses rasterio.warp.transform_bounds (no extra deps).

    Saves the cropped RGB image as PNG at *out_path*.
    Falls back to a full-image resize if anything goes wrong.
    """
    try:
        import rasterio
        from rasterio.windows import from_bounds, Window
        from rasterio.warp import transform_bounds

        min_lon, min_lat, max_lon, max_lat = bbox

        with rasterio.open(tiff_path) as src:
            try:
                # ── Reproject bbox from EPSG:4326 → scene CRS ────────
                scene_crs = src.crs
                if scene_crs and not scene_crs.is_geographic:
                    # Scene is in a projected CRS (e.g. UTM) – transform
                    x_min, y_min, x_max, y_max = transform_bounds(
                        "EPSG:4326", scene_crs,
                        min_lon, min_lat, max_lon, max_lat,
                    )
                    logger.info(
                        f"Reprojected bbox: lon/lat ({min_lon:.4f},{min_lat:.4f})"
                        f"→({max_lon:.4f},{max_lat:.4f}) "
                        f"→ native CRS ({x_min:.1f},{y_min:.1f})"
                        f"→({x_max:.1f},{y_max:.1f})"
                    )
                else:
                    # Already geographic
                    x_min, y_min = min_lon, min_lat
                    x_max, y_max = max_lon, max_lat

                # ── Compute pixel window ─────────────────────────────
                window = from_bounds(
                    x_min, y_min, x_max, y_max,
                    transform=src.transform,
                )

                # Clamp to raster extent
                row_off = max(int(window.row_off), 0)
                col_off = max(int(window.col_off), 0)
                height  = min(int(window.height), src.height - row_off)
                width   = min(int(window.width),  src.width  - col_off)

                if height < 10 or width < 10:
                    raise ValueError(
                        f"Crop window too small: {width}×{height}"
                    )

                crop_window = Window(col_off, row_off, width, height)
                logger.info(
                    f"Crop window: col={col_off} row={row_off} "
                    f"{width}×{height} (scene {src.width}×{src.height})"
                )

                n_bands = src.count
                if n_bands >= 3:
                    r = src.read(1, window=crop_window)
                    g = src.read(2, window=crop_window)
                    b = src.read(3, window=crop_window)
                    img_array = np.stack([r, g, b], axis=-1)
                else:
                    band = src.read(1, window=crop_window)
                    img_array = np.stack([band, band, band], axis=-1)

                if img_array.dtype != np.uint8:
                    mx = img_array.max()
                    if mx > 255 and mx > 0:
                        img_array = (img_array / mx * 255).astype(np.uint8)
                    else:
                        img_array = img_array.astype(np.uint8)

                Image.fromarray(img_array).save(out_path)
                return out_path

            except Exception as e:
                logger.warning(f"Windowed crop failed ({e}), resizing full image")

            # ── Fallback: read full image, resize to ~1024px ─────────
            # This avoids sending 40 MB frames through the GIF pipeline
            n_bands = src.count
            if n_bands >= 3:
                r, g, b = src.read(1), src.read(2), src.read(3)
                img_array = np.stack([r, g, b], axis=-1)
            else:
                band = src.read(1)
                img_array = np.stack([band, band, band], axis=-1)

            if img_array.dtype != np.uint8:
                mx = img_array.max()
                if mx > 255 and mx > 0:
                    img_array = (img_array / mx * 255).astype(np.uint8)
                else:
                    img_array = img_array.astype(np.uint8)

            # Resize to manageable size
            pil_img = Image.fromarray(img_array)
            max_dim = max(pil_img.size)
            if max_dim > 1024:
                scale = 1024 / max_dim
                new_w = int(pil_img.width * scale)
                new_h = int(pil_img.height * scale)
                pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)

            pil_img.save(out_path)
            return out_path

    except ImportError:
        # No rasterio → PIL only, cannot crop by geo-coords
        img = Image.open(tiff_path).convert("RGB")
        img.save(out_path)
        return out_path


# ── Processor ────────────────────────────────────────────────────────────────

class VideoAnimationProcessor(BaseProcessor):
    """
    PlanetScope satellite timelapse processor (Planet SDK v3).

    Uses the Planet **Data API** to directly activate + download individual
    scene assets, then crops locally with rasterio.  This is *much* faster
    than the Orders API (seconds per scene vs 5-20 min batch processing).

    Data source : PlanetScope PSScene (~3 m resolution)
    Asset type  : ortho_visual (RGB, display-ready)
    Auth        : PLANET_API_KEY or PL_API_KEY env var
    """

    def __init__(self, config: LLEOConfig):
        super().__init__(config)
        self._planet_client = None
        self.log_info("VideoAnimationProcessor initialised (Planet SDK)")

    # ── BaseProcessor interface ──────────────────────────────────────────────

    def get_analysis_type(self) -> str:
        return "video_animation"

    def get_required_parameters(self) -> List[str]:
        return ["location", "time_period"]

    def validate_request(self, request: AnalysisRequest) -> bool:
        self.validate_location(request.location)
        self.validate_time_period(request.time_period)

        tp = request.time_period
        start_year = tp.get("start_year")
        end_year = tp.get("end_year")

        if start_year and end_year:
            if start_year < 2016:
                raise ValidationException(
                    "PlanetScope data is available from 2016 onwards. "
                    "Please use a start year of 2016 or later."
                )
            if end_year - start_year > 5:
                raise ValidationException(
                    "Maximum time range for video animation is 5 years "
                    "(to keep GIF size manageable)"
                )
        return True

    # ── Main processing entry point ──────────────────────────────────────────

    async def process(self, request: AnalysisRequest) -> AnalysisResult:
        start_time = time.time()

        try:
            self.log_info(
                "Starting video animation (Planet SDK)",
                session_id=request.session_id,
                location=request.location.get("name"),
            )
            self.validate_request(request)

            # ── Extract parameters ───────────────────────────────────────
            lat = request.location["latitude"]
            lon = request.location["longitude"]
            location_name = request.location.get("name", "")
            start_year = request.time_period["start_year"]
            end_year = request.time_period["end_year"]
            start_month = request.time_period.get("start_month", 1)
            end_month = request.time_period.get("end_month", 12)

            # Single-year → compare with previous year
            if start_year == end_year:
                start_year -= 1
                start_month = 1
                end_month = 12

            params = request.parameters or {}
            distance_m = params.get("distance", 5000)           # bbox half-side
            frame_duration = params.get("frame_duration", 600)  # ms per GIF frame
            max_cloud = params.get("max_cloud_cover", 0.15)     # 15 %

            # ── Bounding box ─────────────────────────────────────────────
            bbox_coords = _get_bounding_box(lat, lon, distance_m)
            aoi_geojson = _bbox_to_geojson(bbox_coords)

            self.log_info(
                f"BBox: {bbox_coords}, "
                f"range: {start_year}-{start_month:02d} → "
                f"{end_year}-{end_month:02d}",
                session_id=request.session_id,
            )

            # ── Planet client ────────────────────────────────────────────
            pl = self._get_planet_client()

            temp_dir = os.path.join(self.config.temp_dir, request.session_id)
            os.makedirs(temp_dir, exist_ok=True)

            # ── 1) Search: best scene per month ──────────────────────────
            monthly_scenes: List[Dict] = []

            # For ranges > 2 years, sample quarterly to keep order small
            total_months = (end_year - start_year) * 12 + (end_month - start_month + 1)
            sample_quarterly = total_months > 24  # > 2 years → quarterly
            quarter_months = {1, 4, 7, 10}

            if sample_quarterly:
                self.log_info(
                    f"Range is {total_months} months → sampling quarterly for faster processing",
                    session_id=request.session_id,
                )

            for year in range(start_year, end_year + 1):
                m_start = start_month if year == start_year else 1
                m_end   = end_month   if year == end_year   else 12

                for month in range(m_start, m_end + 1):
                    if sample_quarterly and month not in quarter_months:
                        continue
                    try:
                        item_id = self._search_best_scene(
                            pl, aoi_geojson, year, month, max_cloud,
                        )
                        if item_id:
                            monthly_scenes.append({
                                "year": year,
                                "month": month,
                                "item_id": item_id,
                            })
                            self.log_info(
                                f"  🔍 {year}-{month:02d} → {item_id}",
                                session_id=request.session_id,
                            )
                        else:
                            self.log_warning(
                                f"  ⚠️ No clear scene {year}-{month:02d}",
                                session_id=request.session_id,
                            )
                    except Exception as exc:
                        self.log_warning(
                            f"  ⚠️ Search err {year}-{month:02d}: {exc}",
                            session_id=request.session_id,
                        )

            if not monthly_scenes:
                raise ProcessorException(
                    "Could not find any cloud-free PlanetScope scenes "
                    "for the requested area and time range."
                )

            self.log_info(
                f"Found {len(monthly_scenes)} scenes → downloading via Data API",
                session_id=request.session_id,
            )

            # ── 2) Order + clip + download ───────────────────────────────
            frame_paths = self._order_and_download_clipped(
                pl, monthly_scenes, aoi_geojson, temp_dir,
                location_name, request.session_id,
            )

            if not frame_paths:
                raise ProcessorException(
                    "Could not download any satellite frames. "
                    "The order may have failed or scenes were unusable."
                )

            self.log_info(
                f"Prepared {len(frame_paths)} annotated frames",
                session_id=request.session_id,
            )

            # ── 3) Create GIF ────────────────────────────────────────────
            gif_path = os.path.join(temp_dir, "animation.gif")
            create_animation_gif(
                frame_paths, gif_path, duration_ms=frame_duration,
            )

            # ── 4) Preview grid ──────────────────────────────────────────
            preview_path = os.path.join(temp_dir, "preview.png")
            create_preview_grid(
                frame_paths, preview_path, location_name=location_name,
            )

            # ── 5) Upload to GCS ─────────────────────────────────────────
            outputs: Dict[str, str] = {}
            sid = request.session_id

            if hasattr(self, "file_manager") and self.file_manager:
                try:
                    outputs["animation"] = self.file_manager.save_file_to_cloud(
                        gif_path,
                        f"analysis_results/{sid}/animation.gif",
                    )
                    outputs["preview"] = self.file_manager.save_file_to_cloud(
                        preview_path,
                        f"analysis_results/{sid}/preview.png",
                    )
                    outputs["charts"] = self.file_manager.save_file_to_cloud(
                        frame_paths[0],
                        f"analysis_results/{sid}/first_frame.png",
                    )
                    self.log_info("Files uploaded to GCS", session_id=sid)
                except Exception as exc:
                    self.log_error(f"GCS upload failed: {exc}", session_id=sid)
                    outputs["animation"] = gif_path
                    outputs["preview"]   = preview_path
            else:
                outputs["animation"] = gif_path
                outputs["preview"]   = preview_path

            # ── Metadata ─────────────────────────────────────────────────
            processing_time = time.time() - start_time

            metadata = {
                "start_year": start_year,
                "end_year": end_year,
                "start_month": start_month,
                "end_month": end_month,
                "location_name": location_name,
                "latitude": lat,
                "longitude": lon,
                "bbox": list(bbox_coords),
                "distance_m": distance_m,
                "total_frames": len(frame_paths),
                "scenes_found": len(monthly_scenes),
                "frame_duration_ms": frame_duration,
                "satellite": "PlanetScope (PSScene)",
                "resolution": "~3 m",
                "data_provider": "Planet Labs",
                "analysis_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "coordinates": {"lat": lat, "lon": lon},
            }

            self.log_info(
                f"Video animation done in {processing_time:.1f}s "
                f"({len(frame_paths)} frames)",
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
            self.log_error(
                f"Unexpected error: {exc}", session_id=request.session_id,
            )
            raise ProcessorException(f"Video animation failed: {exc}")

    # ── Planet SDK helpers ───────────────────────────────────────────────────

    def _get_planet_client(self):
        """
        Create and cache a Planet SDK client.

        Auth priority:
          1. PLANET_API_KEY env var
          2. PL_API_KEY env var (Planet convention)
          3. GCP Secret Manager  → planet-api-key
          4. LLEOConfig.planet_api_key
        """
        if self._planet_client is not None:
            return self._planet_client

        import planet

        api_key = os.getenv("PLANET_API_KEY") or os.getenv("PL_API_KEY")

        if not api_key and hasattr(self, "get_secret") and self.get_secret:
            try:
                api_key = self.get_secret("planet-api-key")
            except Exception:
                pass

        if not api_key:
            api_key = getattr(self.config, "planet_api_key", None)

        if not api_key:
            raise ProcessorException(
                "Planet API key not configured. "
                "Set PLANET_API_KEY or PL_API_KEY env var, "
                "or store 'planet-api-key' in GCP Secret Manager."
            )

        auth = planet.Auth.from_key(api_key.strip())
        sess = planet.Session(auth)
        self._planet_client = planet.Planet(sess)

        self.log_info(f"Planet SDK ready (key: {api_key.strip()[:8]}…)")
        return self._planet_client

    # ── Search ───────────────────────────────────────────────────────────────

    def _search_best_scene(
        self,
        pl,
        aoi_geojson: dict,
        year: int,
        month: int,
        max_cloud: float = 0.15,
    ) -> Optional[str]:
        """
        Find the least-cloudy PlanetScope scene that covers the AOI in
        the given month.  Returns item_id or None.
        """
        from planet import data_filter

        last_day = _last_day_of_month(year, month)

        sfilter = data_filter.and_filter([
            data_filter.permission_filter(),
            data_filter.date_range_filter(
                "acquired",
                gt=datetime(year, month, 1, 0, 0, 0),
                lt=datetime(year, month, last_day, 23, 59, 59),
            ),
            data_filter.range_filter("cloud_cover", lte=max_cloud),
        ])

        best_item = None
        best_cloud = 1.0

        for item in pl.data.search(
            ["PSScene"],
            search_filter=sfilter,
            geometry=aoi_geojson,
            limit=10,
        ):
            cc = item.get("properties", {}).get("cloud_cover", 1.0)
            if cc < best_cloud:
                best_cloud = cc
                best_item = item

        return best_item["id"] if best_item else None

    # ── Direct Data API download + local crop ─────────────────────────────────

    def _order_and_download_clipped(
        self,
        pl,
        monthly_scenes: List[Dict],
        aoi_geojson: dict,
        temp_dir: str,
        location_name: str,
        session_id: str,
    ) -> List[str]:
        """
        Download each scene via Planet **Data API** (activate → wait →
        download) and crop locally to the AOI bbox.

        This replaces the previous Orders API approach which took 5-20 min
        for server-side batch clipping.  Data API is MUCH faster: typically
        a few seconds per scene if the asset is already cached, or 30-60 s
        if activation is needed.

        Returns a chronologically-sorted list of annotated PNG frame paths.
        """
        # Extract bbox from aoi_geojson for local cropping
        coords = aoi_geojson["coordinates"][0]
        lons = [c[0] for c in coords]
        lats = [c[1] for c in coords]
        bbox = (min(lons), min(lats), max(lons), max(lats))

        frame_entries: List[Dict] = []
        dl_dir = os.path.join(temp_dir, "planet_dl")
        os.makedirs(dl_dir, exist_ok=True)

        total = len(monthly_scenes)

        for idx, scene in enumerate(monthly_scenes, 1):
            item_id = scene["item_id"]
            year    = scene["year"]
            month   = scene["month"]

            self.log_info(
                f"  📡 [{idx}/{total}] Downloading {item_id} "
                f"({year}-{month:02d})…",
                session_id=session_id,
            )

            try:
                # ── 1) Get asset description ─────────────────────────
                asset = pl.data.get_asset(
                    item_type_id="PSScene",
                    item_id=item_id,
                    asset_type_id="ortho_visual",
                )

                # ── 2) Activate (no-op if already active) ───────────
                pl.data.activate_asset(asset)

                # ── 3) Wait for activation (usually seconds) ────────
                asset = pl.data.wait_asset(
                    asset,
                    callback=lambda st: self.log_info(
                        f"    Asset status: {st}", session_id=session_id,
                    ),
                )

                # ── 4) Download the asset ────────────────────────────
                dl_path = pl.data.download_asset(
                    asset,
                    directory=pathlib.Path(dl_dir),
                    overwrite=True,
                )
                dl_path = str(dl_path)
                self.log_info(
                    f"    ✅ Downloaded → {os.path.basename(dl_path)}",
                    session_id=session_id,
                )

                # ── 5) Crop to AOI bbox locally ─────────────────────
                cropped_path = os.path.join(
                    temp_dir,
                    f"cropped_{year}_{month:02d}.png",
                )
                _crop_geotiff_to_bbox(dl_path, bbox, cropped_path)

                # ── 6) Read cropped image as RGB array ───────────────
                img_array = np.array(Image.open(cropped_path).convert("RGB"))

                # ── 7) Annotate frame with date label ────────────────
                frame_path = os.path.join(
                    temp_dir,
                    f"{year}_{month:02d}_"
                    f"{calendar.month_name[month]}.png",
                )

                annotate_and_save_frame(
                    img_array, year, month,
                    frame_path, location_name=location_name,
                )

                frame_entries.append({
                    "year": year,
                    "month": month,
                    "path": frame_path,
                })
                self.log_info(
                    f"    ✅ Frame ready: {year}-{month:02d}",
                    session_id=session_id,
                )

                # Clean up the large TIFF to save disk space
                try:
                    os.remove(dl_path)
                    if os.path.exists(cropped_path) and cropped_path != frame_path:
                        os.remove(cropped_path)
                except OSError:
                    pass

            except Exception as exc:
                self.log_warning(
                    f"    ⚠️ Failed {item_id} ({year}-{month:02d}): {exc}",
                    session_id=session_id,
                )
                # Try fallback asset type: "ortho_analytic_3b_sr"
                try:
                    self.log_info(
                        f"    🔄 Trying fallback asset ortho_analytic_3b…",
                        session_id=session_id,
                    )
                    asset = pl.data.get_asset(
                        item_type_id="PSScene",
                        item_id=item_id,
                        asset_type_id="ortho_analytic_3b",
                    )
                    pl.data.activate_asset(asset)
                    asset = pl.data.wait_asset(asset)

                    dl_path = pl.data.download_asset(
                        asset,
                        directory=pathlib.Path(dl_dir),
                        overwrite=True,
                    )
                    dl_path = str(dl_path)

                    cropped_path = os.path.join(
                        temp_dir,
                        f"cropped_{year}_{month:02d}.png",
                    )
                    _crop_geotiff_to_bbox(dl_path, bbox, cropped_path)
                    img_array = np.array(Image.open(cropped_path).convert("RGB"))

                    frame_path = os.path.join(
                        temp_dir,
                        f"{year}_{month:02d}_"
                        f"{calendar.month_name[month]}.png",
                    )
                    annotate_and_save_frame(
                        img_array, year, month,
                        frame_path, location_name=location_name,
                    )
                    frame_entries.append({
                        "year": year,
                        "month": month,
                        "path": frame_path,
                    })
                    self.log_info(
                        f"    ✅ Fallback frame ready: {year}-{month:02d}",
                        session_id=session_id,
                    )
                    try:
                        os.remove(dl_path)
                    except OSError:
                        pass
                except Exception as fallback_exc:
                    self.log_warning(
                        f"    ❌ Fallback also failed {item_id}: {fallback_exc}",
                        session_id=session_id,
                    )
                    continue

        # Sort chronologically, deduplicate per month
        frame_entries.sort(key=lambda x: (x["year"], x["month"]))
        seen: set = set()
        unique: List[str] = []
        for e in frame_entries:
            k = (e["year"], e["month"])
            if k not in seen:
                seen.add(k)
                unique.append(e["path"])

        return unique

    # ── Capabilities ─────────────────────────────────────────────────────────

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "analysis_type": self.get_analysis_type(),
            "name": "Satellite Video Animation (Timelapse)",
            "description": (
                "Generate animated satellite timelapse GIFs from PlanetScope "
                "true-colour imagery (~3 m resolution). Images are clipped "
                "to the area of interest for fast downloads. Ideal for urban "
                "expansion, glacier retreat, coastal changes, and "
                "construction monitoring."
            ),
            "satellite": "PlanetScope (PSScene)",
            "resolution": "~3 m",
            "data_provider": "Planet Labs (paid subscription)",
            "min_year": 2016,
            "max_time_range_years": 5,
            "temporal_resolution": "Monthly (best cloud-free scene)",
            "use_cases": [
                "Urban expansion / construction monitoring",
                "Glacier retreat tracking",
                "Coastal change detection",
                "Deforestation visualisation",
                "Seasonal change observation",
            ],
            "outputs": [
                "Animated GIF timelapse",
                "Preview grid image",
                "Individual monthly frames",
            ],
            "notes": [
                "Direct Data API download + local crop for speed",
                "Requires PLANET_API_KEY environment variable",
                "~5-15 seconds per scene (vs 5-20 min with Orders API)",
            ],
        }