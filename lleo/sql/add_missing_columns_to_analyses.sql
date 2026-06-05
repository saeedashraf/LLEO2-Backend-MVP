-- Add missing columns to Supabase 'analyses' table
-- Run this in Supabase SQL Editor

ALTER TABLE analyses
ADD COLUMN IF NOT EXISTS analysis_session_id text,
ADD COLUMN IF NOT EXISTS conversation_session_id text,
ADD COLUMN IF NOT EXISTS location_lat double precision,
ADD COLUMN IF NOT EXISTS location_lng double precision,
ADD COLUMN IF NOT EXISTS start_year integer,
ADD COLUMN IF NOT EXISTS end_year integer,
ADD COLUMN IF NOT EXISTS specific_months jsonb,
ADD COLUMN IF NOT EXISTS parameters jsonb,
ADD COLUMN IF NOT EXISTS storage_path text,
ADD COLUMN IF NOT EXISTS bucket_name text,
ADD COLUMN IF NOT EXISTS charts_url text,
ADD COLUMN IF NOT EXISTS preview_url text,
ADD COLUMN IF NOT EXISTS results_json_url text,
ADD COLUMN IF NOT EXISTS status text DEFAULT 'completed',
ADD COLUMN IF NOT EXISTS completed_at timestamptz,
ADD COLUMN IF NOT EXISTS metadata jsonb,
ADD COLUMN IF NOT EXISTS processing_time_seconds double precision;

-- Add comments
COMMENT ON COLUMN analyses.analysis_session_id IS 'Session ID for the analysis';
COMMENT ON COLUMN analyses.conversation_session_id IS 'Conversation session ID if from chat';
COMMENT ON COLUMN analyses.location_lat IS 'Latitude of analysis location';
COMMENT ON COLUMN analyses.location_lng IS 'Longitude of analysis location';
COMMENT ON COLUMN analyses.start_year IS 'Start year of analysis period';
COMMENT ON COLUMN analyses.end_year IS 'End year of analysis period';
COMMENT ON COLUMN analyses.storage_path IS 'GCS storage path for analysis files';
COMMENT ON COLUMN analyses.bucket_name IS 'GCS bucket name';
COMMENT ON COLUMN analyses.metadata IS 'Analysis metadata from Earth Engine';
