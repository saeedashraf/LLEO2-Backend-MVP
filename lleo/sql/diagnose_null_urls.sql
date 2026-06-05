-- Diagnose why 111 records are not showing up in any URL format count
-- This will help us understand what's in those old records

-- Check for NULL values
SELECT
  'NULL map_url' as issue,
  COUNT(*) as count
FROM analyses
WHERE map_url IS NULL
UNION ALL
SELECT
  'NULL chart_url' as issue,
  COUNT(*) as count
FROM analyses
WHERE chart_url IS NULL
UNION ALL
SELECT
  'Empty string map_url' as issue,
  COUNT(*) as count
FROM analyses
WHERE map_url = ''
UNION ALL
SELECT
  'Empty string chart_url' as issue,
  COUNT(*) as count
FROM analyses
WHERE chart_url = '';

-- Show sample of records with NULL URLs
SELECT
  session_id,
  query,
  analysis_type,
  map_url,
  chart_url,
  thumbnail_url,
  created_at
FROM analyses
WHERE map_url IS NULL OR chart_url IS NULL
ORDER BY created_at DESC
LIMIT 10;

-- Count records by analysis type
SELECT
  analysis_type,
  COUNT(*) as count,
  COUNT(map_url) as has_map_url,
  COUNT(chart_url) as has_chart_url
FROM analyses
GROUP BY analysis_type
ORDER BY count DESC;
