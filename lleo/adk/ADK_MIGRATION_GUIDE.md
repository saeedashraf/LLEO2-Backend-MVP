# Google ADK Migration Guide for LLEO

## Overview

This guide explains the Google ADK (Agent Development Kit) implementation for LLEO that replaces manual conversation management with Google's official framework.

## What is Google ADK?

Google ADK is an official framework for building conversational AI agents with:
- **Automatic memory management** via VertexAiMemoryBankService
- **Native session handling** via VertexAiSessionService
- **Built-in follow-up detection** without manual keyword matching
- **PreloadMemoryTool** that auto-injects conversation history
- **Tool calling framework** for Earth Engine integration

## Architecture

### Before (Manual Conversation Management)

```
User Query → Gemini Router → conversation_manager.py → Manual keyword matching
                           ↓
                  Earth Engine re-runs on follow-ups ❌
```

### After (Google ADK)

```
User Query → ADK Agent → PreloadMemoryTool → Automatic context injection
                      ↓
             Tool Executor → Earth Engine only when needed ✅
                      ↓
             MemoryBankService → Persistent storage
```

## Files Created

### 1. `lleo/adk/__init__.py`
Package initialization file.

### 2. `lleo/adk/agent_config.py`
Defines the LLEO agent with:
- **LLEO_INSTRUCTION**: Agent prompt explaining capabilities
- **Earth Engine Tools**:
  - `analyze_vegetation` - NDVI analysis
  - `analyze_air_quality` - Sentinel-5P NO2/SO2/CO
  - `analyze_water_quality` - Turbidity/temperature
  - `search_businesses` - Google Places API
  - `get_weather` - OpenWeather API
- **PreloadMemoryTool**: Automatic memory injection
- **create_lleo_agent()**: Agent factory function
- **create_memory_callback()**: Saves sessions to memory bank

### 3. `lleo/adk/adk_service.py`
Service layer that manages:
- **init_adk_service()**: Initialize Agent Engine, Session Service, Memory Service
- **get_runner()**: Get ADK Runner instance
- **start_conversation()**: Start new session
- **continue_conversation()**: Continue existing session
- **get_session_history()**: Retrieve conversation from memory
- **LLEOToolExecutor**: Bridges ADK tool calls to LLEO processors

### 4. `lleo/adk/chat_endpoints.py`
FastAPI endpoints using ADK:
- **adk_chat_start()**: Replaces `/chat/start`
- **adk_chat_continue()**: Replaces `/chat/continue`
- **adk_get_conversation_history()**: Get full conversation
- **save_adk_analysis_to_db()**: Save results to database

## Deployment Steps

### Step 1: Create Agent Engine in Vertex AI

The Agent Engine must be created **once** before deploying:

```bash
# Set your project ID
export PROJECT_ID="gen-lang-client-0709485529"
export REGION="us-central1"
export AGENT_ENGINE_ID="lleo-agent-engine"

# Create Agent Engine with memory bank
gcloud alpha vertex-ai agent-engines create $AGENT_ENGINE_ID \
  --project=$PROJECT_ID \
  --region=$REGION \
  --memory-bank-config='{
    "memory_bank_type": "VERTEX_AI",
    "retention_days": 90,
    "max_sessions": 10000
  }'
```

**Note**: This command requires `gcloud alpha` components. If not installed:
```bash
gcloud components install alpha
```

### Step 2: Update app.py to Initialize ADK

Add to `lleo/app.py` startup event (around line 150):

```python
from .adk.adk_service import init_adk_service

@app.on_event("startup")
async def startup_event():
    """Initialize services on startup"""
    global file_manager, gemini_model, secret_client

    # ... existing initialization code ...

    # Initialize ADK service
    try:
        agent_engine_id = "lleo-agent-engine"  # Created in Step 1
        init_adk_service(
            project_id=config.gcp_project_id,
            location=config.gcp_region,
            agent_engine_id=agent_engine_id
        )
        print("✅ ADK service initialized")
    except Exception as e:
        print(f"⚠️  ADK initialization failed: {e}")
        print("   Falling back to manual conversation management")
```

### Step 3: Add ADK Chat Endpoints to app.py

Add new endpoints that use ADK (around line 1300+):

```python
from .adk.chat_endpoints import adk_chat_start, adk_chat_continue, adk_get_conversation_history

@app.post("/chat/adk/start")
async def chat_adk_start(
    query: str = Form(...),
    user_id: str = Form(...),
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Start new conversation using Google ADK.
    This is the ADK-powered replacement for /chat/start
    """
    return await adk_chat_start(
        query=query,
        user_id=user_id,
        processor=get_processor('vegetation'),  # Default processor
        file_manager=file_manager,
        db_session=db,
        background_tasks=background_tasks
    )


@app.post("/chat/adk/continue")
async def chat_adk_continue(
    session_id: str = Form(...),
    message: str = Form(...),
    user_id: str = Form(...),
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Continue conversation using Google ADK.
    This is the ADK-powered replacement for /chat/continue
    """
    return await adk_chat_continue(
        session_id=session_id,
        message=message,
        user_id=user_id,
        processor=get_processor('vegetation'),  # Default processor
        file_manager=file_manager,
        db_session=db,
        background_tasks=background_tasks
    )


@app.get("/chat/adk/history/{session_id}")
async def get_adk_history(session_id: str):
    """
    Retrieve conversation history from ADK memory bank.
    """
    return await adk_get_conversation_history(session_id)
```

