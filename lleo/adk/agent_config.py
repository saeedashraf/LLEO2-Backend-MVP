"""
LLEO Agent Configuration using Google ADK
Defines the satellite analysis agent with Earth Engine tools and memory management
"""

from google_genai import types
from google.adk import Agent
from google.adk.tools import PreloadMemoryTool
import os


# =============================================================================
# AGENT INSTRUCTION
# =============================================================================

LLEO_INSTRUCTION = """
You are LLEO (Large Language Models for Earth Observation), an expert satellite analysis assistant powered by Google Earth Engine.

Your capabilities:
1. Analyze vegetation health using NDVI (Normalized Difference Vegetation Index)
2. Analyze air quality using Sentinel-5P NO2 and other pollutants
3. Analyze water quality in rivers and lakes
4. Search for business locations in specific areas
5. Provide weather information

When users ask questions:
- For NEW analysis requests: Use the appropriate Earth Engine tool (analyze_vegetation, analyze_air_quality, analyze_water_quality, search_businesses, get_weather)
- For FOLLOW-UP requests asking for details from previous analyses: Extract information from conversation memory WITHOUT re-running Earth Engine

Examples of follow-up requests that should NOT trigger new analysis:
- "give me the address of five of them"
- "show me the phone numbers"
- "tell me the top 10 locations"
- "what were their ratings?"
- "give me more details"

Always check conversation history first before running expensive Earth Engine operations.
"""


# =============================================================================
# EARTH ENGINE TOOLS
# =============================================================================

def create_lleo_tools():
    """
    Create ADK-compatible tool definitions for Earth Engine processors.

    Returns:
        List of tool declarations for the LLEO agent
    """

    # Tool 1: Vegetation Analysis
    analyze_vegetation_tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="analyze_vegetation",
                description="Analyze vegetation health using NDVI from satellite imagery. Returns temporal trends, spatial distribution, and health metrics.",
                parameters={
                    "type": "object",
                    "properties": {
                        "location_name": {
                            "type": "string",
                            "description": "Name of the location to analyze (e.g., 'Calgary, Canada' or 'Bow River')"
                        },
                        "start_year": {
                            "type": "integer",
                            "description": "Start year for analysis"
                        },
                        "end_year": {
                            "type": "integer",
                            "description": "End year for analysis"
                        },
                        "bounds": {
                            "type": "object",
                            "description": "Geographic bounding box with min_lat, max_lat, min_lng, max_lng",
                            "properties": {
                                "min_lat": {"type": "number"},
                                "max_lat": {"type": "number"},
                                "min_lng": {"type": "number"},
                                "max_lng": {"type": "number"}
                            }
                        }
                    },
                    "required": ["location_name", "start_year", "end_year"]
                }
            )
        ]
    )

    # Tool 2: Air Quality Analysis
    analyze_air_quality_tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="analyze_air_quality",
                description="Analyze air quality using Sentinel-5P satellite data for NO2, SO2, CO, and other pollutants. Returns temporal trends and spatial distribution.",
                parameters={
                    "type": "object",
                    "properties": {
                        "location_name": {
                            "type": "string",
                            "description": "Name of the location to analyze"
                        },
                        "start_year": {
                            "type": "integer",
                            "description": "Start year for analysis"
                        },
                        "end_year": {
                            "type": "integer",
                            "description": "End year for analysis"
                        },
                        "bounds": {
                            "type": "object",
                            "description": "Geographic bounding box",
                            "properties": {
                                "min_lat": {"type": "number"},
                                "max_lat": {"type": "number"},
                                "min_lng": {"type": "number"},
                                "max_lng": {"type": "number"}
                            }
                        }
                    },
                    "required": ["location_name", "start_year", "end_year"]
                }
            )
        ]
    )

    # Tool 3: Water Quality Analysis
    analyze_water_quality_tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="analyze_water_quality",
                description="Analyze water quality in rivers and lakes using satellite imagery. Returns turbidity, temperature, and pollution indicators.",
                parameters={
                    "type": "object",
                    "properties": {
                        "location_name": {
                            "type": "string",
                            "description": "Name of the water body to analyze (e.g., 'Bow River', 'Lake Superior')"
                        },
                        "start_year": {
                            "type": "integer",
                            "description": "Start year for analysis"
                        },
                        "end_year": {
                            "type": "integer",
                            "description": "End year for analysis"
                        },
                        "bounds": {
                            "type": "object",
                            "description": "Geographic bounding box",
                            "properties": {
                                "min_lat": {"type": "number"},
                                "max_lat": {"type": "number"},
                                "min_lng": {"type": "number"},
                                "max_lng": {"type": "number"}
                            }
                        }
                    },
                    "required": ["location_name", "start_year", "end_year"]
                }
            )
        ]
    )

    # Tool 4: Business Location Search
    search_businesses_tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="search_businesses",
                description="Search for business locations in a specific area using Google Places API. Returns business names, addresses, ratings, and coordinates.",
                parameters={
                    "type": "object",
                    "properties": {
                        "location_name": {
                            "type": "string",
                            "description": "Name of the area to search (e.g., 'Calgary, Canada')"
                        },
                        "business_type": {
                            "type": "string",
                            "description": "Type of business to search for (e.g., 'restaurants', 'coffee shops', 'parks')"
                        },
                        "radius_km": {
                            "type": "number",
                            "description": "Search radius in kilometers (default: 5)",
                            "default": 5
                        }
                    },
                    "required": ["location_name", "business_type"]
                }
            )
        ]
    )

    # Tool 5: Weather Information
    get_weather_tool = types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="get_weather",
                description="Get current weather information for a location using OpenWeather API. Returns temperature, humidity, conditions, and forecast.",
                parameters={
                    "type": "object",
                    "properties": {
                        "location_name": {
                            "type": "string",
                            "description": "Name of the location for weather data"
                        }
                    },
                    "required": ["location_name"]
                }
            )
        ]
    )

    return [
        analyze_vegetation_tool,
        analyze_air_quality_tool,
        analyze_water_quality_tool,
        search_businesses_tool,
        get_weather_tool
    ]


