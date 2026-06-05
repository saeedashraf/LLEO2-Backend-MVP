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
