-- Clean up incomplete/broken analyses with NULL URLs
-- Run this in Supabase SQL Editor

-- First, check how many will be deleted
SELECT COUNT(*) as records_to_delete
FROM analyses
WHERE map_url IS NULL AND chart_url IS NULL;

-- Delete the incomplete records
DELETE FROM analyses
WHERE map_url IS NULL AND chart_url IS NULL;

-- Verify the cleanup
SELECT
  COUNT(*) as total_records,
  COUNT(map_url) as records_with_map,
  COUNT(chart_url) as records_with_chart
FROM analyses;

-- Show remaining analyses
SELECT
  session_id,
  query,
  analysis_type,
  map_url,
  created_at
FROM analyses
ORDER BY created_at DESC
LIMIT 10;
