"""Call an admin endpoint on prod.  Usage: python scripts/admin_call.py <path> '<json>'
Reads the admin key from %TEMP%/adminkey.txt or LLEO_ADMIN_KEY."""
import json
import os
import sys
from pathlib import Path

import requests

API = os.environ.get("LLEO_API", "https://ndvi6-analysis-api-258557095482.us-central1.run.app").rstrip("/")


def admin_key() -> str:
    k = os.environ.get("LLEO_ADMIN_KEY", "").strip()
    if not k:
        p = Path(os.environ.get("TEMP", "/tmp")) / "adminkey.txt"
        k = p.read_text(encoding="utf-8").strip().lstrip("\ufeff")
    return k


if __name__ == "__main__":
    path = sys.argv[1]
    body = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    r = requests.post(f"{API}{path}", headers={"X-Admin-Key": admin_key()}, json=body, timeout=600)
    print(r.status_code)
    try:
        print(json.dumps(r.json(), indent=2)[:6000])
    except Exception:  # noqa: BLE001
        print(r.text[:2000])
