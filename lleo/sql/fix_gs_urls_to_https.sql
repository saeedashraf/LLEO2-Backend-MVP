-- Fix existing gs:// URLs to backend API URLs for private access control
-- Run this in Supabase SQL Editor

UPDATE analyses
SET
  map_url = REPLACE(REPLACE(map_url, 'gs://ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'), 'gs://', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'),
  chart_url = REPLACE(REPLACE(chart_url, 'gs://ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'), 'gs://', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'),
  charts_url = REPLACE(REPLACE(charts_url, 'gs://ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'), 'gs://', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'),
  thumbnail_url = REPLACE(REPLACE(thumbnail_url, 'gs://ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'), 'gs://', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'),
  preview_url = REPLACE(REPLACE(preview_url, 'gs://ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'), 'gs://', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/')
WHERE
  map_url LIKE 'gs://%'
  OR chart_url LIKE 'gs://%'
  OR charts_url LIKE 'gs://%'
  OR thumbnail_url LIKE 'gs://%'
  OR preview_url LIKE 'gs://%';

-- Verify the update
SELECT
  session_id,
  query,
  map_url,
  chart_url,
  thumbnail_url
FROM analyses
ORDER BY created_at DESC
LIMIT 5;
