-- Fix existing public GCS URLs to backend API URLs for private access control
-- Run this in Supabase SQL Editor

UPDATE analyses
SET
  map_url = REPLACE(map_url, 'https://storage.googleapis.com/ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'),
  chart_url = REPLACE(chart_url, 'https://storage.googleapis.com/ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'),
  charts_url = REPLACE(charts_url, 'https://storage.googleapis.com/ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'),
  thumbnail_url = REPLACE(thumbnail_url, 'https://storage.googleapis.com/ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/'),
  preview_url = REPLACE(preview_url, 'https://storage.googleapis.com/ndvi3-analysis-storage-bucket/analysis_results/', 'https://ndvi5-analysis-api-258557095482.us-central1.run.app/files/')
WHERE
  map_url LIKE 'https://storage.googleapis.com/%'
  OR chart_url LIKE 'https://storage.googleapis.com/%'
  OR charts_url LIKE 'https://storage.googleapis.com/%'
  OR thumbnail_url LIKE 'https://storage.googleapis.com/%'
  OR preview_url LIKE 'https://storage.googleapis.com/%';

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
