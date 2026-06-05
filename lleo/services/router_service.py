"""
LLEO v2 — Single-call LLM router.

ONE Gemini call extracts intent, location, coordinates, time range,
analysis-specific parameters, and clarification needs.

The LLM either fills everything it can infer → confidence "high",
or it sets needs_clarification + clarification_message to ask the user.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Optional

from ..models.routing import RoutingDecision, AnalysisIntent, Confidence

# ────────────────────────────────────────────────────────────────────────────
# System prompt — tells the LLM what it is and how to think
# ────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = f"""\
You are **LLEO**, an expert geospatial analyst.
Today's date is **{date.today().isoformat()}** (year {date.today().year}).

Your ONLY job is to read a user's message and return ONE JSON object.

### DECISION LOGIC

1. Read the user message and decide which **intent** best fits (see list below).
   Use your understanding of the request — do NOT rely on keyword matching.
   The user may describe what they want in any phrasing.

2. **Location**: Use your knowledge to fill location_name, latitude, longitude.
   You know coordinates for every city, neighborhood, farm, landmark, and region.
   When the user mentions a specific neighborhood or sub-area (e.g. "Yosef Abad, Tehran"),
   use the coordinates of THAT area, not the city center.
   Include the full location context: "Yosef Abad, Tehran, Iran".

3. **Time range**:
   - If no time range mentioned → default to current year ({date.today().year}).
   - "last 3 years" → start_year={date.today().year - 3}, end_year={date.today().year}.
   - For vegetation / NDVI "change" queries with a single year (e.g. "in 2024"):
     the user wants to see change, so set start_year = that_year - 1, end_year = that_year.
   - If the user gives an explicit range ("between 2020 and 2024"), use as-is.
   - For air quality or weather, a single year with start_year = end_year is fine.
   - **Months matter** — when the user mentions specific months (e.g.
     "March 2025 to June 2025", "Jan 2024 till August 2024", "from May to October"),
     set start_month and end_month accordingly (3-6, 1-8, 5-10). Do NOT leave them null
     when the user explicitly says month names. This is especially important for
     video_animation requests where the month range bounds the timelapse.

4. Fill any analysis-specific parameters (pollutants, water_indicators, etc.).

5. If you have EVERYTHING needed → confidence = "high", needs_clarification = [].

6. If something critical is MISSING (e.g. no location) → confidence = "low",
   needs_clarification = [...], clarification_message = "friendly question to the user".

### INTENT VALUES (pick exactly one)
- vegetation_index — any vegetation, greenness, NDVI/EVI/SAVI, deforestation, crops, forest health
- air_quality — any air pollution, emissions, smog, specific pollutants
- water_quality — any water body analysis, lake/river quality, turbidity, algae
- weather_forecast — weather, temperature, precipitation, forecast
- business_location — find places, shops, restaurants, services, facilities, factories, plants near a location.
  Use this when users ask to FIND or LOCATE physical establishments (e.g. "water treatment plants",
  "solar farms", "hospitals", "universities"). This is about finding POIs, NOT analyzing satellite data.
- video_animation — time-lapse, temporal animation, urban expansion, change over time
- general_question — greetings, explanations, casual conversation, "what is NDVI?",
  AND any analysis request we do NOT currently support (see below).

### UNSUPPORTED ANALYSIS REQUESTS → use general_question
Our platform currently supports ONLY these analysis types:
  1. Vegetation Index (NDVI, EVI, SAVI, deforestation, crop health)
  2. Air Quality (NO2, SO2, CO, O3, CH4)
  3. Water Quality (NDWI, turbidity, chlorophyll, TSM, CDOM)
  4. Weather Forecast (temperature, precipitation, forecast)
  5. Business / Location Search (find places via Google Places)
  6. Video Animation (time-lapse, urban expansion)

If the user asks about something OUTSIDE these (e.g. snow depth, glacier analysis,
water temperature, soil moisture, flood mapping, land use classification, ocean currents,
wildfire detection, biodiversity hotspots, bathymetry, etc.), you MUST:
  - Set intent = "general_question"
  - Provide a helpful general_answer using your own knowledge about the topic
  - At the END of your general_answer, add:
    "\n\n🔬 Note: [topic] analysis is currently under development on LLEO. "
    "Our available analysis types are: Vegetation Index, Air Quality, Water Quality, "
    "Weather Forecast, Business/Location Search, and Video Animation. "
    "Feel free to ask about any of these!"
  - Do NOT route these to a processor — they will fail.

