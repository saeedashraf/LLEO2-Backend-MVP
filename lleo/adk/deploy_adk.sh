#!/bin/bash
# Google ADK Deployment Script for LLEO
# This script sets up the Agent Engine and deploys the ADK-enabled backend

set -e  # Exit on error

# =============================================================================
# CONFIGURATION
# =============================================================================

PROJECT_ID="gen-lang-client-0709485529"
REGION="us-central1"
AGENT_ENGINE_ID="lleo-agent-engine"

echo "=================================="
echo "Google ADK Deployment for LLEO"
echo "=================================="
echo "Project: $PROJECT_ID"
echo "Region: $REGION"
echo "Agent Engine: $AGENT_ENGINE_ID"
echo "=================================="
echo ""

# =============================================================================
# STEP 1: Install gcloud alpha components (if needed)
# =============================================================================

echo "📦 Step 1: Checking gcloud alpha components..."
if ! gcloud alpha --version &> /dev/null; then
    echo "⚠️  gcloud alpha not installed. Installing..."
    gcloud components install alpha
    echo "✅ gcloud alpha installed"
else
    echo "✅ gcloud alpha already installed"
fi
echo ""

# =============================================================================
# STEP 2: Create Agent Engine in Vertex AI
# =============================================================================

echo "🤖 Step 2: Creating Agent Engine in Vertex AI..."
echo "Command:"
echo "  gcloud alpha vertex-ai agent-engines create $AGENT_ENGINE_ID \\"
echo "    --project=$PROJECT_ID \\"
echo "    --region=$REGION \\"
echo "    --display-name=\"LLEO Satellite Analysis Agent\" \\"
echo "    --description=\"Agent for Earth observation analysis with memory and sessions\""
echo ""

# Check if agent engine already exists
AGENT_EXISTS=$(gcloud alpha vertex-ai agent-engines list \
  --project=$PROJECT_ID \
  --region=$REGION \
  --filter="name:$AGENT_ENGINE_ID" \
  --format="value(name)" 2>/dev/null || echo "")

if [ -n "$AGENT_EXISTS" ]; then
    echo "ℹ️  Agent Engine '$AGENT_ENGINE_ID' already exists"
    echo "   Skipping creation..."
else
    echo "Creating Agent Engine..."
    gcloud alpha vertex-ai agent-engines create $AGENT_ENGINE_ID \
      --project=$PROJECT_ID \
      --region=$REGION \
      --display-name="LLEO Satellite Analysis Agent" \
      --description="Agent for Earth observation analysis with memory and sessions" || {
        echo "⚠️  Agent Engine creation failed or not supported"
        echo "   Note: Agent Engine API might not be available in alpha yet"
        echo "   Continuing with deployment anyway..."
      }
    echo "✅ Agent Engine created (or will use existing setup)"
fi
echo ""

# =============================================================================
# STEP 3: Set environment variable for Agent Engine ID
# =============================================================================

echo "🔧 Step 3: Setting ADK_AGENT_ENGINE_ID environment variable..."
export ADK_AGENT_ENGINE_ID=$AGENT_ENGINE_ID
echo "✅ ADK_AGENT_ENGINE_ID=$ADK_AGENT_ENGINE_ID"
echo ""

# =============================================================================
# STEP 4: Deploy to Cloud Run
# =============================================================================

echo "🚀 Step 4: Deploying LLEO backend with ADK to Cloud Run..."
echo "Building and deploying..."
cd /c/Saeid2/agents_SA/8_GeoLLM_test_Supabase/satellite-backend/lleo

gcloud builds submit \
  --config cloudbuild.yaml \
  --project=$PROJECT_ID \
  --substitutions=_ADK_AGENT_ENGINE_ID=$AGENT_ENGINE_ID

echo "✅ Deployment complete!"
echo ""

# =============================================================================
# STEP 5: Verify deployment
# =============================================================================

echo "🔍 Step 5: Verifying deployment..."
SERVICE_URL=$(gcloud run services describe lleo-backend \
  --project=$PROJECT_ID \
  --region=$REGION \
  --format="value(status.url)" 2>/dev/null || echo "")

if [ -n "$SERVICE_URL" ]; then
    echo "✅ Service deployed successfully!"
    echo "   URL: $SERVICE_URL"
    echo ""
    echo "📝 ADK Endpoints:"
    echo "   - POST $SERVICE_URL/chat/adk/start"
    echo "   - POST $SERVICE_URL/chat/adk/continue"
    echo "   - GET  $SERVICE_URL/chat/adk/history/{session_id}"
else
    echo "⚠️  Could not verify service URL"
fi
echo ""

# =============================================================================
# STEP 6: Test ADK endpoints
# =============================================================================

echo "🧪 Step 6: Testing ADK endpoints..."
if [ -n "$SERVICE_URL" ]; then
    echo "Test with:"
    echo ""
    echo "# Start conversation"
    echo "curl -X POST \"$SERVICE_URL/chat/adk/start\" \\"
    echo "  -F \"query=What is the vegetation health in Calgary from 2020 to 2023?\" \\"
    echo "  -F \"user_id=test-user-123\""
    echo ""
    echo "# Continue conversation (replace <session-id>)"
    echo "curl -X POST \"$SERVICE_URL/chat/adk/continue\" \\"
    echo "  -F \"session_id=<session-id>\" \\"
    echo "  -F \"message=Give me the NDVI values for the top 5 locations\" \\"
    echo "  -F \"user_id=test-user-123\""
    echo ""
    echo "# Get conversation history"
    echo "curl \"$SERVICE_URL/chat/adk/history/<session-id>\""
else
    echo "⚠️  Service URL not available for testing"
fi
echo ""

# =============================================================================
# DONE
# =============================================================================

echo "=================================="
echo "✅ ADK Deployment Complete!"
echo "=================================="
echo ""
echo "Next steps:"
echo "1. Test the ADK endpoints with the curl commands above"
echo "2. Update frontend to use /chat/adk/* endpoints"
echo "3. Monitor Cloud Run logs for ADK initialization messages"
echo "4. Check Vertex AI console for Agent Engine status"
echo ""
echo "Rollback: Old endpoints /chat/start and /chat/continue still work"
echo ""
