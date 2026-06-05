-- Verify that URLs have been converted correctly to backend API format
-- Run this in Supabase SQL Editor to check the status of URL conversion

SELECT
  session_id,
  query,
  -- Show map_url and check if it's using backend API
  map_url,
  CASE
    WHEN map_url LIKE 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/%' THEN '✅ Backend API'
    WHEN map_url LIKE 'https://storage.googleapis.com/%' THEN '❌ Public GCS'
    WHEN map_url LIKE 'gs://%' THEN '❌ gs:// format'
    ELSE '⚠️ Unknown format'
  END as map_url_status,
  -- Show chart_url and check format
  chart_url,
  CASE
    WHEN chart_url LIKE 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/%' THEN '✅ Backend API'
    WHEN chart_url LIKE 'https://storage.googleapis.com/%' THEN '❌ Public GCS'
    WHEN chart_url LIKE 'gs://%' THEN '❌ gs:// format'
    ELSE '⚠️ Unknown format'
  END as chart_url_status,
  -- Show thumbnail status
  CASE
    WHEN thumbnail_url IS NULL THEN 'NULL (expected for non-EE)'
    WHEN thumbnail_url LIKE 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/%' THEN '✅ Backend API'
    WHEN thumbnail_url LIKE 'https://storage.googleapis.com/%' THEN '❌ Public GCS'
    WHEN thumbnail_url LIKE 'gs://%' THEN '❌ gs:// format'
    ELSE '⚠️ Unknown format'
  END as thumbnail_status,
  created_at
FROM analyses
ORDER BY created_at DESC
LIMIT 10;

-- Summary count by URL format
SELECT
  'Map URLs' as url_type,
  COUNT(*) FILTER (WHERE map_url LIKE 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/%') as backend_api_count,
  COUNT(*) FILTER (WHERE map_url LIKE 'https://storage.googleapis.com/%') as public_gcs_count,
  COUNT(*) FILTER (WHERE map_url LIKE 'gs://%') as gs_format_count,
  COUNT(*) as total
FROM analyses
UNION ALL
SELECT
  'Chart URLs' as url_type,
  COUNT(*) FILTER (WHERE chart_url LIKE 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/%') as backend_api_count,
  COUNT(*) FILTER (WHERE chart_url LIKE 'https://storage.googleapis.com/%') as public_gcs_count,
  COUNT(*) FILTER (WHERE chart_url LIKE 'gs://%') as gs_format_count,
  COUNT(*) as total
FROM analyses;
