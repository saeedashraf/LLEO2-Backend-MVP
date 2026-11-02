"""Dump recent video-animation processor log lines from Cloud Run.

Usage:  python scripts/video_logs.py [freshness, default 7d] [limit, default 120]
"""
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8")

freshness = sys.argv[1] if len(sys.argv) > 1 else "7d"
limit = sys.argv[2] if len(sys.argv) > 2 else "120"

FILTER = (
    'resource.type="cloud_run_revision" AND resource.labels.service_name="ndvi6-analysis-api" AND ('
    'textPayload:"GEE" OR textPayload:"Stitching" OR textPayload:"frames ready" OR '
    'textPayload:"Counting scenes" OR textPayload:"Video animation failed" OR '
    'textPayload:"AOI bbox" OR textPayload:"Downloading " OR textPayload:"GCS upload" OR '
    'textPayload:"GIF:" OR textPayload:"Error uploading" OR textPayload:"Uploaded animation" OR '
    'textPayload:"animation.gif" OR textPayload:"Encoding")'
    ' AND NOT textPayload:"Error downloading"'
)
out = subprocess.run(
    ["gcloud.cmd", "logging", "read", FILTER, f"--limit={limit}", f"--freshness={freshness}",
     "--format=value(timestamp,textPayload)"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
for line in reversed(out.stdout.splitlines()):
    print(line[:230])
if out.stderr.strip():
    print("STDERR:", out.stderr[:500])