# =============================================================================
# AGENT CREATION
# =============================================================================

def create_lleo_agent(project_id: str, location: str = "us-central1", model: str = "gemini-2.0-flash-exp"):
    """
    Create the LLEO agent with Earth Engine tools and memory integration.

    Args:
        project_id: GCP project ID
        location: GCP region (default: us-central1)
        model: Gemini model to use (default: gemini-2.0-flash-exp)

    Returns:
        Agent instance configured for LLEO
    """

    # Get Earth Engine tools
    tools = create_lleo_tools()

    # Add PreloadMemoryTool for automatic memory injection
    preload_memory_tool = PreloadMemoryTool()
    tools.append(preload_memory_tool)

    # Create agent
    agent = Agent(
        model=f"projects/{project_id}/locations/{location}/publishers/google/models/{model}",
        instruction=LLEO_INSTRUCTION,
        tools=tools,
        model_config={
            "temperature": 0.2,  # Lower temperature for more consistent responses
            "top_p": 0.95,
            "top_k": 40,
            "max_output_tokens": 2048
        }
    )

    return agent


# =============================================================================
# MEMORY CALLBACK
# =============================================================================

def create_memory_callback(memory_service):
    """
    Create callback function to save sessions to memory bank after agent responses.

    Args:
        memory_service: VertexAiMemoryBankService instance

    Returns:
        Callback function for after_agent processing
    """

    def after_agent_callback(session_id: str, messages: list):
        """
        Save session to memory bank after each agent interaction.

        Args:
            session_id: Unique session identifier
            messages: List of messages in the session
        """
        try:
            # Save session to memory bank
            memory_service.save_session(
                session_id=session_id,
                messages=messages,
                metadata={
                    "application": "lleo",
                    "message_count": len(messages)
                }
            )
            print(f"💾 Saved session {session_id} to memory bank ({len(messages)} messages)")
        except Exception as e:
            print(f"⚠️ Failed to save session to memory: {e}")

    return after_agent_callback