### PARAMETER SELECTION — ASK before computing
- **air_quality**: Available pollutants: NO2, SO2, CO, O3, CH4.
  If the user did NOT specify which → ask via needs_clarification = ["pollutants"],
  clarification_message = "Which pollutants would you like to analyze?
  Available: NO2 (nitrogen dioxide), SO2 (sulfur dioxide), CO (carbon monoxide),
  O3 (ozone), CH4 (methane), or I can analyze all of them."
  If they said "all" → pollutants = ["NO2", "SO2", "CO", "O3", "CH4"].
  If they picked specific ones → only those.

- **water_quality**: Available indicators: NDWI, turbidity (NDTI), chlorophyll (NDCI), TSM, CDOM.
    If the user did NOT specify which → ask via needs_clarification = ["water_indicators"],
    clarification_message = "Which water quality indicators would you like to analyze?
    Available: NDWI (water extent), Turbidity (water clarity, expressed as NDTI),
    Chlorophyll (algal/plant signal, expressed as NDCI), TSM (total suspended matter proxy),
    and CDOM (colored dissolved organic matter proxy). You can pick any subset or say 'all'"
    If they said "all" → water_indicators = ["NDWI", "turbidity", "chlorophyll", "TSM", "CDOM"].
    If they picked specific ones → only those. Use the indicators' canonical names (NDWI, turbidity, chlorophyll, TSM, CDOM).

- **vegetation_index**: Default to NDVI if not specified (no need to ask).

- **business_location**: Extract the business_type (general category like "restaurant",
    "cafe", "hospital", "water treatment plant", "university", "factory", etc.)
    and keyword (specific brand or refinement like "Starbucks", "organic", "solar", etc.)
    directly from the user's query. Do NOT ask for clarification — infer them.
    If the query mentions a specific thing to find (e.g. "water treatment plants"),
    set business_type to that phrase. If a brand or specific keyword is mentioned,
    put it in keyword.
    radius: for common POIs (restaurants, cafes, shops) use 2000.
    For specialized/rare facilities (factories, treatment plants, power stations) use 5000–10000.

### CONVERSATION CONTEXT
When prior context is provided, KEEP the same intent and merge new info.
Only change intent if the user explicitly asks for something different.
If the user responds with parameter choices (e.g. "NO2 and CO", "all"),
keep the same intent and location, just fill in the parameters.
"""

# ────────────────────────────────────────────────────────────────────────────
# JSON output instruction — appended to every call
# ────────────────────────────────────────────────────────────────────────────

JSON_INSTRUCTION = f"""\
Respond with ONLY a JSON object. No markdown, no explanation, nothing else.

Use exactly these 18 keys (null for unknown, [] for empty arrays):

{{
  "intent": "weather_forecast",
  "confidence": "high",
  "location_name": "Aarhus, Denmark",
  "latitude": 56.1629,
  "longitude": 10.2039,
  "start_year": {date.today().year},
  "end_year": {date.today().year},
  "start_month": null,
  "end_month": null,
  "business_type": null,
  "keyword": null,
  "radius": 2000,
  "pollutants": null,
  "water_indicators": null,
  "vegetation_index_type": null,
  "needs_clarification": [],
  "clarification_message": null,
  "general_answer": null
}}

IMPORTANT:
- ALL 18 keys must be present in your response.
- latitude and longitude: use decimal degrees. You know them for all cities.
- needs_clarification: empty [] when you have everything needed.
- general_answer: fill for general_question intent — this includes both casual questions
  AND unsupported analysis types. For unsupported analyses, answer from your knowledge
  and append the "under development" note with available analysis types.
