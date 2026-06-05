-- Verify Conversation Grouping in Supabase
-- Run this in Supabase SQL Editor to see how analyses are grouped by conversation

-- 1. Show all conversations with their analysis counts
SELECT
  conversation_id,
  COUNT(*) as analysis_count,
  STRING_AGG(query, ' → ' ORDER BY created_at) as conversation_flow,
  MIN(created_at) as conversation_started,
  MAX(created_at) as last_activity
FROM analyses
WHERE conversation_id IS NOT NULL
GROUP BY conversation_id
ORDER BY last_activity DESC;

-- 2. Show detailed view of each conversation
SELECT
  conversation_id,
  session_id,
  query,
  analysis_type,
  map_url,
  chart_url,
  thumbnail_url,
  created_at,
  ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY created_at) as question_number
FROM analyses
WHERE conversation_id IS NOT NULL
ORDER BY conversation_id, created_at;

-- 3. Find standalone analyses (no conversation grouping)
SELECT
  session_id,
  query,
  analysis_type,
  created_at
FROM analyses
WHERE conversation_id IS NULL
ORDER BY created_at DESC;

-- 4. Count by grouping status
SELECT
  CASE
    WHEN conversation_id IS NOT NULL THEN 'Grouped in conversation'
    ELSE 'Standalone'
  END as grouping_status,
  COUNT(*) as count
FROM analyses
GROUP BY grouping_status;

-- 5. Show most active conversations (most questions/analyses)
SELECT
  conversation_id,
  COUNT(*) as total_analyses,
  STRING_AGG(DISTINCT analysis_type, ', ') as analysis_types_used,
  MIN(created_at) as started,
  MAX(created_at) as last_updated
FROM analyses
WHERE conversation_id IS NOT NULL
GROUP BY conversation_id
HAVING COUNT(*) > 1  -- Only show conversations with multiple analyses
ORDER BY total_analyses DESC, last_updated DESC;
