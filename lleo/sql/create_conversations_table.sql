-- ============================================================================
-- Create conversations table for storing chat sessions
-- ============================================================================
-- Run this in Supabase SQL Editor:
-- https://supabase.com/dashboard/project/quvbsftdyaxjqlwnjtxv/sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.conversations (
    -- Primary key
    session_id TEXT PRIMARY KEY,

    -- User info
    user_id TEXT NOT NULL,

    -- Original query
    initial_query TEXT NOT NULL,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Full conversation state stored as JSON
    -- This includes messages array and all conversation history
    conversation_data JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- Extracted information (denormalized for quick queries)
    analysis_type TEXT,
    location TEXT,
    start_year INTEGER,
    end_year INTEGER,
    specific_months INTEGER[],
    parameters JSONB DEFAULT '{}'::jsonb,

    -- Status flags
    is_complete BOOLEAN DEFAULT FALSE,
    needs_clarification TEXT[] DEFAULT ARRAY[]::TEXT[]
);

-- ============================================================================
-- Indexes for performance
-- ============================================================================

-- Index on user_id for faster user-specific queries
CREATE INDEX IF NOT EXISTS idx_conversations_user_id
    ON public.conversations(user_id);

-- Index on created_at for time-based queries and cleanup
CREATE INDEX IF NOT EXISTS idx_conversations_created_at
    ON public.conversations(created_at DESC);

-- Index on is_complete for filtering active conversations
CREATE INDEX IF NOT EXISTS idx_conversations_is_complete
    ON public.conversations(is_complete);

-- GIN index on conversation_data for JSON queries
CREATE INDEX IF NOT EXISTS idx_conversations_data
    ON public.conversations USING GIN (conversation_data);

-- ============================================================================
-- Auto-update trigger for updated_at timestamp
-- ============================================================================

CREATE OR REPLACE FUNCTION update_conversations_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trigger_update_conversations_updated_at ON public.conversations;

CREATE TRIGGER trigger_update_conversations_updated_at
    BEFORE UPDATE ON public.conversations
    FOR EACH ROW
    EXECUTE FUNCTION update_conversations_updated_at();

-- ============================================================================
-- Row Level Security (RLS) Policies
-- ============================================================================

-- Enable RLS
ALTER TABLE public.conversations ENABLE ROW LEVEL SECURITY;

-- Policy 1: Users can view their own conversations (when authenticated)
DROP POLICY IF EXISTS "Users can view own conversations" ON public.conversations;
CREATE POLICY "Users can view own conversations"
    ON public.conversations
    FOR SELECT
    USING (auth.uid()::text = user_id);

-- Policy 2: Users can insert their own conversations
DROP POLICY IF EXISTS "Users can insert own conversations" ON public.conversations;
CREATE POLICY "Users can insert own conversations"
    ON public.conversations
    FOR INSERT
    WITH CHECK (auth.uid()::text = user_id);

-- Policy 3: Users can update their own conversations
DROP POLICY IF EXISTS "Users can update own conversations" ON public.conversations;
CREATE POLICY "Users can update own conversations"
    ON public.conversations
    FOR UPDATE
    USING (auth.uid()::text = user_id)
    WITH CHECK (auth.uid()::text = user_id);

-- Policy 4: Service role has full access (for backend operations)
DROP POLICY IF EXISTS "Service role has full access" ON public.conversations;
CREATE POLICY "Service role has full access"
    ON public.conversations
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ============================================================================
-- Table and column comments
-- ============================================================================

COMMENT ON TABLE public.conversations IS
    'Stores chat conversation sessions for LLEO analysis clarification. Used to maintain conversation state across multiple Cloud Run instances.';

COMMENT ON COLUMN public.conversations.session_id IS
    'Unique session identifier format: chat_YYYYMMDD_HHMMSS_hex';

COMMENT ON COLUMN public.conversations.conversation_data IS
    'Full conversation state as JSONB including messages array and history';

COMMENT ON COLUMN public.conversations.needs_clarification IS
    'Array of fields that still need user clarification (e.g., time_period, location)';

COMMENT ON COLUMN public.conversations.parameters IS
    'Analysis-specific parameters extracted from conversation';

-- ============================================================================
-- Optional: Cleanup function for old conversations
-- ============================================================================
-- You can run this manually or set up a scheduled cron job in Supabase

CREATE OR REPLACE FUNCTION cleanup_old_conversations(days_old INTEGER DEFAULT 7)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM public.conversations
    WHERE created_at < NOW() - (days_old || ' days')::INTERVAL;

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

COMMENT ON FUNCTION cleanup_old_conversations IS
    'Deletes conversations older than specified days (default 7). Usage: SELECT cleanup_old_conversations(7);';

-- ============================================================================
-- Verify table creation
-- ============================================================================

-- Run this to verify the table was created successfully
SELECT
    'conversations' AS table_name,
    COUNT(*) AS row_count,
    pg_size_pretty(pg_total_relation_size('public.conversations')) AS table_size
FROM public.conversations;

-- List all indexes
SELECT
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'conversations'
ORDER BY indexname;
