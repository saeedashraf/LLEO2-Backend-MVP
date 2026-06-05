"""
Smart Query Router using Vertex AI Gemini
Single-step intelligent routing that extracts location, time, and analysis type
"""

import json
import re
from datetime import datetime
from fastapi import HTTPException


async def smart_route_query(query: str, model) -> dict:
    """
    Single-step intelligent routing that combines extraction and analysis type selection.
    Gemini decides everything in one call: location, time, and best analysis type.

    Args:
        query: User's natural language query
        model: Vertex AI Gemini model instance

    Returns:
        dict: Routing result with status, analysis_type, location, time_period
    """
    if not model:
        raise HTTPException(status_code=500, detail="Vertex AI model not initialized")

    print(f"🧠 SMART ROUTING - Single-step analysis")
    print(f"📝 Query: '{query}'")

    prompt = f"""
    **Role:** You are an intelligent satellite imagery analysis router with deep understanding of Earth observation.

    **User Query:** "{query}"

    **Your Task:** Analyze this query and determine:
    1. What location the user wants to analyze (if clear enough)
    2. What time period they're interested in (if specified)
    3. What type of analysis is most appropriate based on their intent
    4. Whether you have enough information to proceed

    **Analysis Types Available:**

    1. **vegetation_index** - Vegetation/Forest Analysis (NDVI Index)
       - Use for: deforestation, forest health, vegetation changes, crop monitoring, greenness, agricultural analysis
       - Measures: Normalized Difference Vegetation Index to quantify vegetation
       - Example: "deforestation in Amazon 2020-2024" → Vegetation Index shows vegetation loss quantitatively
       - Example: "forest health in Golestan" → Vegetation Index measures forest vitality

    2. **video_animation** - Visual Time-Lapse Animation
       - Use for: urban development, glacier melting, construction progress, coastal changes, visual development
       - Creates: Animated GIF showing visual changes over time
       - Example: "show me how Dubai changed" → Animation shows urban expansion visually
       - Example: "glacier retreat in Alps" → Animation shows ice melting over time

    3. **air_quality** - Air Pollution Analysis (Sentinel-5P)
       - Use for: air pollution, smog, emissions, NO2, PM2.5, AQI, atmospheric quality
       - Measures: Air pollutant concentrations
       - Example: "air quality in Tehran" → Shows NO2, CO, aerosol levels

    4. **water_quality** - Water Body Analysis (Sentinel-2)
       - Use for: lake/river/ocean water quality, turbidity, chlorophyll, water pollution
       - Measures: Water clarity, sediment, algae blooms
       - Example: "water quality of Lake Geneva" → Shows turbidity and water health

    5. **business_location** - Business/POI Search (Google Places API)
       - Use for: finding restaurants, cafes, shops, hotels, businesses, points of interest
       - Searches: Google Places database for specific business types
       - Example: "find coffee shops in Zurich" → Lists nearby coffee shops
       - Example: "restaurants near Paris" → Shows restaurants in the area
       - **IMPORTANT**: When choosing business_location, also extract:
         * business_type: general category (cafe, restaurant, hotel, university, hospital, shop, etc.)
         * keyword: specific keyword/brand (coffee, pizza, Starbucks, etc.)
         * radius: search radius in meters (default: 2000)

    6. **weather_forecast** - Weather Forecast (OpenWeather API)
       - Use for: weather, forecast, temperature, rain, climate, conditions
       - Provides: Current weather and 5-day forecast
       - Example: "weather in London" → Shows current weather and forecast
       - Example: "will it rain in Tokyo tomorrow?" → Shows precipitation forecast

    **CRITICAL ROUTING RULES:**
    - "deforestation" → **vegetation_index** (NOT animation - Vegetation Index quantifies vegetation loss)
    - "forest changes" OR "forest loss" → **vegetation_index** (measures vegetation decrease)
    - "vegetation monitoring" → **vegetation_index**
    - "urban development" OR "city growth" → **video_animation** (shows building expansion)
    - "glacier melting" OR "ice retreat" → **video_animation** (shows ice changes visually)
    - "find restaurants/cafes/shops/hotels" → **business_location** (POI search)
    - "where to eat/stay/shop" → **business_location** (business search)
    - "weather/forecast/temperature/rain" → **weather_forecast** (weather info)
    - "will it rain/snow" → **weather_forecast** (precipitation forecast)
    - "what is the [LOCATION] water quality" OR "how is [LOCATION] water" → **water_quality** (analyze water, NOT definition)
    - "what is the [LOCATION] air quality" → **air_quality** (analyze air pollution, NOT definition)
    - Any query about VEGETATION/FORESTS/CROPS → **vegetation_index** (even with time series)
    - Any query about BUILDINGS/CITIES/CONSTRUCTION → **video_animation**
    - Any query about FINDING BUSINESSES/POIs → **business_location**
    - Any query about WEATHER/CLIMATE/FORECAST → **weather_forecast**
    - Any query about WATER QUALITY of a SPECIFIC LOCATION → **water_quality** (NOT informational)
    - Any query about AIR QUALITY of a SPECIFIC LOCATION → **air_quality** (NOT informational)

    **🛰️ CRITICAL — Satellite Data Availability Windows:**
    Our analyses use specific satellite missions. Data DOES NOT exist before these dates:
    | Analysis Type       | Satellite / Source   | Earliest Data |
    |---------------------|----------------------|---------------|
    | vegetation_index    | Sentinel-2 (ESA)     | **June 2015** |
    | water_quality       | Sentinel-2 (ESA)     | **June 2015** |
    | air_quality         | Sentinel-5P (ESA)    | **April 2018** |
    | video_animation     | Sentinel-2 (ESA)     | **June 2015** |
    | weather_forecast    | OpenWeather API      | Current only (no historical) |
    | business_location   | Google Places API    | Current only  |

    **Rules for time period:**
    - If user requests a start_year BEFORE the satellite's earliest date, you MUST:
      1. Set start_year to the satellite's earliest year (2015 or 2018).
      2. Mention the adjustment in the "message" field, e.g.:
         "Sentinel-2 data is only available from June 2015, so I've adjusted your start year to 2015."
      3. Still set status="complete" (don't block — just adjust and inform).
    - Maximum time span is 10 years for any satellite analysis.
    - If end_year > {datetime.now().year}, cap it at {datetime.now().year}.

    **⚠️ CRITICAL — Time-Span & Area-Size Warnings (MUST FOLLOW):**
    Long time spans and large geographic areas make satellite processing very slow or cause timeouts.
    You MUST warn users and suggest refinements in these cases:

    **Time-span guidelines:**
    - **≤ 3 years**: ✅ Good — fast processing, no warning needed.
    - **3–5 years**: ⚠️ OK but mention it may take longer. Proceed with status="complete".
    - **> 5 years**: 🛑 WARN the user. Set status="needs_clarification" and suggest a shorter period.
      Example message: "Analyzing 10 years of satellite data can be very slow or may time out. I'd recommend splitting into 3-year windows (e.g. 2015–2018, then 2018–2021). Would you like to proceed with a shorter range, or should I try the full period?"
    - **> 8 years**: 🛑 STRONGLY discourage. Suggest splitting into 2–3 separate analyses.

    **Area-size guidelines (applies to satellite analyses: vegetation, air, water, video):**
    - **City / town / small district**: ✅ Good — proceed normally.
    - **Large province / state** (e.g. "East Kalimantan", "California", "Golestan Province"): ⚠️ Warn the user that province-level areas are very large and may time out. Suggest a specific city or district within the province.
      Example: "East Kalimantan is a very large province (~130,000 km²). For best results, could you specify a particular city or district? For example: 'Samarinda' or 'Kembang Janggut sub-district'."
    - **Entire country** (e.g. "Indonesia", "Brazil", "India"): 🛑 Set status="needs_clarification". Country-level satellite analysis is not supported — too large. Ask for a specific city/region.
      Example: "Country-level satellite analysis is not feasible — Indonesia covers over 1.9 million km². Please specify a city or district, e.g. 'Jakarta' or 'Bali'."

    **Combined risk:** If BOTH the time span is long AND the area is large, be extra cautious.
    Example: "30 years of data for all of Kalimantan" → strongly recommend narrowing BOTH dimensions.

    **Confidence Assessment:**
    - **high**: Location is specific (city name/landmark/coordinates), time is clear, intent is unambiguous
    - **medium**: Location is recognizable but might have typos, time is implied or partially specified
    - **low**: Location is too vague/ambiguous, time is completely missing, or multiple interpretations possible

    **Decision Logic:**
    - If confidence is HIGH or MEDIUM for both location AND time → status: "complete"
    - If location is missing/unclear OR time is missing when required → status: "needs_clarification"

    **Response Format (JSON only):**
    {{
        "status": "complete|needs_clarification",
        "confidence": "high|medium|low",
        "analysis_type": "vegetation_index|air_quality|water_quality|video_animation|business_location|weather_forecast",
        "location": "extracted location name (or null if unclear)",
        "time_period": "extracted time period like '2021 to 2025' or 'current' (or null if not specified)",
        "start_year": integer year or null,
        "end_year": integer year or null,
        "reasoning": "detailed explanation of why you chose this analysis type",
        "clarification_needed": "what specific information is missing (if status=needs_clarification)",
        "message": "friendly conversational message to user (if needs clarification)",
        "business_type": "ONLY for business_location: general category like 'cafe', 'restaurant', 'hotel', 'university', 'hospital', 'shop', etc. (null for other types)",
        "keyword": "ONLY for business_location: specific keyword or brand like 'coffee', 'pizza', 'Starbucks' (null for other types)",
        "radius": "ONLY for business_location: search radius in meters, default 2000 (null for other types)"
    }}

    **Examples:**

    Query: "deforestation analysis of forests in golestan iran from 2021 to 2025"
    Response:
    {{
        "status": "complete",
        "confidence": "high",
        "analysis_type": "vegetation_index",
        "location": "Golestan Province, Iran",
        "time_period": "2021 to 2025",
        "start_year": 2021,
        "end_year": 2025,
        "reasoning": "Deforestation analysis requires Vegetation Index (NDVI) to quantitatively measure vegetation loss over time. This is NOT a visual animation request - it's about measuring forest health decline. The query specifies clear location (Golestan, Iran) and time period (2021-2025). Vegetation Index will show vegetation index changes indicating deforestation.",
        "clarification_needed": null,
        "message": null,
        "business_type": null,
        "keyword": null,
        "radius": null
    }}

    Query: "show me changes in Dubai from 2015 to 2023"
    Response:
    {{
        "status": "complete",
        "confidence": "high",
        "analysis_type": "video_animation",
        "location": "Dubai, United Arab Emirates",
        "time_period": "2015 to 2023",
        "start_year": 2015,
        "end_year": 2023,
        "reasoning": "User wants to see visual changes in an urban area (Dubai) over time. This is about urban development and construction, not vegetation. Video animation will create a time-lapse showing building growth, land reclamation, and infrastructure expansion.",
        "clarification_needed": null,
        "message": null
    }}

    Query: "air pollution in Tehran today"
    Response:
    {{
        "status": "complete",
        "confidence": "high",
        "analysis_type": "air_quality",
        "location": "Tehran, Iran",
        "time_period": "current",
        "start_year": 2025,
        "end_year": 2025,
        "reasoning": "Query explicitly requests air pollution analysis for a specific city (Tehran) with current/recent data. This requires Sentinel-5P atmospheric data to measure pollutant concentrations.",
        "clarification_needed": null,
        "message": null
    }}

    Query: "show me forest changes"
    Response:
    {{
        "status": "needs_clarification",
        "confidence": "low",
        "analysis_type": "vegetation_index",
        "location": null,
        "time_period": null,
        "start_year": null,
        "end_year": null,
        "reasoning": "Forest changes analysis would use Vegetation Index to measure vegetation health over time, but both location and time period are missing.",
        "clarification_needed": "specific forest location and time period",
        "message": "I'd be happy to help analyze forest changes! 🌲 Which specific forest or region would you like to monitor? And what time period are you interested in? (e.g., 'Amazon rainforest from 2020 to 2024')"
    }}

    Query: "water quality of Lake grefense from january 2024 to November 2024"
    Response:
    {{
        "status": "complete",
        "confidence": "medium",
        "analysis_type": "water_quality",
        "location": "Lake Greifensee",
        "time_period": "January 2024 to November 2024",
        "start_year": 2024,
        "end_year": 2024,
        "reasoning": "Query asks for water quality analysis of a lake (likely 'Greifensee' with typo) over a specific time period. Water quality analysis will measure turbidity, chlorophyll, and water clarity using Sentinel-2 data. The location has a minor typo but is recognizable, and time period is clear.",
        "clarification_needed": null,
        "message": null
    }}

    Query: "find coffee shops in Zurich"
    Response:
    {{
        "status": "complete",
        "confidence": "high",
        "analysis_type": "business_location",
        "location": "Zurich, Switzerland",
        "time_period": "current",
        "start_year": 2025,
        "end_year": 2025,
        "reasoning": "User is searching for specific businesses (coffee shops) in a specific location (Zurich). This is a Point of Interest (POI) search that requires the business_location analysis type, which uses Google Places API to find nearby businesses.",
        "clarification_needed": null,
        "message": null,
        "business_type": "cafe",
        "keyword": "coffee",
        "radius": 2000
    }}

    Query: "What is the Bow River quality"
    Response:
    {{
        "status": "complete",
        "confidence": "high",
        "analysis_type": "water_quality",
        "location": "Bow River, Calgary, Canada",
        "time_period": "current",
        "start_year": 2025,
        "end_year": 2025,
        "reasoning": "This is a request to ANALYZE water quality of a specific water body (Bow River), NOT a request for a definition. The user wants to know the actual water quality conditions, which requires satellite analysis using water_quality type to measure turbidity, chlorophyll, and water clarity.",
        "clarification_needed": null,
        "message": null
    }}

    Query: "weather in London today"
    Response:
    {{
        "status": "complete",
        "confidence": "high",
        "analysis_type": "weather_forecast",
        "location": "London, United Kingdom",
        "time_period": "current",
        "start_year": 2025,
        "end_year": 2025,
        "reasoning": "User wants current weather information for London. This requires the weather_forecast analysis type, which uses OpenWeather API to provide current conditions and forecast.",
        "clarification_needed": null,
        "message": null
    }}

    **IMPORTANT:**
    - Return ONLY valid JSON
    - For deforestation/vegetation queries → ALWAYS choose "vegetation_index" (NOT video_animation)
    - For urban/glacier/construction queries → choose "video_animation"
    - Extract start_year and end_year as integers when time period is specified
    - If time is "current" or "today", set start_year and end_year to 2025
    - Be generous with "medium" confidence if location is recognizable despite typos
    - Today's date for reference: 2025-10-28
    """

    try:
        response = model.generate_content(prompt, generation_config={
            "max_output_tokens": 8192,
            "temperature": 0.2  # Lower temperature for more consistent routing
        })

        text = response.text.strip()
        print(f"🧠 Smart Routing Response: {text}")

        # Check if response looks truncated
        if text.count('{') != text.count('}'):
            print(f"⚠️ WARNING: Response truncated! Braces: {{ {text.count('{')} }} {text.count('}')}")

        json_match = re.search(r'{[\s\S]*}', text)
        if json_match:
            json_text = json_match.group(0)
            print(f"🔍 DEBUG: Smart routing JSON length: {len(json_text)} chars")
            result = json.loads(json_text)

            print(f"📊 SMART ROUTING RESULT:")
            print(f"   Status: {result.get('status')}")
            print(f"   Confidence: {result.get('confidence')}")
            print(f"   Analysis Type: {result.get('analysis_type')}")
            print(f"   Location: {result.get('location')}")
            print(f"   Time Period: {result.get('time_period')}")
            print(f"   Years: {result.get('start_year')} - {result.get('end_year')}")
            print(f"   Reasoning: {result.get('reasoning')}")

            return result

    except Exception as e:
        print(f"❌ Smart routing failed: {e}")
        import traceback
        print(f"❌ Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Smart routing failed: {str(e)}")
