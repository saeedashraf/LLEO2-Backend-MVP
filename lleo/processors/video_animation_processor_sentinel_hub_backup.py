"""
Video Animation Processor – Satellite Timelapse Generator

Downloads monthly Sentinel-2 true-colour images via Sentinel Hub API,
annotates each frame with a date stamp, and stitches them into an
animated GIF that is uploaded to GCS.

Based on the UZH-ETHZ notebook2 pipeline.
"""

from __future__ import annotations

import calendar
import math
import os
import time
from typing import Any, Dict, List, Optional

import numpy as np

from sentinelhub import (
    SHConfig,
    BBox,
    CRS,
    bbox_to_dimensions,
    SentinelHubRequest,
    DataCollection,
    MimeType,
    MosaickingOrder,
)

from lleo.core.base_processor import BaseProcessor, AnalysisRequest, AnalysisResult
from lleo.core.config import LLEOConfig
from lleo.core.exceptions import ProcessorException, ValidationException
from lleo.visualization.video_animation_visualization import (
    annotate_and_save_frame,
    create_animation_gif,
    create_preview_grid,
)


# ── Sentinel Hub evalscript (true-colour RGB) ───────────────────────────────

EVALSCRIPT_TRUE_COLOR = """
//VERSION=3
function setup() {
    return {
        input: [{ bands: ["B02", "B03", "B04"] }],
        output: { bands: 3 }
    };
}
function evaluatePixel(sample) {
    return [sample.B04, sample.B03, sample.B02];
}
"""


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
        latitude - math.degrees(d_lat),   # min_lat
        longitude + math.degrees(d_lon),  # max_lon
        latitude + math.degrees(d_lat),   # max_lat
    )


def _last_day_of_month(year: int, month: int) -> int:
    """Return the last calendar day for a given year/month."""
    return calendar.monthrange(year, month)[1]


# ── Processor ────────────────────────────────────────────────────────────────

