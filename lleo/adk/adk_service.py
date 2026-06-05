"""
ADK Service Layer for LLEO
Manages Agent Engine, Session Service, Memory Service, and Runner initialization
"""

import os
from typing import Optional, Dict, Any
from google.adk import Runner
from google.adk.services import VertexAiSessionService, VertexAiMemoryBankService
from google.cloud import aiplatform
from .agent_config import create_lleo_agent, create_memory_callback


# =============================================================================
# GLOBAL ADK INSTANCES
# =============================================================================

_runner: Optional[Runner] = None
_agent = None
_session_service: Optional[VertexAiSessionService] = None
_memory_service: Optional[VertexAiMemoryBankService] = None
_project_id: str = None
_location: str = "us-central1"


# =============================================================================
# INITIALIZATION
# =============================================================================

def init_adk_service(project_id: str, location: str = "us-central1", agent_engine_id: str = None):
    """
    Initialize ADK services: Agent, Session Service, Memory Service, and Runner.

    Args:
        project_id: GCP project ID
        location: GCP region (default: us-central1)
        agent_engine_id: Optional existing Agent Engine ID. If not provided, will create new one.

    This should be called once at application startup.
    """
    global _runner, _agent, _session_service, _memory_service, _project_id, _location

    _project_id = project_id
    _location = location

    # Initialize Vertex AI
    aiplatform.init(project=project_id, location=location)
    print(f"✅ Vertex AI initialized: {project_id} ({location})")

    # Create or get Agent Engine
    if not agent_engine_id:
        # Create new Agent Engine with memory bank configuration
        agent_engine_id = f"lleo-agent-engine-{os.getenv('GAE_VERSION', 'local')}"
        print(f"🤖 Creating Agent Engine: {agent_engine_id}")

        # Note: Agent Engine creation is typically done via gcloud or Vertex AI console
        # For programmatic creation, use the Vertex AI Agent Builder API
        print(f"⚠️  Agent Engine should be created via:")
        print(f"    gcloud alpha vertex-ai agent-engines create {agent_engine_id} \\")
        print(f"      --project={project_id} \\")
        print(f"      --region={location} \\")
        print(f"      --memory-bank-config='{{}}'")

    # Create LLEO agent
    _agent = create_lleo_agent(project_id=project_id, location=location)
    print(f"✅ LLEO Agent created")

    # Initialize Session Service
    _session_service = VertexAiSessionService(
        project_id=project_id,
        location=location,
        agent_engine_id=agent_engine_id
    )
    print(f"✅ Session Service initialized")

    # Initialize Memory Bank Service
    _memory_service = VertexAiMemoryBankService(
        project_id=project_id,
        location=location,
        agent_engine_id=agent_engine_id
    )
    print(f"✅ Memory Bank Service initialized")

    # Create memory callback
    memory_callback = create_memory_callback(_memory_service)

    # Initialize Runner
    _runner = Runner(
        agent=_agent,
        session_service=_session_service,
        memory_service=_memory_service,
        after_agent_callback=memory_callback
    )
    print(f"✅ ADK Runner initialized")

    return _runner


# =============================================================================
# GETTER FUNCTIONS
# =============================================================================

def get_runner() -> Runner:
    """
    Get the initialized ADK Runner instance.

    Returns:
        Runner instance for handling conversations

    Raises:
        RuntimeError: If ADK service not initialized
    """
    if _runner is None:
        raise RuntimeError("ADK service not initialized. Call init_adk_service() first.")
    return _runner


def get_session_service() -> VertexAiSessionService:
    """Get the Session Service instance."""
    if _session_service is None:
        raise RuntimeError("ADK service not initialized. Call init_adk_service() first.")
    return _session_service


def get_memory_service() -> VertexAiMemoryBankService:
    """Get the Memory Bank Service instance."""
    if _memory_service is None:
        raise RuntimeError("ADK service not initialized. Call init_adk_service() first.")
    return _memory_service


# =============================================================================
# CONVERSATION HELPERS
# =============================================================================

async def start_conversation(user_id: str, message: str) -> Dict[str, Any]:
    """
    Start a new conversation session using ADK.

    Args:
        user_id: Unique user identifier
        message: Initial user message

    Returns:
        Dictionary with session_id and response
    """
    runner = get_runner()

    # Create new session
    session_id = f"session-{user_id}-{int(time.time())}"

    # Run agent with message
    response = await runner.run(
        session_id=session_id,
        message=message,
        user_id=user_id
    )

    return {
        "session_id": session_id,
        "response": response.text,
        "metadata": response.metadata
    }


