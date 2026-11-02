"""One-shot production housekeeping. Usage: python scripts/housekeeping.py <step>
steps: stats | prune-dry | prune | restore-dry | restore | regen | community

Previews never expire: `restore` clears false-positive `artifacts_expired`
flags and rebuilds missing preview.png files from the stored location."""
import json
import sys

import requests

from admin_call import API, admin_key  # type: ignore


def post(path, body):
    r = requests.post(f"{API}{path}", headers={"X-Admin-Key": admin_key()}, json=body, timeout=1800)
    print(r.status_code)
    return r.json()


step = sys.argv[1]
if step == "stats":
    r = requests.get(f"{API}/api/admin/stats", headers={"X-Admin-Key": admin_key()}, timeout=60)
    print(r.status_code, json.dumps(r.json(), indent=2))
elif step in ("restore-dry", "restore"):
    j = post("/api/admin/restore-expired-artifacts",
             {"dry_run": step == "restore-dry", "regenerate": True, "limit": 3000})
    print({k: v for k, v in j.items() if not k.endswith("_ids")})
    for k in ("unflagged_ids", "regenerated_ids", "still_missing_ids", "error_ids"):
        print(k, (j.get(k) or [])[:10])
elif step == "prune-dry":
    j = post("/api/admin/prune-expired-artifacts", {"dry_run": True, "only_shared": False, "limit": 3000})
    print({k: v for k, v in j.items() if k not in ("expired_session_ids", "sessions", "expired")})
    ids = j.get("expired_session_ids") or j.get("sessions") or j.get("expired") or []
    print("sample:", ids[:5])
elif step == "prune":
    j = post("/api/admin/prune-expired-artifacts", {"dry_run": False, "only_shared": False, "limit": 3000})
    print({k: v for k, v in j.items() if k not in ("expired_session_ids", "sessions", "expired")})
elif step == "regen":
    j = post("/api/admin/regenerate-thumbnails", {"only_missing": True, "limit": 500})
    print(json.dumps({k: v for k, v in j.items() if k not in ("regenerated_session_ids", "sessions")}, indent=2)[:3000])
elif step == "community":
    r = requests.get(f"{API}/api/community", timeout=60)
    items = r.json()
    items = items.get("items", items) if isinstance(items, dict) else items
    print("count:", len(items))
    for it in items[:40]:
        print(it.get("session_id"), "|", it.get("analysis_type"), "|", it.get("user_name"), "|", it.get("artifacts_expired"), "|", (it.get("urls") or {}).get("thumbnail", "")[:40])
