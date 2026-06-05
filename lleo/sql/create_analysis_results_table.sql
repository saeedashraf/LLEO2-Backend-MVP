-- ============================================================================
-- Create analysis_results table for tracking completed analyses
-- ============================================================================
-- Run this in Supabase SQL Editor:
-- https://supabase.com/dashboard/project/quvbsftdyaxjqlwnjtxv/sql
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.analysis_results (
    -- Primary key: analysis execution ID (e.g., 20251113_063949_3d8f7b6dd691)
    analysis_session_id TEXT PRIMARY KEY,

    -- Link to conversation session (can be NULL for legacy /analyze endpoint)
    conversation_session_id TEXT REFERENCES public.conversations(session_id) ON DELETE SET NULL,

    -- User info
    user_id TEXT NOT NULL,

    -- Analysis details
    analysis_type TEXT NOT NULL, -- vegetation_index, water_quality, air_quality, etc.
    location TEXT NOT NULL, -- normalized location name
    location_lat DOUBLE PRECISION,
    location_lng DOUBLE PRECISION,

    -- Time period (NULL for non-temporal analyses like business_location)
    start_year INTEGER,
    end_year INTEGER,
    specific_months INTEGER[],

    -- Parameters (analysis-specific, stored as JSON)
    parameters JSONB DEFAULT '{}'::jsonb,

    -- Storage information
    storage_path TEXT NOT NULL, -- GCS path: analysis_results/{analysis_session_id}/
    bucket_name TEXT DEFAULT 'ndvi3-analysis-storage-bucket',

    -- Files generated
    map_url TEXT, -- HTML map file URL
    charts_url TEXT, -- PNG charts file URL
    preview_url TEXT, -- Preview/thumbnail URL
    results_json_url TEXT, -- results.json URL

    -- Status tracking
    status TEXT NOT NULL DEFAULT 'pending', -- pending, processing, completed, failed
    error_message TEXT,

    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,

    -- Processing metadata (stored as JSON)
    metadata JSONB DEFAULT '{}'::jsonb,

    -- Processing time in seconds
    processing_time_seconds DOUBLE PRECISION
);

-- ============================================================================
-- Indexes for performance
-- ============================================================================

-- Index on user_id for user-specific queries
CREATE INDEX IF NOT EXISTS idx_analysis_results_user_id
    ON public.analysis_results(user_id);

-- Index on conversation_session_id for follow-up detection
CREATE INDEX IF NOT EXISTS idx_analysis_results_conversation_session_id
    ON public.analysis_results(conversation_session_id);

-- Composite index for fast follow-up detection
-- Query: "Find recent analysis with same type, location, and time period"
CREATE INDEX IF NOT EXISTS idx_analysis_results_follow_up
    ON public.analysis_results(user_id, analysis_type, location, start_year, end_year, status, completed_at DESC);

-- Index on status for filtering active/completed analyses
CREATE INDEX IF NOT EXISTS idx_analysis_results_status
    ON public.analysis_results(status);

-- Index on created_at for time-based queries and cleanup
CREATE INDEX IF NOT EXISTS idx_analysis_results_created_at
    ON public.analysis_results(created_at DESC);

-- Index on analysis_type for analytics
CREATE INDEX IF NOT EXISTS idx_analysis_results_type
    ON public.analysis_results(analysis_type);

-- GIN index on metadata for JSON queries
CREATE INDEX IF NOT EXISTS idx_analysis_results_metadata
    ON public.analysis_results USING GIN (metadata);

-- GIN index on parameters for parameter-based queries
CREATE INDEX IF NOT EXISTS idx_analysis_results_parameters
    ON public.analysis_results USING GIN (parameters);

-- ============================================================================
-- Row Level Security (RLS) Policies
-- ============================================================================

-- Enable RLS
ALTER TABLE public.analysis_results ENABLE ROW LEVEL SECURITY;

-- Policy 1: Users can view their own analysis results
DROP POLICY IF EXISTS "Users can view own analysis results" ON public.analysis_results;
CREATE POLICY "Users can view own analysis results"
    ON public.analysis_results
    FOR SELECT
    USING (auth.uid()::text = user_id);