async def continue_conversation(session_id: str, user_id: str, message: str) -> Dict[str, Any]:
    """
    Continue an existing conversation session using ADK.

    Args:
        session_id: Existing session identifier
        user_id: User identifier
        message: User's new message

    Returns:
        Dictionary with response and metadata
    """
    runner = get_runner()

    # Run agent with message in existing session
    response = await runner.run(
        session_id=session_id,
        message=message,
        user_id=user_id
    )

    return {
        "response": response.text,
        "metadata": response.metadata,
        "tool_calls": response.tool_calls if hasattr(response, 'tool_calls') else []
    }


async def get_session_history(session_id: str) -> Dict[str, Any]:
    """
    Retrieve conversation history for a session from ADK memory.

    Args:
        session_id: Session identifier

    Returns:
        Dictionary with messages and metadata
    """
    session_service = get_session_service()

    # Get session from Vertex AI
    session = await session_service.get_session(session_id)

    if not session:
        return {
            "session_id": session_id,
            "messages": [],
            "exists": False
        }

    return {
        "session_id": session_id,
        "messages": session.messages,
        "metadata": session.metadata,
        "exists": True
    }


# =============================================================================
# TOOL EXECUTION INTEGRATION
# =============================================================================

class LLEOToolExecutor:
    """
    Tool executor that connects ADK tool calls to actual Earth Engine processors.
    """

    def __init__(self, lleo_processor):
        """
        Initialize with LLEO processor instance.

        Args:
            lleo_processor: LLEOProcessor instance with Earth Engine methods
        """
        self.processor = lleo_processor

    async def execute_tool(self, tool_name: str, parameters: Dict[str, Any]) -> Any:
        """
        Execute a tool call from the ADK agent.

        Args:
            tool_name: Name of the tool to execute
            parameters: Tool parameters from agent

        Returns:
            Tool execution result
        """

        if tool_name == "analyze_vegetation":
            return await self._execute_vegetation_analysis(parameters)

        elif tool_name == "analyze_air_quality":
            return await self._execute_air_quality_analysis(parameters)

        elif tool_name == "analyze_water_quality":
            return await self._execute_water_quality_analysis(parameters)

        elif tool_name == "search_businesses":
            return await self._execute_business_search(parameters)

        elif tool_name == "get_weather":
            return await self._execute_weather_request(parameters)

        else:
            raise ValueError(f"Unknown tool: {tool_name}")

    async def _execute_vegetation_analysis(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute vegetation analysis using LLEO processor."""
        # Convert ADK parameters to LLEO processor format
        result = await self.processor.process(
            query=f"Analyze vegetation in {params['location_name']}",
            analysis_type="vegetation",
            location_name=params['location_name'],
            time_period={
                "start_year": params['start_year'],
                "end_year": params['end_year']
            },
            bounds=params.get('bounds')
        )
        return result

    async def _execute_air_quality_analysis(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute air quality analysis using LLEO processor."""
        result = await self.processor.process(
            query=f"Analyze air quality in {params['location_name']}",
            analysis_type="air_quality",
            location_name=params['location_name'],
            time_period={
                "start_year": params['start_year'],
                "end_year": params['end_year']
            },
            bounds=params.get('bounds')
        )
        return result

    async def _execute_water_quality_analysis(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute water quality analysis using LLEO processor."""
        result = await self.processor.process(
            query=f"Analyze water quality in {params['location_name']}",
            analysis_type="water_quality",
            location_name=params['location_name'],
            time_period={
                "start_year": params['start_year'],
                "end_year": params['end_year']
            },
            bounds=params.get('bounds')
        )
        return result

    async def _execute_business_search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute business search using Google Places."""
        result = await self.processor.process(
            query=f"Find {params['business_type']} in {params['location_name']}",
            analysis_type="business_location",
            location_name=params['location_name'],
            business_type=params['business_type'],
            radius_km=params.get('radius_km', 5)
        )
        return result

    async def _execute_weather_request(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute weather data request."""
        result = await self.processor.process(
            query=f"Get weather for {params['location_name']}",
            analysis_type="weather",
            location_name=params['location_name']
        )
        return result


import time
