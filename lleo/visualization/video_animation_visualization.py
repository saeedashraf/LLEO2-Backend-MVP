"""
Video Animation Visualization Module

Creates annotated satellite image frames and stitches them into an animated GIF.
Based on UZH-ETHZ notebook logic for Sentinel-2 timelapse generation.
"""

import calendar
import os
from typing import List, Optional, Tuple

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image

import logging

logger = logging.getLogger(__name__)


# ── Frame annotation ─────────────────────────────────────────────────────────

def annotate_and_save_frame(
    image_array: np.ndarray,
    year: int,
    month: int,
    save_path: str,
    location_name: str = "",
    brightness_factor: float = 2.0,
) -> str:
    """
    Take a raw Sentinel-2 RGB array, apply brightness scaling, annotate with
    a date label, and save as a PNG frame.

    Args:
        image_array: uint8 array of shape (H, W, 3) — B04/B03/B02 (RGB)
        year: Image year
        month: Image month (1-12)
        save_path: Full path for the output PNG
        location_name: Optional location name for the annotation
        brightness_factor: Multiplicative brightness (default 2.0 as in notebook)

    Returns:
        save_path on success
    """
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    month_name = calendar.month_name[month]
    timestamp = f"{year} – {month_name}"

    fig, ax = plt.subplots(figsize=(5, 5))
    # Brightness-scale the same way the notebook does: factor / 255
    factor = brightness_factor / 255.0
    ax.imshow(np.clip(image_array * factor, 0, 1))
    ax.axis("off")

    # White date label in top-left
    label = f"{timestamp}  {location_name}" if location_name else timestamp
    ax.annotate(
        label,
        xy=(0.0125, 0.967),
        xycoords="axes fraction",
        ha="left",
        va="center",
        fontsize=14,
        color="white",
        bbox=dict(boxstyle="round,pad=0.3", facecolor="black", alpha=0.5),
    )

    plt.savefig(save_path, dpi=150, bbox_inches="tight", pad_inches=0)
    plt.close(fig)
    return save_path


# ── GIF assembly ─────────────────────────────────────────────────────────────

def create_animation_gif(
    frame_paths: List[str],
    output_path: str,
    duration_ms: int = 600,
    loop: int = 0,
) -> str:
    """
    Stitch a list of PNG frame paths into an animated GIF.

    Args:
        frame_paths: Ordered list of PNG file paths
        output_path: Destination .gif path
        duration_ms: Milliseconds between frames (default 600)
        loop: 0 = infinite loop

    Returns:
        output_path
    """
    if not frame_paths:
        raise ValueError("No frames provided for GIF creation")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    frames: List[Image.Image] = []
    for fp in frame_paths:
        img = Image.open(fp).convert("RGBA")
        frames.append(img)

    # Resize all to same dimensions (use first frame as reference)
    ref_size = frames[0].size
    frames = [f.resize(ref_size, Image.LANCZOS) if f.size != ref_size else f for f in frames]

    # Convert to palette mode for GIF
    rgb_frames = [f.convert("RGB") for f in frames]

    rgb_frames[0].save(
        output_path,
        save_all=True,
        append_images=rgb_frames[1:],
        duration=duration_ms,
        loop=loop,
        optimize=True,
    )

    file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
    logger.info(f"GIF created: {output_path} ({len(frames)} frames, {file_size_mb:.1f} MB)")
    return output_path


# ── Summary preview image ────────────────────────────────────────────────────

def create_preview_grid(
    frame_paths: List[str],
    output_path: str,
    max_cols: int = 4,
    location_name: str = "",
) -> str:
    """
    Create a grid preview image showing a selection of key frames.

    Args:
        frame_paths: All frame paths
        output_path: Destination PNG
        max_cols: Max columns in grid
        location_name: Location label

    Returns:
        output_path
    """
    if not frame_paths:
        raise ValueError("No frames for preview grid")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Pick evenly-spaced frames (max 8)
    n = min(len(frame_paths), 8)
    indices = np.linspace(0, len(frame_paths) - 1, n, dtype=int)
    selected = [frame_paths[i] for i in indices]

    cols = min(max_cols, len(selected))
    rows = (len(selected) + cols - 1) // cols

    fig, axes = plt.subplots(rows, cols, figsize=(4 * cols, 4 * rows))
    if rows == 1 and cols == 1:
        axes = np.array([[axes]])
    elif rows == 1:
        axes = axes[np.newaxis, :]
    elif cols == 1:
        axes = axes[:, np.newaxis]

    for idx, ax in enumerate(axes.flat):
        if idx < len(selected):
            img = Image.open(selected[idx])
            ax.imshow(np.array(img))
            # Extract filename for label
            fname = os.path.basename(selected[idx]).replace(".png", "")
            ax.set_title(fname, fontsize=9)
        ax.axis("off")

    title = f"Satellite Timelapse – {location_name}" if location_name else "Satellite Timelapse"
    fig.suptitle(title, fontsize=14, fontweight="bold")
    plt.tight_layout()
    plt.savefig(output_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return output_path
