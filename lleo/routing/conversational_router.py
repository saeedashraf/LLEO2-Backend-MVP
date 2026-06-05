"""
Conversational Smart Router

Enhanced router that supports multi-turn conversations for clarification
"""

import json
import re
from datetime import datetime
from typing import Dict, Any, Optional
from fastapi import HTTPException

from .conversation_manager import (
    ConversationState,
    get_parameter_options,
    parse_parameter_selection,
    format_parameter_question
)


async def analyze_query_with_conversation(
    query: str,
    model,
    conversation_state: Optional[ConversationState] = None
) -> Dict[str, Any]:
    """
    Analyze query with conversational clarification support

    Args:
        query: User's query or response
        model: Vertex AI Gemini model
        conversation_state: Optional existing conversation state

    Returns:
        dict with routing result or clarification request
    """
    if not model:
        raise HTTPException(status_code=500, detail="Vertex AI model not initialized")

    print(f"🧠 CONVERSATIONAL ROUTING")
    print(f"📝 Query: '{query}'")
    if conversation_state:
        print(f"💬 Continuing conversation: {conversation_state.session_id}")

    # Build conversation context
    conversation_history = ""
    if conversation_state and conversation_state.messages:
        conversation_history = "\n".join([
            f"{msg['role']}: {msg['content']}"
            for msg in conversation_state.messages[-6:]  # Last 6 messages for context (increased from 4)
        ])

    # Prepare prompt with conversation context
    prompt = f"""
    **Role:** You are an expert geospatial analyst specializing in satellite imagery interpretation, remote sensing, and Earth observation data analysis. You assist users with location-based environmental monitoring, land use analysis, and geospatial intelligence.

    **User's Current Message:** "{query}"

    {"**Conversation History (Previous Q&A Thread):**" if conversation_history else ""}
    {conversation_history}

    {"**Previously Extracted Information from This Conversation:**" if conversation_state else ""}
    {f"- Analysis Type: {conversation_state.analysis_type}" if conversation_state and conversation_state.analysis_type else ""}
    {f"- Location: {conversation_state.location}" if conversation_state and conversation_state.location else ""}
    {f"- Time Period: {conversation_state.start_year} to {conversation_state.end_year}" if conversation_state and conversation_state.start_year else ""}
    {f"- Parameters: {conversation_state.parameters}" if conversation_state and conversation_state.parameters else ""}

    **🔒 CRITICAL RULE - STATE PRESERVATION:**
    When Previously Extracted Information shows an analysis_type (like "air_quality", "vegetation_index", "water_quality"):
    - You are in CLARIFICATION MODE for that specific analysis type
    - The user is answering YOUR clarification questions
    - YOU MUST keep the same analysis_type in your response
    - YOU MUST NOT change analysis_type to null or "general_question"
    - YOU MUST NOT start a new analysis unless user explicitly requests it

    Example of CORRECT behavior:
    - Previous: analysis_type="air_quality", asking for parameters
    - User says: "CO"
    - Your response MUST have: analysis_type="air_quality" (NOT null, NOT general_question)

    Example of WRONG behavior (DO NOT DO THIS):
    - Previous: analysis_type="air_quality"
    - User says: "last 3 months"
    - Your response: analysis_type="general_question" ❌ WRONG! Should stay "air_quality"

    **Analysis Types Available (Your Geospatial Expertise):**
    1. **vegetation_index** - Vegetation/Forest/Agriculture Analysis (NDVI satellite data)
    2. **air_quality** - Air Pollution Analysis (NO2, CO, SO2, Aerosol from satellite)
    3. **water_quality** - Water Body Analysis (NDWI, Turbidity, Chlorophyll from satellite)
    4. **business_location** - Business/POI Search (Google Places API - NO satellite needed)
    5. **weather_forecast** - Weather Forecast (OpenWeather API - NO satellite needed)
    6. **video_animation** - Visual Time-Lapse Animation (satellite imagery over time)
    7. **general_question** - General questions, greetings, explanations (NO analysis needed)

    **Your Task:**
    1. **First, determine if this needs analysis or is just a question:**
       - Greetings ("hi", "hello") → general_question
       - Info questions ("what is NDVI?", "how does this work?") → general_question
       - Factual questions ("address of Hotel X", "capital of France") → general_question
       - Casual chat ("thanks", "great") → general_question
       - Questions about specific businesses WITHOUT search intent → general_question

    2. **If it needs SEARCH/ANALYSIS, determine:**
       - Analysis type (vegetation_index, air_quality, water_quality, business_location, weather_forecast, video_animation)
       - Location (city, region, coordinates)
       - Time period (for satellite analyses: vegetation, air, water)
       - Parameters (for water/air quality)

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
      2. Inform the user in next_question / clarification message, e.g.:
         "Sentinel-2 data is only available from June 2015 onwards, so I've adjusted your start year to 2015."
      3. Still set status="complete" (don't block the analysis, just adjust and inform).
    - Maximum time span is 10 years for any satellite analysis.
    - If end_year is in the future beyond the current year, cap it at the current year ({datetime.now().year}).

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

    **CRITICAL - Distinguish between factual questions and search requests:**
    - "What is the address of Hotel Beige in Paris?" → general_question (factual, use your knowledge)
    - "Find hotels in Paris" → business_location (search request, needs Google Places)
    - "Show me restaurants near Eiffel Tower" → business_location (search request)
    - "What's the phone number of Cafe X?" → general_question (factual, use your knowledge)

    **Important Rules:**
    - **General questions:** Set analysis_type="general_question", status="complete", NO analysis/search needed
    - **Satellite analyses** (vegetation, air, water): Need location AND time period
    - **Business/Weather searches**: Need location only (no time period), use when user asks to "find" or "show" multiple places
    - **Water/Air quality**: Also ask which parameters user wants (unless they say "all")
    - **Storage location:** All results save to gs://ndvi3-analysis-storage-bucket/analysis_results/{{session_id}}/

    **CRITICAL - Follow-up vs NEW Analysis Detection:**

    **NEW ANALYSIS** (user wants DIFFERENT analysis type):
    - If user asks for DIFFERENT analysis type than previous → This is NEW analysis, NOT a follow-up
    - Examples:
      * Previous: "coffee shops in Biel" → Current: "weather forecast there" → NEW analysis (different type)
      * Previous: "vegetation in Zurich" → Current: "air quality there" → NEW analysis (different type)
      * Previous: "weather in Paris" → Current: "show me restaurants there" → NEW analysis (different type)
    - For NEW analysis: status="complete", analysis_type="the_new_type", NO detail_request
    - REUSE location from conversation if user says "there", "same place", "that location"
    - REUSE time period from conversation ONLY if appropriate for the new analysis type

    **INFORMATIONAL FOLLOW-UP** (user wants MORE DETAILS about SAME analysis):
    - If user asks for MORE DETAILS about SAME analysis type → INFORMATIONAL FOLLOW-UP
    - Examples: "give me addresses", "show ratings", "phone numbers", "give me 5 of them", "more details", "show me the top 10"
    - For follow-ups: status="complete", set extracted_info.detail_request="what they want"
    - CRITICAL: Also set extracted_info.count=N if user asks for top N items
    - DO NOT ask clarification on follow-ups
    - DO NOT trigger new analysis - Backend will answer from CACHED results instantly
    - Keep analysis_type same as previous analysis

    **IMPORTANT - Location/Time/Parameter Reuse:**
    - If location not mentioned but conversation has location → REUSE previous location
    - If user says "there", "same place", "that location" → REUSE previous location
    - For weather_forecast: NO time period needed (it's always current/forecast)
    - For business_location: NO time period needed
    - For vegetation/air/water: Reuse time period from conversation if not specified
    - If unclear, ask user to clarify

    **Response Format (JSON only):**
    {{
        "status": "complete|needs_clarification|processing_response",
        "analysis_type": "type or null",
        "location": "location name or null",
        "start_year": integer or null,
        "end_year": integer or null,
        "specific_months": [list of month numbers] or null,
        "confidence": "high|medium|low",
        "needs_clarification": ["list of what needs clarification"],
        "clarification_type": "parameters|location|time_period|multiple",
        "extracted_info": {{
            "detail_request": "CRITICAL: Set to 'addresses'/'ratings'/'details' etc if this is a follow-up asking for more info from previous analysis",
            "any_other_extracted_info": "value"
        }},
        "reasoning": "explanation of your analysis",
        "next_question": "what to ask next if clarification needed"
    }}

    **Examples Covering All Domains:**

    **Example 1 - General Question (NO analysis):**
    Query: "Hi, what can you do?"
    {{
        "status": "complete",
        "analysis_type": "general_question",
        "location": null,
        "confidence": "high",
        "needs_clarification": [],
        "reasoning": "This is a greeting and general question. No analysis needed, just conversational response."
    }}

    **Example 1b - Factual Question (NO analysis):**
    Query: "What is the address of Hotel Beige in Paris?"
    {{
        "status": "complete",
        "analysis_type": "general_question",
        "location": null,
        "confidence": "high",
        "needs_clarification": [],
        "reasoning": "Factual question about a specific business. Answer using existing knowledge, no search/analysis needed."
    }}

    **Example 2 - Vegetation Analysis (NDVI):**
    Query: "show me deforestation in Amazon from 2020 to 2024"
    {{
        "status": "complete",
        "analysis_type": "vegetation_index",
        "location": "Amazon Rainforest, Brazil",
        "start_year": 2020,
        "end_year": 2024,
        "confidence": "high",
        "needs_clarification": [],
        "reasoning": "Complete vegetation query with location and time period"
    }}

    **Example 3 - Water Quality (needs clarification):**
    Query: "water quality in Lake Geneva"
    {{
        "status": "needs_clarification",
        "analysis_type": "water_quality",
        "location": "Lake Geneva, Switzerland",
        "confidence": "medium",
        "needs_clarification": ["parameters", "time_period"],
        "clarification_type": "parameters",
        "reasoning": "Water quality requested but missing parameters and time period",
        "next_question": "parameters"
    }}

    **Example 4 - Air Quality (complete):**
    Query: "analyze air pollution in Los Angeles 2023"
    {{
        "status": "needs_clarification",
        "analysis_type": "air_quality",
        "location": "Los Angeles, CA, USA",
        "start_year": 2023,
        "end_year": 2023,
        "confidence": "high",
        "needs_clarification": ["parameters"],
        "clarification_type": "parameters",
        "reasoning": "Air quality with location and year, but need to ask which parameters (NO2, CO, SO2, Aerosol)"
    }}

    **Example 5 - Weather Forecast (NO time period needed):**
    Query: "weather forecast for Tokyo"
    {{
        "status": "complete",
        "analysis_type": "weather_forecast",
        "location": "Tokyo, Japan",
        "confidence": "high",
        "needs_clarification": [],
        "reasoning": "Weather forecast only needs location, no time period required"
    }}

    **Example 6 - Business Location (NO time period):**
    Query: "find coffee shops in Paris"
    {{
        "status": "complete",
        "analysis_type": "business_location",
        "location": "Paris, France",
        "confidence": "high",
        "needs_clarification": [],
        "reasoning": "Business search needs only location"
    }}

    **Example 7 - Follow-up Request (addresses/ratings):**
    Previous: "coffee shops in Paris" (completed)
    Query: "give me the addresses and ratings"
    {{
        "status": "complete",
        "analysis_type": "business_location",
        "location": "Paris, France",
        "confidence": "high",
        "needs_clarification": [],
        "extracted_info": {{"detail_request": "addresses_and_ratings"}},
        "reasoning": "Follow-up for more details, reuse previous search, NO new analysis needed"
    }}

    **Example 8 - Follow-up Request (top N):**
    Previous: "find coffee shops near Bern" (completed)
    Query: "give me the address of five of them"
    {{
        "status": "complete",
        "analysis_type": "business_location",
        "location": "Bern, Switzerland",
        "confidence": "high",
        "needs_clarification": [],
        "extracted_info": {{
            "detail_request": "addresses",
            "count": 5
        }},
        "reasoning": "Follow-up asking for addresses of top 5 results from previous search. DO NOT re-run analysis, use cached results."
    }}

    **Example 8b - NEW Analysis (different type, reuse location):**
    Previous: "coffee shops in Biel" (completed, type=business_location)
    Query: "show me weather forecast there"
    {{
        "status": "complete",
        "analysis_type": "weather_forecast",
        "location": "Biel, Switzerland",
        "confidence": "high",
        "needs_clarification": [],
        "reasoning": "User wants DIFFERENT analysis type (weather vs business). This is NEW analysis. Reuse location 'Biel' from conversation. NO detail_request field."
    }}

    **Example 8c - NEW Analysis (different type, same location explicitly):**
    Previous: "vegetation index in Zurich 2020-2023" (completed, type=vegetation_index)
    Query: "air quality in same place 2023"
    {{
        "status": "needs_clarification",
        "analysis_type": "air_quality",
        "location": "Zurich, Switzerland",
        "start_year": 2023,
        "end_year": 2023,
        "confidence": "high",
        "needs_clarification": ["parameters"],
        "clarification_type": "parameters",
        "reasoning": "User wants DIFFERENT analysis type (air_quality vs vegetation). Reused location from conversation. Has time period. Need to ask which air quality parameters."
    }}

    **Example 8 - Parameter Selection Response:**
    Previous: Asked about water quality parameters
    Query: "all of them"
    {{
        "status": "processing_response",
        "analysis_type": "water_quality",
        "location": "Lake Geneva, Switzerland",
        "needs_clarification": ["time_period"],
        "clarification_type": "time_period",
        "extracted_info": {{"parameters": ["ndwi", "turbidity", "chlorophyll"]}},
        "reasoning": "User selected all parameters, still need time period",
        "next_question": "time_period"
    }}

    **Return ONLY valid JSON.**
    """

    try:
        response = model.generate_content(prompt, generation_config={
            "max_output_tokens": 8192,
            "temperature": 0.2
        })

        text = response.text.strip()
        print(f"🧠 Conversational Routing Response: {text[:500]}...")

        # Extract JSON
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            json_text = json_match.group(0)
            result = json.loads(json_text)

            print(f"📊 ROUTING RESULT:")
            print(f"   Status: {result.get('status')}")
            print(f"   Analysis Type: {result.get('analysis_type')}")
            print(f"   Location: {result.get('location')}")
            print(f"   Needs Clarification: {result.get('needs_clarification')}")
            print(f"   Next Question: {result.get('next_question')}")

            return result

    except Exception as e:
        print(f"❌ Conversational routing failed: {e}")
        import traceback
        print(f"❌ Traceback: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Conversational routing failed: {str(e)}")