-- Policy 2: Users can insert their own analysis results
DROP POLICY IF EXISTS "Users can insert own analysis results" ON public.analysis_results;
CREATE POLICY "Users can insert own analysis results"
    ON public.analysis_results
    FOR INSERT
    WITH CHECK (auth.uid()::text = user_id);

-- Policy 3: Users can update their own analysis results
DROP POLICY IF EXISTS "Users can update own analysis results" ON public.analysis_results;
CREATE POLICY "Users can update own analysis results"
    ON public.analysis_results
    FOR UPDATE
    USING (auth.uid()::text = user_id)
    WITH CHECK (auth.uid()::text = user_id);

-- Policy 4: Service role has full access (for backend operations)
DROP POLICY IF EXISTS "Service role has full access to analysis" ON public.analysis_results;
CREATE POLICY "Service role has full access to analysis"
    ON public.analysis_results
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ============================================================================
-- Helper Functions
-- ============================================================================

-- Function to find recent matching analysis (for follow-up detection)
CREATE OR REPLACE FUNCTION find_recent_analysis(
    p_user_id TEXT,
    p_analysis_type TEXT,
    p_location TEXT,
    p_start_year INTEGER DEFAULT NULL,
    p_end_year INTEGER DEFAULT NULL,
    p_hours_ago INTEGER DEFAULT 24
)
RETURNS TABLE (
    analysis_session_id TEXT,
    storage_path TEXT,
    results_json_url TEXT,
    completed_at TIMESTAMP WITH TIME ZONE
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        ar.analysis_session_id,
        ar.storage_path,
        ar.results_json_url,
        ar.completed_at
    FROM public.analysis_results ar
    WHERE ar.user_id = p_user_id
        AND ar.analysis_type = p_analysis_type
        AND ar.location = p_location
        AND ar.status = 'completed'
        AND ar.completed_at > NOW() - (p_hours_ago || ' hours')::INTERVAL
        AND (p_start_year IS NULL OR ar.start_year = p_start_year)
        AND (p_end_year IS NULL OR ar.end_year = p_end_year)
    ORDER BY ar.completed_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

COMMENT ON FUNCTION find_recent_analysis IS
    'Finds the most recent completed analysis matching the given parameters within the specified time window (default 24 hours)';

-- Function to cleanup old failed/pending analyses
CREATE OR REPLACE FUNCTION cleanup_stale_analyses(hours_old INTEGER DEFAULT 24)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM public.analysis_results
    WHERE status IN ('pending', 'processing', 'failed')
        AND created_at < NOW() - (hours_old || ' hours')::INTERVAL;

    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

COMMENT ON FUNCTION cleanup_stale_analyses IS
    'Deletes stale pending/processing/failed analyses older than specified hours (default 24). Usage: SELECT cleanup_stale_analyses(24);';

-- ============================================================================
-- Table and column comments
-- ============================================================================

COMMENT ON TABLE public.analysis_results IS
    'Tracks all analysis executions with their results and storage locations. Used for follow-up detection to avoid redundant Earth Engine processing.';

COMMENT ON COLUMN public.analysis_results.analysis_session_id IS
    'Unique analysis execution ID format: YYYYMMDD_HHMMSS_hex (e.g., 20251113_063949_3d8f7b6dd691)';

COMMENT ON COLUMN public.analysis_results.conversation_session_id IS
    'Links to conversations table. NULL for analyses from legacy /analyze endpoint.';

COMMENT ON COLUMN public.analysis_results.storage_path IS
    'GCS bucket path where results are stored: analysis_results/{analysis_session_id}/';

COMMENT ON COLUMN public.analysis_results.status IS
    'Analysis status: pending (created), processing (Earth Engine running), completed (success), failed (error)';

COMMENT ON COLUMN public.analysis_results.metadata IS
    'Analysis-specific metadata including statistics, computed values, etc.';

-- ============================================================================
-- Verify table creation
-- ============================================================================

-- Run this to verify the table was created successfully
SELECT
    'analysis_results' AS table_name,
    COUNT(*) AS row_count,
    pg_size_pretty(pg_total_relation_size('public.analysis_results')) AS table_size
FROM public.analysis_results;

-- List all indexes
SELECT
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'analysis_results'
ORDER BY indexname;

-- Test the helper function
-- SELECT * FROM find_recent_analysis('test_user_id', 'vegetation_index', 'Zurich, Switzerland', 2020, 2024, 24);