- Return the COMPLETE JSON object. Do NOT truncate.
"""


# ────────────────────────────────────────────────────────────────────────────
# Main entry point
# ────────────────────────────────────────────────────────────────────────────

async def route_query(
    query: str,
    model=None,
    conversation_context: Optional[str] = None,
    prior_intent: Optional[str] = None,
    prior_fields: Optional[dict] = None,
) -> RoutingDecision:
    """
    ONE Gemini call → RoutingDecision.

    Either returns a fully-populated decision (confidence=high)
    or a clarification request (confidence=low/medium with
    needs_clarification listing the missing fields).
    """
    if model is None:
        from ..config import services
        model = services.gemini_model
    if model is None:
        raise RuntimeError("Gemini model not initialised — cannot route query")

    # ── Build user prompt ──
    parts: list[str] = []

    if conversation_context:
        parts.append(f"**Conversation so far:**\n{conversation_context}\n")

    if prior_intent or prior_fields:
        parts.append("**Previously extracted (keep unless user overrides):**")
        if prior_intent:
            parts.append(f"  intent: {prior_intent}")
        if prior_fields:
            for k, v in prior_fields.items():
                if v is not None:
                    parts.append(f"  {k}: {v}")
        parts.append("")

    parts.append(f'**User says:** "{query}"')
    user_prompt = "\n".join(parts)

    print(f"\n🧠 SINGLE-CALL ROUTER")
    print(f"   Query: {query}")

    try:
        response = model.generate_content(
            [SYSTEM_PROMPT, JSON_INSTRUCTION, user_prompt],
            generation_config={
                "max_output_tokens": 8192,
                "temperature": 0.1,
            },
        )

        # ── Diagnostics ──
        if response.candidates:
            c = response.candidates[0]
            print(f"   finish_reason : {c.finish_reason}")
            if c.safety_ratings:
                blocked = [
                    r for r in c.safety_ratings
                    if r.probability.name not in ("NEGLIGIBLE", "LOW")
                ]
                if blocked:
                    print(f"   ⚠️  safety flags: {blocked}")
        if hasattr(response, "usage_metadata"):
            u = response.usage_metadata
            print(
                f"   tokens — prompt: {u.prompt_token_count}  "
                f"output: {u.candidates_token_count}  "
                f"total: {u.total_token_count}"
            )

        raw = response.text.strip()
        print(f"   Raw length : {len(raw)} chars")
        print(f"   Raw text   : {raw[:1000]}")

        data = _parse_json(raw)
        decision = RoutingDecision.model_validate(data)

        # ── Merge prior fields for /chat/continue ──
        if prior_fields:
            decision = _merge_prior(decision, prior_fields, prior_intent)

        # ── Log result ──
        print(
            f"   ✅ intent={decision.intent.value}  "
            f"conf={decision.confidence.value}"
        )
        print(
            f"      location={decision.location_name}  "
            f"lat={decision.latitude}  lon={decision.longitude}"
        )
        print(
            f"      time={decision.start_year}→{decision.end_year}  "
            f"months={decision.start_month}→{decision.end_month}  "
            f"clarify={decision.needs_clarification}"
        )

        return decision

    except Exception as exc:
        print(f"   ❌ Router error: {exc}")
        import traceback
        traceback.print_exc()
        return RoutingDecision(
            intent=AnalysisIntent.general_question,
            confidence=Confidence.low,
            needs_clarification=["intent", "location_name"],
            clarification_message=(
                "I'm sorry, I couldn't fully understand your request. "
                "Could you tell me what analysis you need and for which location?"
            ),
        )


# ────────────────────────────────────────────────────────────────────────────
# JSON parsing with fallbacks
# ────────────────────────────────────────────────────────────────────────────

def _parse_json(raw: str) -> dict:
    """Parse JSON from Gemini output, with progressive repair strategies."""

    # Strip markdown fences
    text = re.sub(r"^```(?:json)?\s*", "", raw)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()

    # Strategy 1 — direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2 — extract first {...} block
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass

    # Strategy 3 — fix truncation (add missing closing braces/brackets)
    open_b = text.count("{") - text.count("}")
    open_k = text.count("[") - text.count("]")
    patched = text + ("]" * max(0, open_k)) + ("}" * max(0, open_b))
    try:
        return json.loads(patched)
    except json.JSONDecodeError:
        pass

    # Strategy 4 — regex extraction of critical fields from broken JSON
    print(f"   ⚠️  JSON parse failed, extracting fields via regex…")
    data: dict = {}

    for key in (
        "intent", "confidence", "location_name",
        "clarification_message", "general_answer",
    ):
        m2 = re.search(rf'"{key}"\s*:\s*"([^"]*)"', raw)
        if m2:
            data[key] = m2.group(1)

    for key in (
        "latitude", "longitude", "start_year", "end_year",
        "start_month", "end_month", "radius",
    ):
        m2 = re.search(rf'"{key}"\s*:\s*([\d.eE+-]+)', raw)
        if m2:
            val = m2.group(1)
            data[key] = float(val) if "." in val else int(val)

    if data.get("intent"):
        data.setdefault("needs_clarification", [])
        return data

    raise ValueError(
        f"Cannot parse Gemini response ({len(raw)} chars): {raw[:300]}"
    )


# ────────────────────────────────────────────────────────────────────────────
# Prior-field merging (for /chat/continue flows)
# ────────────────────────────────────────────────────────────────────────────

def _merge_prior(
    decision: RoutingDecision,
    prior: dict,
    prior_intent: Optional[str],
) -> RoutingDecision:
    """Keep previously extracted fields when Gemini didn't override them."""

    # Don't lose a real analysis intent
    if (
        prior_intent
        and prior_intent != "general_question"
        and decision.intent == AnalysisIntent.general_question
    ):
        decision.intent = AnalysisIntent(prior_intent)

    FIELDS = [
        "location_name", "latitude", "longitude",
        "start_year", "end_year", "start_month", "end_month",
        "business_type", "keyword",
        "pollutants", "water_indicators", "vegetation_index_type",
    ]
    for field in FIELDS:
        if prior.get(field) is not None and getattr(decision, field, None) is None:
            setattr(decision, field, prior[field])

    return decision