class VideoAnimationProcessor(BaseProcessor):
    """
    Sentinel-2 satellite timelapse processor.

    Capabilities:
    - Urban expansion visualisation
    - Glacier retreat tracking
    - Coastal change monitoring
    - Construction / development progress
    """

    def __init__(self, config: LLEOConfig):
        super().__init__(config)
        self._sh_config: Optional[SHConfig] = None
        self.log_info("VideoAnimationProcessor initialised")

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
            if start_year < 2015:
                raise ValidationException(
                    "Sentinel-2 data is only available from June 2015 onwards"
                )
            if end_year - start_year > 5:
                raise ValidationException(
                    "Maximum time range for video animation is 5 years "
                    "(to keep GIF size manageable)"
                )
        return True

    async def process(self, request: AnalysisRequest) -> AnalysisResult:
        start_time = time.time()

        try:
            self.log_info(
                "Starting video animation",
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

            # Single-year → compare previous year
            if start_year == end_year:
                start_year -= 1
                start_month = 1
                end_month = 12

            params = request.parameters or {}
            resolution = params.get("resolution", 10)          # m/px
            distance_m = params.get("distance", 5000)           # bbox radius
            frame_duration = params.get("frame_duration", 600)  # ms

            # ── Configure Sentinel Hub ───────────────────────────────────
            sh_config = self._get_sentinel_hub_config()

            # ── Bounding box ─────────────────────────────────────────────
            bbox_coords = _get_bounding_box(lat, lon, distance_m)
            target_bbox = BBox(bbox=bbox_coords, crs=CRS.WGS84)
            target_size = bbox_to_dimensions(target_bbox, resolution=resolution)

            self.log_info(
                f"BBox: {bbox_coords}, size: {target_size}, "
                f"range: {start_year}-{start_month:02d} → {end_year}-{end_month:02d}",
                session_id=request.session_id,
            )

            # ── Download monthly frames ──────────────────────────────────
            temp_dir = os.path.join(self.config.temp_dir, request.session_id)
            os.makedirs(temp_dir, exist_ok=True)

            frame_paths: List[str] = []
            months_downloaded = 0
            months_failed = 0

            for year in range(start_year, end_year + 1):
                m_start = start_month if year == start_year else 1
                m_end = end_month if year == end_year else 12

                for month in range(m_start, m_end + 1):
                    try:
                        img_array = self._fetch_sentinel_image(
                            sh_config, target_bbox, target_size, year, month
                        )

                        frame_path = os.path.join(
                            temp_dir,
                            f"{year}_{month:02d}_{calendar.month_name[month]}.png",
                        )

                        annotate_and_save_frame(
                            img_array, year, month, frame_path,
                            location_name=location_name,
                        )
                        frame_paths.append(frame_path)
                        months_downloaded += 1
                        self.log_info(
                            f"  ✅ Frame {year}-{month:02d}",
                            session_id=request.session_id,
                        )

                    except Exception as exc:
                        months_failed += 1
                        self.log_warning(
                            f"  ⚠️ Skip {year}-{month:02d}: {exc}",
                            session_id=request.session_id,
                        )

            if not frame_paths:
                raise ProcessorException(
                    "Could not download any satellite frames. "
                    "The area/time range may have no cloud-free imagery."
                )

            self.log_info(
                f"Downloaded {months_downloaded} frames ({months_failed} failed)",
                session_id=request.session_id,
            )

            # ── Create GIF ───────────────────────────────────────────────
            gif_path = os.path.join(temp_dir, "animation.gif")
            create_animation_gif(frame_paths, gif_path, duration_ms=frame_duration)

            # ── Create preview grid ──────────────────────────────────────
            preview_path = os.path.join(temp_dir, "preview.png")
            create_preview_grid(
                frame_paths, preview_path, location_name=location_name
            )

            # ── Upload to GCS ────────────────────────────────────────────
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

                    # Upload individual frames for potential future use
                    first_frame = frame_paths[0]
                    outputs["charts"] = self.file_manager.save_file_to_cloud(
                        first_frame,
                        f"analysis_results/{sid}/first_frame.png",
                    )

                    self.log_info("Files uploaded to GCS", session_id=sid)
                except Exception as exc:
                    self.log_error(f"GCS upload failed: {exc}", session_id=sid)
                    outputs["animation"] = gif_path
                    outputs["preview"] = preview_path
            else:
                outputs["animation"] = gif_path
                outputs["preview"] = preview_path

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
                "resolution_m": resolution,
                "distance_m": distance_m,
                "total_frames": len(frame_paths),
                "frames_failed": months_failed,
                "frame_duration_ms": frame_duration,
                "satellite": "Sentinel-2 L1C",
                "analysis_date": time.strftime("%Y-%m-%d %H:%M:%S"),
                "coordinates": {"lat": lat, "lon": lon},
            }

            self.log_info(
                f"Video animation completed in {processing_time:.1f}s  "
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
                f"Unexpected error: {exc}", session_id=request.session_id
            )
            raise ProcessorException(f"Video animation failed: {exc}")

    # ── Sentinel Hub helpers ─────────────────────────────────────────────────

    def _get_sentinel_hub_config(self) -> SHConfig:
        """Lazily build and cache SHConfig using secrets or env vars."""
        if self._sh_config is not None:
            return self._sh_config

        client_id = None
        client_secret = None

        # 1) Try env vars
        client_id = os.getenv("SENTINEL_HUB_CLIENT_ID")
        client_secret = os.getenv("SENTINEL_HUB_CLIENT_SECRET")

        # 2) Try GCP Secret Manager
        if not client_id and hasattr(self, "get_secret") and self.get_secret:
            try:
                client_id = self.get_secret("sentinel-hub-client-id")
            except Exception:
                pass
        if not client_secret and hasattr(self, "get_secret") and self.get_secret:
            try:
                client_secret = self.get_secret("sentinel-hub-client-secret")
            except Exception:
                pass

        # 3) Fall back to LLEOConfig
        if not client_id:
            client_id = self.config.sentinel_hub_client_id
        if not client_secret:
            client_secret = self.config.sentinel_hub_client_secret

        if not client_id or not client_secret:
            raise ProcessorException(
                "Sentinel Hub credentials not configured. "
                "Set SENTINEL_HUB_CLIENT_ID / SENTINEL_HUB_CLIENT_SECRET "
                "env vars or store them in GCP Secret Manager."
            )

        cfg = SHConfig()
        cfg.sh_client_id = client_id.strip()
        cfg.sh_client_secret = client_secret.strip()
        self._sh_config = cfg

        self.log_info(
            f"Sentinel Hub configured (client_id: {client_id[:8]}…)"
        )
        return cfg

    def _fetch_sentinel_image(
        self,
        sh_config: SHConfig,
        bbox: BBox,
        size: tuple,
        year: int,
        month: int,
    ) -> np.ndarray:
        """
        Fetch one month's least-cloud-cover Sentinel-2 mosaic as uint8 RGB.

        Returns:
            np.ndarray of shape (H, W, 3), dtype uint8
        """
        last_day = _last_day_of_month(year, month)

        request = SentinelHubRequest(
            evalscript=EVALSCRIPT_TRUE_COLOR,
            input_data=[
                SentinelHubRequest.input_data(
                    data_collection=DataCollection.SENTINEL2_L1C,
                    time_interval=(
                        f"{year}-{month:02d}-01",
                        f"{year}-{month:02d}-{last_day:02d}",
                    ),
                    mosaicking_order=MosaickingOrder.LEAST_CC,
                )
            ],
            responses=[
                SentinelHubRequest.output_response("default", MimeType.PNG)
            ],
            bbox=bbox,
            size=size,
            config=sh_config,
        )

        data = request.get_data()
        if not data or len(data) == 0:
            raise ProcessorException(
                f"No imagery returned for {year}-{month:02d}"
            )

        image = data[0].astype(np.uint8)
        return image

    # ── Capabilities ─────────────────────────────────────────────────────────

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "analysis_type": self.get_analysis_type(),
            "name": "Satellite Video Animation (Timelapse)",
            "description": (
                "Generate animated satellite timelapse GIFs from Sentinel-2 "
                "true-colour imagery. Ideal for urban expansion, glacier "
                "retreat, coastal changes, and construction monitoring."
            ),
            "satellite": "Sentinel-2 L1C",
            "bands_used": ["B02 (Blue)", "B03 (Green)", "B04 (Red)"],
            "min_year": 2015,
            "max_time_range_years": 5,
            "spatial_resolution": "10 m",
            "temporal_resolution": "Monthly composites",
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
        }
