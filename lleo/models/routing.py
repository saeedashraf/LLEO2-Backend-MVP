"""
Pydantic models for the single-call LLM router.

RoutingDecision is the structured output schema that Gemini fills
in ONE call — extracting intent, location, coordinates, time horizon,
parameters, and clarification needs all at once.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class AnalysisIntent(str, Enum):
    """All analysis types the backend supports."""
    vegetation_index = "vegetation_index"
    air_quality = "air_quality"
    water_quality = "water_quality"
    weather_forecast = "weather_forecast"
    business_location = "business_location"
    video_animation = "video_animation"
    general_question = "general_question"


class Confidence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


def describe_decision(decision: "RoutingDecision", location: Optional[dict] = None,
                      time_period: Optional[dict] = None,
                      parameters: Optional[dict] = None) -> str:
    """Plain-language summary of a routing decision (first reasoning step)."""
    loc = location or {}
    tp = time_period or {}
    params = parameters or {}
    intent = decision.intent.value if hasattr(decision.intent, "value") else str(decision.intent)
    parts = [f"Understood the request as {intent.replace('_', ' ')}"]
    name = loc.get("name") or decision.location_name
    if name:
        lat, lon = loc.get("latitude", decision.latitude), loc.get("longitude", decision.longitude)
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon):
            parts[0] += f" for {name} ({lat:.4f}, {lon:.4f})"
        else:
            parts[0] += f" for {name}"
    parts[0] += "."

    if tp.get("start_date") or tp.get("end_date"):
        parts.append(f"Window: {tp.get('start_date') or 'archive start'} to {tp.get('end_date') or 'today'}.")
    elif tp.get("start_year"):
        sy, ey = tp.get("start_year"), tp.get("end_year") or tp.get("start_year")
        if tp.get("start_month") or tp.get("end_month"):
            parts.append(f"Window: months {tp.get('start_month') or 1} to {tp.get('end_month') or 12}, {sy}"
                         + (f" to {ey}" if ey != sy else "") + ".")
        else:
            parts.append(f"Window: {sy}" + (f" to {ey}" if ey != sy else "") + ".")
    if tp.get("event_date"):
        parts.append(f"Event date: {tp['event_date']}.")
    if tp.get("temporal_resolution"):
        parts.append(f"Requested resolution: {tp['temporal_resolution']}.")
    else:
        parts.append("No resolution requested; the planner will choose one.")
    for key, label in (("water_indicators", "Indicators"), ("pollutants", "Pollutants"),
                       ("vegetation_index_type", "Index"), ("business_type", "Place type")):
        val = params.get(key)
        if val:
            parts.append(f"{label}: {', '.join(val) if isinstance(val, list) else val}.")
    if decision.reasoning:
        parts.append(f"Router reasoning: {decision.reasoning}")
    return " ".join(parts)


class RoutingDecision(BaseModel):
    """
    Single structured output returned by Gemini.

    Every field the old pipeline needed 4-5 LLM calls for
    is now extracted in a single call.
    """

    # --- Intent ---
    intent: AnalysisIntent = Field(
        description="The analysis type to route to."
    )
    confidence: Confidence = Field(
        default=Confidence.medium,
        description="How confident the model is that it has all info."
    )

    # --- Location ---
    location_name: Optional[str] = Field(
        default=None,
        description="Human-readable location name (city, region, landmark, etc.)."
    )
    latitude: Optional[float] = Field(
        default=None,
        description="Latitude of the location center (decimal degrees). "
                    "Provide if you know it, otherwise leave null."
    )
    longitude: Optional[float] = Field(
        default=None,
        description="Longitude of the location center (decimal degrees). "
                    "Provide if you know it, otherwise leave null."
    )

    # --- Time horizon ---
    start_year: Optional[int] = Field(
        default=None,
        description="Start year for temporal analysis."
    )
    end_year: Optional[int] = Field(
        default=None,
        description="End year for temporal analysis."
    )
    start_month: Optional[int] = Field(
        default=None,
        description="Start month (1-12) if user specified a sub-annual range."
    )
    end_month: Optional[int] = Field(
        default=None,
        description="End month (1-12) if user specified a sub-annual range."
    )

    # --- Temporal resolution (how fine the time steps should be) ---
    temporal_resolution: Optional[str] = Field(
        default=None,
        description="daily | weekly | monthly | seasonal | annual. Only set when the "
                    "user asks for a cadence (e.g. 'weekly', 'every month', 'day by day')."
    )
    start_date: Optional[str] = Field(
        default=None,
        description="Exact start date YYYY-MM-DD when the user gives a specific date."
    )
    end_date: Optional[str] = Field(
        default=None,
        description="Exact end date YYYY-MM-DD when the user gives a specific date."
    )
    event_date: Optional[str] = Field(
        default=None,
        description="YYYY-MM-DD of an event the user refers to (flood, landslide, spill) "
                    "when the question is about before/after that event."
    )

    @field_validator("reasoning", mode="before")
    @classmethod
    def _reasoning_normalise(cls, v):
        if v is None:
            return None
        s = str(v).strip()
        return s[:600] if s else None

    @field_validator("temporal_resolution", mode="before")
    @classmethod
    def _resolution_normalise(cls, v):
        """Accept synonyms Gemini may emit; anything else becomes null."""
        if v is None:
            return None
        v = str(v).strip().lower()
        aliases = {
            "day": "daily", "days": "daily", "daily": "daily",
            "week": "weekly", "weeks": "weekly", "weekly": "weekly",
            "month": "monthly", "months": "monthly", "monthly": "monthly",
            "season": "seasonal", "seasons": "seasonal", "seasonal": "seasonal",
            "year": "annual", "years": "annual", "yearly": "annual", "annual": "annual",
        }
        return aliases.get(v)

    @field_validator("start_date", "end_date", "event_date", mode="before")
    @classmethod
    def _date_normalise(cls, v):
        """Keep only well-formed YYYY-MM-DD strings."""
        if v is None:
            return None
        v = str(v).strip()
        if len(v) >= 10 and v[4] == "-" and v[7] == "-":
            try:
                from datetime import date
                date.fromisoformat(v[:10])
                return v[:10]
            except ValueError:
                return None
        return None

    # --- Business-search parameters ---
    business_type: Optional[str] = Field(
        default=None,
        description="For business_location: category (cafe, restaurant, hotel, …)."
    )
    keyword: Optional[str] = Field(
        default=None,
        description="For business_location: specific keyword (sushi, pizza, Starbucks, …)."
    )
    radius: Optional[int] = Field(
        default=2000,
        description="Search radius in metres (business_location)."
    )

    @field_validator("radius", mode="before")
    @classmethod
    def _radius_default(cls, v):
        """Gemini sometimes returns null — fall back to 2000 m."""
        return v if v is not None else 2000

    # --- Satellite / environmental parameters ---
    pollutants: Optional[List[str]] = Field(
        default=None,
        description="For air_quality: list of pollutants to analyse (NO2, CO, SO2, O3, CH4, Aerosol)."
    )
    water_indicators: Optional[List[str]] = Field(
        default=None,
        description="For water_quality: list of indicators (turbidity, chlorophyll, temperature, NDWI)."
    )
    vegetation_index_type: Optional[str] = Field(
        default=None,
        description="For vegetation_index: index type (NDVI, EVI, SAVI). Default NDVI."
    )

    # --- Clarification ---
    needs_clarification: List[str] = Field(
        default_factory=list,
        description="List of fields that are still missing and need user input. "
                    "Empty list means we have everything."
    )
    clarification_message: Optional[str] = Field(
        default=None,
        description="A friendly message asking the user for the missing info. "
                    "Only set when needs_clarification is non-empty."
    )

    # --- General question ---
    general_answer: Optional[str] = Field(
        default=None,
        description="If intent is general_question, provide the answer directly here."
    )

    # --- Router reasoning (shown to the user as the first thinking step) ---
    reasoning: Optional[str] = Field(
        default=None,
        description="One or two sentences on how the request was read: intent, "
                    "location, time window, resolution."
    )
