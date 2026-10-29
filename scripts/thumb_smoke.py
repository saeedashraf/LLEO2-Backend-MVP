"""Local smoke test: render thumbnails for bbox + point-only locations."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image  # noqa: E402

from lleo.utils.thumbnail import _zoom_for_bounds, create_satellite_thumbnail  # noqa: E402

out = os.path.join(os.environ.get("TEMP", "/tmp"), "thumb_test")
os.makedirs(out, exist_ok=True)

cases = [
    ("la.png", {"name": "Los Angeles, CA, USA", "latitude": 34.0522, "longitude": -118.2437,
                "bounds": {"lat_min": 33.7037, "lat_max": 34.3373, "lng_min": -118.6682, "lng_max": -118.1553}},
     "vegetation_index", 2023, 2024),
    ("mt.png", {"name": "Mato Grosso, Brazil", "latitude": -12.6, "longitude": -55.7,
                "bounds": {"lat_min": -18.0, "lat_max": -7.3, "lng_min": -61.6, "lng_max": -50.2}},
     "vegetation_index", 2019, 2024),
    ("delhi.png", {"name": "Delhi", "latitude": 28.7041, "longitude": 77.1025}, "air_quality", None, None),
]
for fname, loc, atype, sy, ey in cases:
    b = loc.get("bounds")
    if b:
        print(fname, "zoom", _zoom_for_bounds(b["lat_min"], b["lat_max"], b["lng_min"], b["lng_max"], 400, 300))
    ok = create_satellite_thumbnail(loc, atype, os.path.join(out, fname), sy, ey)
    p = os.path.join(out, fname)
    im = Image.open(p)
    print(fname, ok, im.size, os.path.getsize(p), "bytes")
print("OUT:", out)
