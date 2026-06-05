"""
Location Extraction and Geocoding with Typo Correction
Uses Vertex AI Gemini for typo correction and coordinate extraction
Falls back to Google Geocoding API if needed
"""

import re
import requests
from fastapi import HTTPException


def extract_numeric_field(text: str, field_name: str) -> float:
    """Extract numeric value from text field"""
    pattern = rf"{field_name}:\s*([-+]?\d*\.?\d+)"
    match = re.search(pattern, text, re.IGNORECASE)
    return float(match.group(1)) if match else None


async def extract_location_data(query: str, routing_result: dict, model, get_secret_func) -> dict:
    """
    Extract location coordinates using Vertex AI with typo correction.
    Falls back to Google Geocoding API if Gemini fails.

    Args:
        query: User's original query
        routing_result: Result from smart_route_query containing location name
        model: Vertex AI Gemini model instance
        get_secret_func: Function to retrieve Google Places API key from secrets

    Returns:
        dict: Location data with name, latitude bounds, longitude bounds

    Raises:
        HTTPException: If location cannot be geocoded
    """
    location_name = routing_result.get('location', 'Unknown Location')

    # Check for invalid location early
    if not location_name or location_name in ["Unknown Location", "unknown", "", "null"]:
        raise HTTPException(
            status_code=400,
            detail="No valid location found. Please use /chat/start for interactive location clarification."
        )

    print(f"🌍 Extracting coordinates for: {location_name}")

    # First, try to correct typos using Gemini
    correction_prompt = f"""
    The user mentioned this location: "{location_name}"

    If this is a typo or misspelling of a well-known place, provide the CORRECT name.
    If it's already correct, respond with the same name.

    Examples:
    - "Lake grefense" → "Lake Greifensee" or "Greifensee"
    - "Shiraz" → "Shiraz"
    - "Teh ran" → "Tehran"
    - "Ouraga dugu" → "Ouagadougou"

    Respond with ONLY the corrected location name, nothing else.
    Corrected name:"""

    try:
        correction_response = model.generate_content(correction_prompt, generation_config={
            "max_output_tokens": 512,  # Increased for complete location names
            "temperature": 0.1
        })
        corrected_name = correction_response.text.strip()
        print(f"📝 Typo correction: '{location_name}' → '{corrected_name}'")

        # Use corrected name for geocoding (only if it's different and not a truncation)
        if corrected_name and corrected_name not in [location_name, "null", "None", ""] and not corrected_name.endswith(','):
            location_name = corrected_name
            print(f"✅ Using corrected name: {location_name}")
    except Exception as e:
        print(f"⚠️ Typo correction failed: {e}, proceeding with original name")

    # Now get coordinates for the (possibly corrected) location
    prompt = f"""
    Get the geographic coordinates for: {location_name}

    **IMPORTANT - Area Size Limits**:
    - Maximum bounding box: 0.1 degrees (~11km) in each direction
    - Recommended: 0.05 degrees (~5-6km) for focused analysis
    - If user provides specific coordinates, use them if within limits
    - If user-provided area is too large (>0.1 degrees), reduce it to 0.1 degrees and mention this

    **Query**: "{query}"

    Check if the user provided specific lat/long boundaries in their query.
    If yes, extract and use them (but enforce the 0.1 degree maximum).
    If no, provide a focused bounding box for {location_name}.

    Provide a bounding box suitable for satellite imagery analysis:
    - For a city/town: ~0.05 degrees (5-6km) focused on center
    - For a specific location: ~0.03 degrees (3km) very focused
    - For a region: up to 0.1 degrees maximum

    Respond with ONLY the numeric values:
    Min Latitude: [number]
    Max Latitude: [number]
    Min Longitude: [number]
    Max Longitude: [number]
    Center Latitude: [number]
    Center Longitude: [number]
    """

    try:
        response = model.generate_content(prompt, generation_config={
            "max_output_tokens": 8192,
            "temperature": 0.1
        })

        text = response.text.strip()
        print(f"📐 Coordinates response: {text}")

        min_lat = extract_numeric_field(text, "Min Latitude")
        max_lat = extract_numeric_field(text, "Max Latitude")
        min_lng = extract_numeric_field(text, "Min Longitude")
        max_lng = extract_numeric_field(text, "Max Longitude")
        center_lat = extract_numeric_field(text, "Center Latitude")
        center_lng = extract_numeric_field(text, "Center Longitude")

        if all([min_lat, max_lat, min_lng, max_lng]):
            # Enforce maximum area limits (0.1 degrees in each direction)
            MAX_SPAN = 0.1
            lat_span = max_lat - min_lat
            lng_span = max_lng - min_lng

            if lat_span > 2 * MAX_SPAN or lng_span > 2 * MAX_SPAN:
                print(f"⚠️  Area too large ({lat_span:.3f}° x {lng_span:.3f}°), reducing to max {2*MAX_SPAN}°")
                # Reduce around center
                calc_center_lat = center_lat or (min_lat + max_lat) / 2
                calc_center_lng = center_lng or (min_lng + max_lng) / 2
                min_lat = calc_center_lat - MAX_SPAN
                max_lat = calc_center_lat + MAX_SPAN
                min_lng = calc_center_lng - MAX_SPAN
                max_lng = calc_center_lng + MAX_SPAN

            print(f"✅ Coordinates extracted: ({min_lat:.4f}, {min_lng:.4f}) to ({max_lat:.4f}, {max_lng:.4f})")
            return {
                "location": {
                    "name": location_name,
                    "latitude": {"min": min_lat, "max": max_lat, "center": center_lat or (min_lat + max_lat) / 2},
                    "longitude": {"min": min_lng, "max": max_lng, "center": center_lng or (min_lng + max_lng) / 2}
                }
            }

    except Exception as e:
        print(f"❌ Location extraction failed: {e}")

    # Fallback: Try Google Geocoding API if Gemini fails/refuses
    print(f"🔄 Attempting fallback geocoding for: {location_name}")
    try:
        google_places_key = get_secret_func('google-places-key')
        if google_places_key:
            geocode_url = f"https://maps.googleapis.com/maps/api/geocode/json?address={location_name}&key={google_places_key}"
            response = requests.get(geocode_url, timeout=10)
            data = response.json()

            if data.get('status') == 'OK' and len(data.get('results', [])) > 0:
                result = data['results'][0]
                location = result['geometry']['location']
                bounds = result['geometry'].get('viewport', result['geometry'].get('bounds'))

                center_lat = location['lat']
                center_lng = location['lng']

                # Create a focused bounding box (0.05 degrees ~5-6km)
                buffer = 0.05
                min_lat = center_lat - buffer
                max_lat = center_lat + buffer
                min_lng = center_lng - buffer
                max_lng = center_lng + buffer

                print(f"✅ Geocoding fallback SUCCESS: {location_name} → ({center_lat:.4f}, {center_lng:.4f})")

                return {
                    "location": {
                        "name": location_name,
                        "latitude": {"min": min_lat, "max": max_lat, "center": center_lat},
                        "longitude": {"min": min_lng, "max": max_lng, "center": center_lng}
                    }
                }
    except Exception as geo_error:
        print(f"⚠️ Geocoding fallback also failed: {geo_error}")

    # If all methods failed
    print(f"❌ ALL geocoding methods failed for '{location_name}'")
    raise HTTPException(
        status_code=400,
        detail=f"Could not find coordinates for '{location_name}'. This may be a typo or an uncommon location. Please use /chat/start endpoint to clarify the exact location, or try a more specific location name (e.g., city name, country)."
    )
