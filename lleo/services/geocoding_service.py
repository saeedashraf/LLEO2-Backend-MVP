"""
LLEO v2 — Geocoding service.

If the single-call router returns a location_name but no lat/lon,
this service resolves coordinates via the Google Geocoding API.
Zero LLM calls.
"""

from __future__ import annotations

import os
from typing import Optional

import requests


async def geocode_location(
    location_name: str,
    get_secret_func=None,
) -> dict:
    """
    Resolve *location_name* → {name, latitude{}, longitude{}} via
    the Google Geocoding REST API.
    """
    api_key = _get_api_key(get_secret_func)

    if not api_key:
        print("⚠️  No Google Geocoding API key — returning zero coords")
        return _empty_location(location_name)

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    params = {"address": location_name, "key": api_key}

    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()

        if data.get("status") != "OK" or not data.get("results"):
            print(f"⚠️  Geocoding failed for '{location_name}': {data.get('status')}")
            return _empty_location(location_name)

        result = data["results"][0]
        geo = result["geometry"]["location"]
        lat, lng = geo["lat"], geo["lng"]

        vp = result["geometry"].get("viewport", {})
        ne = vp.get("northeast", {"lat": lat + 0.05, "lng": lng + 0.05})
        sw = vp.get("southwest", {"lat": lat - 0.05, "lng": lng - 0.05})

        formatted = result.get("formatted_address", location_name)
        print(f"📍 Geocoded '{location_name}' → {lat:.4f}, {lng:.4f}")

        return {
            "name": formatted,
            "latitude": {"center": lat, "min": sw["lat"], "max": ne["lat"]},
            "longitude": {"center": lng, "min": sw["lng"], "max": ne["lng"]},
        }

    except Exception as exc:
        print(f"⚠️  Geocoding request error: {exc}")
        return _empty_location(location_name)


def flatten_location(location_info: dict, fallback_name: str = "Unknown") -> dict:
    """
    Convert the nested lat/lon dict to the flat format processors expect::

        {name, latitude, longitude, bounds: {lat_min, lat_max, lng_min, lng_max}}
    """
    lat_data = location_info.get("latitude", {})
    lon_data = location_info.get("longitude", {})

    if isinstance(lat_data, dict):
        lat_c = lat_data.get("center", 0)
        lat_min = lat_data.get("min", lat_c - 0.05)
        lat_max = lat_data.get("max", lat_c + 0.05)
    else:
        lat_c = float(lat_data) if lat_data else 0
        lat_min, lat_max = lat_c - 0.05, lat_c + 0.05

    if isinstance(lon_data, dict):
        lon_c = lon_data.get("center", 0)
        lon_min = lon_data.get("min", lon_c - 0.05)
        lon_max = lon_data.get("max", lon_c + 0.05)
    else:
        lon_c = float(lon_data) if lon_data else 0
        lon_min, lon_max = lon_c - 0.05, lon_c + 0.05

    return {
        "name": location_info.get("name", fallback_name),
        "latitude": lat_c,
        "longitude": lon_c,
        "bounds": {
            "lat_min": lat_min, "lat_max": lat_max,
            "lng_min": lon_min, "lng_max": lon_max,
        },
    }


def build_location_from_decision(decision, get_secret_func=None) -> Optional[dict]:
    """
    Build the nested location dict from a RoutingDecision.

    Returns None when the LLM didn't provide coords — caller should
    then fall back to ``geocode_location()``.
    """
    BUFFER = 0.05

    if decision.latitude is not None and decision.longitude is not None:
        lat, lon = decision.latitude, decision.longitude
        return {
            "name": decision.location_name or "Unknown",
            "latitude": {"center": lat, "min": lat - BUFFER, "max": lat + BUFFER},
            "longitude": {"center": lon, "min": lon - BUFFER, "max": lon + BUFFER},
        }

    return None


# ── Private ──────────────────────────────────────────────────────────────────

def _get_api_key(get_secret_func=None) -> Optional[str]:
    key = os.getenv("GOOGLE_PLACES_API_KEY") or os.getenv("GOOGLE_GEOCODING_API_KEY")
    if key:
        return key.strip()
    if get_secret_func:
        try:
            return get_secret_func("google-places-key")
        except Exception:
            pass
    return None


def _empty_location(name: str) -> dict:
    return {
        "name": name,
        "latitude": {"center": 0, "min": -0.05, "max": 0.05},
        "longitude": {"center": 0, "min": -0.05, "max": 0.05},
    }
