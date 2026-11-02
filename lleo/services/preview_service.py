"""
Preview (thumbnail) durability helpers.

Previews must never "expire": the GCS bucket has no lifecycle rule, and even
if a ``preview.png`` object goes missing we can rebuild it from the location
stored in Cloud SQL.  This module centralises that rebuild so both the admin
tooling and the ``/results/{id}/preview`` endpoint can self-heal.
"""
from __future__ import annotations

import os
import tempfile
import threading
from typing import Dict, List, Optional

from ..database.models import AnalysisRecord

PREVIEW_KEYS = ("preview", "map", "charts", "chart", "animation")

# One regeneration per session at a time (a card grid can request the same
# missing preview several times in parallel).
_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(session_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(session_id)
        if lock is None:
            lock = _locks[session_id] = threading.Lock()
        return lock


def artifact_paths(rec: AnalysisRecord) -> List[str]:
    """gs:// / local paths that should exist for this analysis (preview first)."""
    paths: List[str] = []
    if rec.thumbnail_path:
        paths.append(rec.thumbnail_path)
    for key in PREVIEW_KEYS:
        p = (rec.files or {}).get(key)
        if p and p not in paths:
            paths.append(p)
    return paths


def location_for_thumbnail(rec: AnalysisRecord) -> Optional[dict]:
    """Rebuild a thumbnail-friendly location dict from what the row stores."""
    coords = rec.coordinates if isinstance(rec.coordinates, dict) else {}
    lat = rec.location_lat
    lng = rec.location_lng
    if lat is None or lng is None:
        lat = coords.get("latitude", coords.get("lat"))
        lng = coords.get("longitude", coords.get("lon", coords.get("lng")))
        if isinstance(lat, dict):
            lat = lat.get("center")
        if isinstance(lng, dict):
            lng = lng.get("center")
    if lat is None or lng is None:
        return None
    try:
        loc: dict = {
            "name": rec.location_name or "Unknown",
            "latitude": float(lat),
            "longitude": float(lng),
        }
    except (TypeError, ValueError):
        return None
    b = coords.get("bounds") if isinstance(coords.get("bounds"), dict) else None
    if b and all(k in b for k in ("lat_min", "lat_max", "lng_min", "lng_max")):
        loc["bounds"] = b
    elif isinstance(coords.get("latitude"), dict) and isinstance(coords.get("longitude"), dict):
        la, lo = coords["latitude"], coords["longitude"]
        if "min" in la and "min" in lo:
            loc["bounds"] = {
                "lat_min": la["min"], "lat_max": la["max"],
                "lng_min": lo["min"], "lng_max": lo["max"],
            }
    return loc


def blob_exists(fm, path: str) -> bool:
    """Existence check that *raises* on infrastructure errors.

    ``CloudFileManager.file_exists`` swallows every exception and returns
    ``False`` — fine for a UI hint, dangerous for anything that flags rows as
    expired.  Here a gs:// path with no working GCS client, or a transient
    API error, is an error rather than "missing".
    """
    if not path:
        return False
    if path.startswith("gs://"):
        if not getattr(fm, "use_cloud", False) or getattr(fm, "bucket", None) is None:
            raise RuntimeError("GCS client not initialised; cannot verify gs:// artefacts")
        blob_name = path.replace(f"gs://{fm.bucket_name}/", "", 1)
        return bool(fm.bucket.blob(blob_name).exists())
    # Local path: os.path.exists never raises, so the manager's own check is safe.
    if hasattr(fm, "file_exists"):
        return bool(fm.file_exists(path))
    return os.path.exists(path)


def any_artifact_alive(fm, rec: AnalysisRecord) -> Optional[bool]:
    """True/False if at least one artefact exists, ``None`` when the row has
    no artefact paths at all (nothing to verify)."""
    paths = artifact_paths(rec)
    if not paths:
        return None
    return any(blob_exists(fm, p) for p in paths)


def regenerate_preview(rec: AnalysisRecord, fm) -> Optional[str]:
    """Rebuild ``preview.png`` from the stored location and upload it.

    Mutates ``rec.thumbnail_path`` / ``rec.files['preview']`` and clears the
    ``artifacts_expired`` flag; the caller commits.  Returns the new cloud
    path, or ``None`` when the row has no usable location or the build failed.
    """
    from ..utils.thumbnail import create_satellite_thumbnail

    loc = location_for_thumbnail(rec)
    if not loc:
        return None

    tmp_dir = os.path.join(tempfile.gettempdir(), "lleo")
    os.makedirs(tmp_dir, exist_ok=True)
    local = os.path.join(tmp_dir, f"{rec.session_id}_preview.png")

    with _lock_for(rec.session_id):
        ok = create_satellite_thumbnail(
            location=loc,
            analysis_type=rec.analysis_type or "analysis",
            thumbnail_path=local,
            start_year=rec.start_year,
            end_year=rec.end_year,
        )
        if not ok or not os.path.exists(local):
            return None
        try:
            if getattr(fm, "use_cloud", False):
                cloud_path = fm.save_file_to_cloud(local, f"analysis_results/{rec.session_id}/preview.png")
                # save_file_to_cloud returns the *local* path on upload
                # failure — never persist that (the temp file is deleted below).
                if not str(cloud_path).startswith("gs://"):
                    print(f"⚠️  preview upload failed for {rec.session_id}: {cloud_path}")
                    return None
            else:
                # Local-storage fallback: keep the file where the results
                # endpoints can read it.
                dest_dir = os.path.join(getattr(fm, "local_results_dir", "analysis_results"), rec.session_id)
                os.makedirs(dest_dir, exist_ok=True)
                cloud_path = os.path.join(dest_dir, "preview.png")
                os.replace(local, cloud_path)
        except Exception as exc:  # pragma: no cover - network path
            print(f"⚠️  preview upload failed for {rec.session_id}: {exc}")
            return None
        finally:
            try:
                os.remove(local)
            except OSError:
                pass

    rec.thumbnail_path = cloud_path
    files = dict(rec.files or {})
    files["preview"] = cloud_path
    rec.files = files
    rec.artifacts_expired = False
    return cloud_path