def generate_clarification_message(
    routing_result: Dict[str, Any],
    conversation_state: Optional[ConversationState] = None
) -> str:
    """
    Generate a friendly clarification message based on what information is missing

    Args:
        routing_result: Result from analyze_query_with_conversation
        conversation_state: Current conversation state

    Returns:
        Clarification message for the user
    """
    needs_clarification = routing_result.get('needs_clarification', [])
    clarification_type = routing_result.get('clarification_type')
    analysis_type = routing_result.get('analysis_type')

    # If we need to ask about parameters
    if clarification_type == 'parameters' or 'parameters' in needs_clarification:
        if analysis_type in ['water_quality', 'air_quality']:
            return format_parameter_question(analysis_type)

    # If we need time period
    if clarification_type == 'time_period' or 'time_period' in needs_clarification:
        location_text = f" for {routing_result.get('location', 'this location')}" if routing_result.get('location') else ""
        return f"""📅 **Time Period Needed**

What time period would you like to analyze{location_text}?

You can specify:
- A year range (e.g., "2020 to 2024")
- A specific year (e.g., "2023")
- Specific months (e.g., "January to March 2024")
- Recent data (e.g., "last 3 months")

Please provide the time period:"""

    # If we need location
    if clarification_type == 'location' or 'location' in needs_clarification:
        analysis_name = {
            'water_quality': 'water quality analysis',
            'air_quality': 'air quality analysis',
            'vegetation_index': 'vegetation analysis',
            'weather_forecast': 'weather forecast',
            'business_location': 'business search'
        }.get(analysis_type, 'analysis')

        return f"""📍 **Location Needed**

Which location would you like to analyze for {analysis_name}?

Please provide:
- City name (e.g., "Paris")
- Lake/river name (e.g., "Lake Geneva")
- Region (e.g., "Amazon Rainforest")
- Coordinates (e.g., "47.3769°N, 8.5417°E")

Please specify the location:"""

    # Multiple items need clarification
    if clarification_type == 'multiple':
        # Start with parameters if it's one of the items
        if 'parameters' in needs_clarification and analysis_type in ['water_quality', 'air_quality']:
            return format_parameter_question(analysis_type)
        # Otherwise ask for location first
        elif 'location' in needs_clarification:
            return generate_clarification_message(
                {**routing_result, 'clarification_type': 'location'},
                conversation_state
            )
        # Then time period
        elif 'time_period' in needs_clarification:
            return generate_clarification_message(
                {**routing_result, 'clarification_type': 'time_period'},
                conversation_state
            )

    # Default fallback
    return f"""I need more information to help you.

What's missing: {', '.join(needs_clarification)}

Please provide the missing information so I can proceed with your analysis."""