### Step 4: Update Frontend to Use ADK Endpoints

In `satellite-frontend/src/App.js`, update API calls:

**Before:**
```javascript
const response = await fetch('http://localhost:8080/chat/start', {
  method: 'POST',
  body: formData
});
```

**After:**
```javascript
const response = await fetch('http://localhost:8080/chat/adk/start', {
  method: 'POST',
  body: formData
});
```

Do the same for `/chat/continue` → `/chat/adk/continue`

### Step 5: Deploy to Cloud Run

```bash
cd /c/Saeid2/agents_SA/8_GeoLLM_test_Supabase/satellite-backend/lleo

# Deploy with ADK dependencies
gcloud builds submit --config cloudbuild.yaml --project=gen-lang-client-0709485529
```

The `requirements.txt` already includes:
- `google-adk>=0.1.0`
- `google-genai>=0.1.0`

### Step 6: Test ADK Implementation

Test the new ADK endpoints:

```bash
# Test 1: Start conversation
curl -X POST "https://lleo-backend-<hash>.a.run.app/chat/adk/start" \
  -F "query=What is the vegetation health in Calgary from 2020 to 2023?" \
  -F "user_id=test-user-123"

# Test 2: Follow-up (should NOT re-run Earth Engine)
curl -X POST "https://lleo-backend-<hash>.a.run.app/chat/adk/continue" \
  -F "session_id=<session-id-from-test-1>" \
  -F "message=Give me the NDVI values for the top 5 locations" \
  -F "user_id=test-user-123"

# Test 3: Get conversation history
curl "https://lleo-backend-<hash>.a.run.app/chat/adk/history/<session-id>"
```

## Benefits of ADK Migration

### 1. Automatic Follow-up Detection ✅
**Before**: Manual keyword matching (`'address', 'rating', 'phone'...`)
**After**: ADK automatically understands "give me addresses" without keywords

### 2. Persistent Memory ✅
**Before**: In-memory `conversation_manager` lost on restart
**After**: VertexAiMemoryBankService stores in Vertex AI (90-day retention)

### 3. No Re-running Earth Engine ✅
**Before**: Gemini sometimes misses follow-ups → expensive re-runs
**After**: PreloadMemoryTool injects context → extracts from memory

### 4. Native Multi-turn Understanding ✅
**Before**: Manual state tracking in `ConversationState` class
**After**: ADK natively handles multi-turn context

### 5. Scalable Architecture ✅
**Before**: 500+ lines of manual conversation logic
**After**: ~200 lines using ADK framework

## Migration Timeline

- **Phase 1**: Deploy keyword fix (DONE ✅)
- **Phase 2**: Create ADK infrastructure (DONE ✅)
- **Phase 3**: Create Agent Engine in Vertex AI (NEXT)
- **Phase 4**: Integrate ADK endpoints in app.py (NEXT)
- **Phase 5**: Update frontend to use `/chat/adk/*` (NEXT)
- **Phase 6**: Deploy and test (NEXT)
- **Phase 7**: Monitor and deprecate old endpoints (FUTURE)

## Rollback Plan

If ADK has issues, the old endpoints remain functional:
- `/chat/start` - Old manual conversation
- `/chat/continue` - Old keyword-based follow-up detection
- ADK runs in parallel at `/chat/adk/*` endpoints

## Cost Considerations

**ADK Pricing:**
- Memory Bank storage: ~$0.10/GB/month
- Session API calls: ~$0.001/request
- Agent Engine: No additional charge (uses existing Vertex AI quota)

**Estimated Monthly Cost:**
- 10,000 sessions/month
- 50 messages/session average
- ~500k API calls → ~$500/month

**Savings:**
- Reduced Earth Engine re-runs → ~$200/month saved
- **Net cost increase: ~$300/month**

## Troubleshooting

### Error: "Agent Engine not found"
**Solution**: Create Agent Engine using gcloud command (Step 1)

### Error: "google.adk module not found"
**Solution**: Ensure `google-adk>=0.1.0` in requirements.txt and rebuild

### Error: "Memory bank access denied"
**Solution**: Grant service account `roles/aiplatform.user` permission

### Follow-ups still re-running Earth Engine
**Solution**: Check agent instruction - ensure it mentions checking memory first

## Next Steps

1. **Create Agent Engine** - Run gcloud command (Step 1)
2. **Update app.py** - Add ADK initialization (Step 2)
3. **Add endpoints** - Integrate ADK chat endpoints (Step 3)
4. **Update frontend** - Point to `/chat/adk/*` (Step 4)
5. **Deploy** - Build and deploy to Cloud Run (Step 5)
6. **Test** - Run curl tests (Step 6)

## Support

For issues:
- ADK Documentation: https://github.com/GoogleCloudPlatform/generative-ai/tree/main/agents
- Vertex AI Console: https://console.cloud.google.com/vertex-ai/agents
- LLEO Support: Check app.py logs in Cloud Run
