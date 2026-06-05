"""
Standalone test: GEE monthly-median timelapse GIF.

Generates a Sentinel-2 monthly composite animation for a chosen AOI and date
range, then downloads it as a local GIF so quality can be eyeballed before
committing to a full processor rewrite.

Run from satellite-backend-2/:
    python test_gee_animation.py
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
from datetime import datetime

import ee
from google.oauth2.service_account import Credentials


# ── CONFIG (edit these) ──────────────────────────────────────────────────────

LOCATION_NAME = "Dubai Airport"
CENTER_LAT = 25.2532
CENTER_LON = 55.3657
HALF_SIZE_DEG = 0.06          # ~6 km half-side. Increase for wider view.

START_YEAR = 2020
END_YEAR = 2024               # inclusive

FPS = 4                       # 4 fps × 60 frames = 15 s loop
DIMENSIONS = 600              # output GIF max edge in pixels.
                              # GEE limit: frames × W × H ≤ 26,214,400 px.
                              # 60 frames × 600² = 21.6M ✓ (768² would be 35.4M ✗)

CLOUD_PCT_MAX = 80            # filter scenes above this scene-level cloud %
VIS_MIN = 0
VIS_MAX = 3000                # Sentinel-2 SR reflectance stretch (consistent across frames)

OUTPUT_DIR = "test_gee_output"
SERVICE_ACCOUNT_JSON = "gen-lang-client-0709485529-7db838c888b4.json"
GCP_PROJECT = "gen-lang-client-0709485529"


# ── EARTH ENGINE INIT ────────────────────────────────────────────────────────

def init_ee() -> None:
    if not os.path.exists(SERVICE_ACCOUNT_JSON):
        sys.exit(f"❌ Service account JSON not found: {SERVICE_ACCOUNT_JSON}")
    credentials = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_JSON,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    ee.Initialize(credentials, project=GCP_PROJECT)
    print(f"✅ Earth Engine ready (project: {GCP_PROJECT})")


# ── PIPELINE ─────────────────────────────────────────────────────────────────

def mask_s2_clouds(img: ee.Image) -> ee.Image:
    """Mask cloud, shadow, cirrus pixels using the SCL band."""
    scl = img.select("SCL")
    # SCL classes to drop: 3=cloud-shadow, 8=cloud-medium, 9=cloud-high, 10=thin-cirrus
    mask = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
    )
    return img.updateMask(mask)


def build_monthly_frames(aoi: ee.Geometry) -> ee.ImageCollection:
    """Return one RGB-visualized image per month over the date range."""
    base = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(aoi)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", CLOUD_PCT_MAX))
        .map(mask_s2_clouds)
    )

    frames: list = []
    for year in range(START_YEAR, END_YEAR + 1):
        for month in range(1, 13):
            start = ee.Date.fromYMD(year, month, 1)
            end = start.advance(1, "month")
            monthly = base.filterDate(start, end).median()
            # Composite a black background so months with NO clear pixels still
            # produce a frame (avoids "missing month" gaps in the GIF).
            monthly = ee.Image(0).blend(monthly)
            frame = (
                monthly
                .visualize(bands=["B4", "B3", "B2"], min=VIS_MIN, max=VIS_MAX)
                .set({
                    "system:time_start": start.millis(),
                    "label": f"{year}-{month:02d}",
                })
            )
            frames.append(frame)

    print(f"   {len(frames)} frames queued ({START_YEAR}-{END_YEAR}, monthly)")
    return ee.ImageCollection(frames)


def download_gif(url: str, out_path: str) -> None:
    print(f"⬇️  Downloading GIF from Earth Engine …")
    t0 = time.time()
    urllib.request.urlretrieve(url, out_path)
    size_mb = os.path.getsize(out_path) / 1024 / 1024
    print(f"✅ Saved {out_path}  ({size_mb:.1f} MB, {time.time() - t0:.1f}s)")


def main() -> None:
    print(f"🛰️  Building timelapse for {LOCATION_NAME} ({CENTER_LAT}, {CENTER_LON})")
    print(f"   Range: {START_YEAR}-01 → {END_YEAR}-12")

    init_ee()

    aoi = ee.Geometry.Rectangle([
        CENTER_LON - HALF_SIZE_DEG, CENTER_LAT - HALF_SIZE_DEG,
        CENTER_LON + HALF_SIZE_DEG, CENTER_LAT + HALF_SIZE_DEG,
    ])

    video = build_monthly_frames(aoi)

    print("🎞️  Requesting server-side GIF render …")
    gif_url = video.getVideoThumbURL({
        "dimensions": DIMENSIONS,
        "region": aoi,
        "framesPerSecond": FPS,
        "crs": "EPSG:3857",
        "format": "gif",
    })
    print(f"   URL: {gif_url[:120]}…")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = LOCATION_NAME.lower().replace(" ", "_")
    out_path = os.path.join(OUTPUT_DIR, f"{safe_name}_{START_YEAR}_{END_YEAR}_{stamp}.gif")

    download_gif(gif_url, out_path)

    print()
    print(f"🎬 Open this file to inspect quality:")
    print(f"   {os.path.abspath(out_path)}")


if __name__ == "__main__":
    main()
