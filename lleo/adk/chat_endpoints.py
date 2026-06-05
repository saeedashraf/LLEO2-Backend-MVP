"""
ADK-Based Chat Endpoints for LLEO
Replaces manual conversation management with Google ADK Runner
"""

from fastapi import HTTPException, Form, BackgroundTasks
from typing import Dict, Any
import json
import time
from .adk_service import get_runner, get_session_service, LLEOToolExecutor
from ..core.base_processor import AnalysisRequest
from ..storage.cloud_manager import CloudFileManager


# =============================================================================
# ADK CHAT ENDPOINTS
# =============================================================================

async def adk_chat_start(
    query: str,
    user_id: str,
    processor,
    file_manager: CloudFileManager,
    db_session,
    background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """
    Start new conversation using Google ADK.

    This replaces the old /chat/start endpoint with ADK-based conversation management.

    Args:
        query: User's initial message
        user_id: User identifier
        processor: LLEO processor instance
        file_manager: Cloud storage manager
        db_session: Database session
        background_tasks: FastAPI background tasks

    Returns:
        Dictionary with session_id, response, and analysis info
    """
    try:
        print(f"💬 Starting ADK chat session for user: {user_id}")
        print(f"📝 Query: {query}")

        # Get ADK runner
        runner = get_runner()

        # Create new session ID
        session_id = f"adk-session-{user_id}-{int(time.time())}"

        # Create tool executor
        tool_executor = LLEOToolExecutor(processor)

        # Run ADK agent with query
        # The agent will automatically:
        # 1. Understand the query using Gemini
        # 2. Determine if it needs to call Earth Engine tools
        # 3. Handle follow-ups by checking memory bank
        # 4. Ask clarifying questions if needed
        response = await runner.run(
            session_id=session_id,
            message=query,
            user_id=user_id,
            tool_executor=tool_executor.execute_tool
        )

        # Parse response
        response_text = response.text
        tool_calls = response.tool_calls if hasattr(response, 'tool_calls') else []
        metadata = response.metadata if hasattr(response, 'metadata') else {}

        # Check if agent called any Earth Engine tools
        if tool_calls:
            print(f"🛠️  Agent called {len(tool_calls)} tools")

            # Process tool results
            for tool_call in tool_calls:
                tool_name = tool_call.get('tool_name')
                tool_result = tool_call.get('result')

                print(f"   - {tool_name}: {tool_result.get('status', 'unknown')}")

                # If this was an analysis tool, save results to database
                if tool_name in ['analyze_vegetation', 'analyze_air_quality', 'analyze_water_quality']:
                    # Schedule background task to save analysis
                    background_tasks.add_task(
                        save_adk_analysis_to_db,
                        session_id=session_id,
                        user_id=user_id,
                        query=query,
                        tool_result=tool_result,
                        response_text=response_text,
                        db_session=db_session
                    )

            return {
                'session_id': session_id,
                'status': 'complete',
                'message': response_text,
                'tool_calls': [{'tool': tc.get('tool_name'), 'status': 'executed'} for tc in tool_calls],
                'analysis_type': metadata.get('analysis_type'),
                'is_conversational': False  # This IS an analysis
            }

        else:
            # No tools called - either clarification needed or informational response
            print(f"💡 No tools called - informational or clarification response")

            return {
                'session_id': session_id,
                'status': 'complete',
                'message': response_text,
                'is_conversational': True,  # Flag to frontend: don't save as full analysis
                'needs_clarification': metadata.get('needs_clarification', False)
            }

    except Exception as e:
        print(f"❌ ADK chat start error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to start ADK chat: {str(e)}")


async def adk_chat_continue(
    session_id: str,
    message: str,
    user_id: str,
    processor,
    file_manager: CloudFileManager,
    db_session,
    background_tasks: BackgroundTasks
) -> Dict[str, Any]:
    """
    Continue existing conversation using Google ADK.

    This replaces the old /chat/continue endpoint with ADK-based conversation management.

    ADK automatically:
    - Loads conversation history from memory bank
    - Detects follow-up requests (e.g., "give me addresses of five of them")
    - Extracts info from previous responses instead of re-running Earth Engine
    - Handles clarification questions naturally

    Args:
        session_id: Existing session identifier
        message: User's new message
        user_id: User identifier
        processor: LLEO processor instance
        file_manager: Cloud storage manager
        db_session: Database session
        background_tasks: FastAPI background tasks

    Returns:
        Dictionary with response and metadata
    """
    try:
        print(f"💬 Continuing ADK chat session: {session_id}")
        print(f"📝 User message: {message}")

        # Get ADK runner
        runner = get_runner()

        # Create tool executor
        tool_executor = LLEOToolExecutor(processor)

        # Run ADK agent with message in existing session
        # ADK will automatically:
        # 1. Load full conversation history from memory
        # 2. Use PreloadMemoryTool to inject relevant context
        # 3. Detect if this is a follow-up request
        # 4. Extract info from memory instead of re-running tools
        response = await runner.run(
            session_id=session_id,
            message=message,
            user_id=user_id,
            tool_executor=tool_executor.execute_tool
        )

        # Parse response
        response_text = response.text
        tool_calls = response.tool_calls if hasattr(response, 'tool_calls') else []
        metadata = response.metadata if hasattr(response, 'metadata') else {}

        # Check if this was a follow-up detail request
        is_detail_request = metadata.get('is_detail_request', False)

        if is_detail_request:
            print(f"🔍 ADK detected follow-up detail request")
            print(f"   Response extracted from memory, no Earth Engine re-run")

            return {
                'session_id': session_id,
                'status': 'detail_response',
                'message': response_text,
                'is_conversational': True,  # Don't save as new analysis
                'from_memory': True  # Flag indicating no new Earth Engine run
            }

        # Check if agent called any Earth Engine tools (new analysis)
        elif tool_calls:
            print(f"🛠️  Agent called {len(tool_calls)} tools for new analysis")

            # Process tool results
            for tool_call in tool_calls:
                tool_name = tool_call.get('tool_name')
                tool_result = tool_call.get('result')

                print(f"   - {tool_name}: {tool_result.get('status', 'unknown')}")

                # If this was an analysis tool, save results to database
                if tool_name in ['analyze_vegetation', 'analyze_air_quality', 'analyze_water_quality']:
                    # Schedule background task to save analysis
                    background_tasks.add_task(
                        save_adk_analysis_to_db,
                        session_id=session_id,
                        user_id=user_id,
                        query=message,
                        tool_result=tool_result,
                        response_text=response_text,
                        db_session=db_session
                    )

            return {
                'session_id': session_id,
                'status': 'complete',
                'message': response_text,
                'tool_calls': [{'tool': tc.get('tool_name'), 'status': 'executed'} for tc in tool_calls],
                'is_conversational': False  # This IS a new analysis
            }

        else:
            # No tools called - informational or clarification response
            print(f"💡 Informational or clarification response")

            return {
                'session_id': session_id,
                'status': 'complete',
                'message': response_text,
                'is_conversational': True,
                'needs_clarification': metadata.get('needs_clarification', False)
            }

    except Exception as e:
        print(f"❌ ADK chat continue error: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Failed to continue ADK chat: {str(e)}")


async def adk_get_conversation_history(session_id: str) -> Dict[str, Any]:
    """
    Retrieve full conversation history from ADK memory bank.

    Args:
        session_id: Session identifier

    Returns:
        Dictionary with messages and metadata
    """
    try:
        session_service = get_session_service()

        # Get session from Vertex AI
        session = await session_service.get_session(session_id)

        if not session:
            return {
                'session_id': session_id,
                'messages': [],
                'exists': False
            }

        return {
            'session_id': session_id,
            'messages': session.messages,
            'metadata': session.metadata,
            'exists': True
        }

    except Exception as e:
        print(f"⚠️  Failed to get conversation history: {e}")
        return {
            'session_id': session_id,
            'messages': [],
            'exists': False,
            'error': str(e)
        }


# =============================================================================
# DATABASE INTEGRATION
# =============================================================================

async def save_adk_analysis_to_db(
    session_id: str,
    user_id: str,
    query: str,
    tool_result: Dict[str, Any],
    response_text: str,
    db_session
):
    """
    Save ADK analysis result to database.

    Args:
        session_id: Session identifier
        user_id: User identifier
        query: User's query
        tool_result: Result from Earth Engine tool
        response_text: Agent's response text
        db_session: Database session
    """
    try:
        from ..database.models import AnalysisRecord

        # Get conversation history from ADK
        history = await adk_get_conversation_history(session_id)

        # Create analysis record
        analysis_record = AnalysisRecord(
            session_id=session_id,
            user_id=user_id,
            query=query,
            analysis_type=tool_result.get('analysis_type'),
            location_name=tool_result.get('location_name'),
            coordinates=tool_result.get('coordinates'),
            start_year=tool_result.get('start_year'),
            end_year=tool_result.get('end_year'),
            files=tool_result.get('files'),
            status='completed',
            thumbnail_path=tool_result.get('thumbnail_path'),
            results_data=tool_result.get('metadata'),
            conversation_history=history.get('messages', [])
        )

        # Save to database
        db_session.add(analysis_record)
        db_session.commit()

        print(f"✅ Saved ADK analysis to database: {session_id}")

    except Exception as e:
        print(f"❌ Failed to save ADK analysis to database: {e}")
        import traceback
        traceback.print_exc()
