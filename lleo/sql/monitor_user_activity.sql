-- ==================== USER ACTIVITY MONITORING QUERIES ====================
-- Purpose: Monitor what users ask and what answers they receive in LLEO
-- Database: Supabase PostgreSQL
-- Tables: conversation_messages, analyses, auth.users

-- ==================== 1. VIEW ALL RECENT CONVERSATIONS ====================
-- Shows all user questions and LLEO answers with user email
SELECT
  cm.conversation_id,
  cm.created_at,
  u.email as user_email,
  CASE WHEN cm.role = 'user' THEN '👤 User' ELSE '🤖 LLEO' END as speaker,
  cm.content,
  cm.message_type
FROM conversation_messages cm
JOIN auth.users u ON cm.user_id = u.id
ORDER BY cm.created_at DESC
LIMIT 100;

-- ==================== 2. VIEW SPECIFIC USER'S QUESTIONS ====================
-- Replace 'USER_EMAIL_HERE' with actual email
SELECT
  cm.created_at,
  cm.content as question,
  cm.message_type
FROM conversation_messages cm
JOIN auth.users u ON cm.user_id = u.id
WHERE u.email = 'USER_EMAIL_HERE'
  AND cm.role = 'user'
ORDER BY cm.created_at DESC;

-- ==================== 3. VIEW SPECIFIC USER'S ANSWERS ====================
-- Replace 'USER_EMAIL_HERE' with actual email
SELECT
  cm.created_at,
  cm.content as answer,
  cm.message_type
FROM conversation_messages cm
JOIN auth.users u ON cm.user_id = u.id
WHERE u.email = 'USER_EMAIL_HERE'
  AND cm.role = 'assistant'
ORDER BY cm.created_at DESC;

-- ==================== 4. VIEW COMPLETE CONVERSATION THREAD ====================
-- Replace 'CONVERSATION_UUID' with actual conversation_id
SELECT
  cm.created_at,
  CASE WHEN cm.role = 'user' THEN '👤 User' ELSE '🤖 LLEO' END as speaker,
  cm.content,
  cm.message_type
FROM conversation_messages cm
WHERE cm.conversation_id = 'CONVERSATION_UUID'
ORDER BY cm.created_at ASC;

-- ==================== 5. CONVERSATION SUMMARY ====================
-- Groups conversations with question/answer counts
SELECT
  cm.conversation_id,
  u.email as user_email,
  COUNT(*) FILTER (WHERE cm.role = 'user') as questions_count,
  COUNT(*) FILTER (WHERE cm.role = 'assistant') as answers_count,
  MIN(cm.created_at) as started_at,
  MAX(cm.created_at) as last_message_at,
  AGE(MAX(cm.created_at), MIN(cm.created_at)) as conversation_duration
FROM conversation_messages cm
JOIN auth.users u ON cm.user_id = u.id
GROUP BY cm.conversation_id, u.email
ORDER BY last_message_at DESC
LIMIT 50;

-- ==================== 6. ANALYSIS REQUESTS BY TYPE ====================
-- Shows what types of analyses users are requesting
SELECT
  a.analysis_type,
  COUNT(*) as total_requests,
  COUNT(DISTINCT a.user_id) as unique_users,
  COUNT(*) FILTER (WHERE a.status = 'completed') as completed,
  COUNT(*) FILTER (WHERE a.status = 'failed') as failed,
  COUNT(*) FILTER (WHERE a.is_shared = true) as shared_to_community
FROM analyses a
GROUP BY a.analysis_type
ORDER BY total_requests DESC;

-- ==================== 7. USER ACTIVITY SUMMARY ====================
-- Overview of each user's activity
SELECT
  u.email,
  u.created_at as signup_date,
  COUNT(DISTINCT a.id) as total_analyses,
  COUNT(DISTINCT a.conversation_id) as total_conversations,
  COUNT(DISTINCT a.id) FILTER (WHERE a.is_shared = true) as shared_analyses,
  MAX(a.created_at) as last_activity,
  AGE(NOW(), MAX(a.created_at)) as time_since_last_activity
FROM auth.users u
LEFT JOIN analyses a ON u.id = a.user_id
GROUP BY u.id, u.email, u.created_at
ORDER BY last_activity DESC NULLS LAST;

-- ==================== 8. RECENT ANALYSIS REQUESTS ====================
-- Shows recent user questions with analysis type and status
SELECT
  a.created_at,
  u.email as user_email,
  a.query as user_question,
  a.analysis_type,
  a.status,
  a.is_shared,
  CASE
    WHEN a.map_url IS NOT NULL THEN '✅ Has Map'
    ELSE '❌ No Map'
  END as map_status,
  CASE
    WHEN a.chart_url IS NOT NULL THEN '✅ Has Chart'
    ELSE '❌ No Chart'
  END as chart_status
FROM analyses a
JOIN auth.users u ON a.user_id = u.id
ORDER BY a.created_at DESC
LIMIT 50;

-- ==================== 9. POPULAR SEARCH LOCATIONS ====================
-- Shows which locations users are most interested in
SELECT
  a.location_lat,
  a.location_lng,
  COUNT(*) as request_count,
  COUNT(DISTINCT a.user_id) as unique_users,
  array_agg(DISTINCT a.analysis_type) as analysis_types,
  MAX(a.created_at) as last_requested
FROM analyses a
WHERE a.location_lat IS NOT NULL
  AND a.location_lng IS NOT NULL
GROUP BY a.location_lat, a.location_lng
HAVING COUNT(*) > 1
ORDER BY request_count DESC
LIMIT 20;

-- ==================== 10. CONVERSATIONS TODAY ====================
-- Shows all conversations from today
SELECT
  cm.conversation_id,
  u.email as user_email,
  COUNT(*) as message_count,
  MIN(cm.created_at) as started_at,
  MAX(cm.created_at) as last_message_at
FROM conversation_messages cm
JOIN auth.users u ON cm.user_id = u.id
WHERE cm.created_at >= CURRENT_DATE
GROUP BY cm.conversation_id, u.email
ORDER BY started_at DESC;

-- ==================== 11. FAILED ANALYSES (NEEDS ATTENTION) ====================
-- Shows analyses that failed - useful for debugging
SELECT
  a.created_at,
  u.email as user_email,
  a.query as user_question,
  a.analysis_type,
  a.status,
  a.error_message
FROM analyses a
JOIN auth.users u ON a.user_id = u.id
WHERE a.status = 'failed'
ORDER BY a.created_at DESC
LIMIT 30;

-- ==================== 12. MOST ACTIVE USERS ====================
-- Shows users who use the system most
SELECT
  u.email,
  COUNT(DISTINCT a.id) as total_analyses,
  COUNT(DISTINCT a.conversation_id) as total_conversations,
  COUNT(DISTINCT DATE(a.created_at)) as active_days,
  MIN(a.created_at) as first_analysis,
  MAX(a.created_at) as last_analysis
FROM auth.users u
JOIN analyses a ON u.id = a.user_id
GROUP BY u.id, u.email
ORDER BY total_analyses DESC
LIMIT 20;

-- ==================== 13. CREATE MONITORING VIEW (RUN ONCE) ====================
-- Creates a permanent view for easy monitoring
CREATE OR REPLACE VIEW user_activity_dashboard AS
SELECT
  u.id as user_id,
  u.email,
  u.created_at as signup_date,
  COUNT(DISTINCT a.id) as total_analyses,
  COUNT(DISTINCT a.conversation_id) as total_conversations,
  COUNT(DISTINCT a.id) FILTER (WHERE a.is_shared = true) as shared_analyses,
  COUNT(DISTINCT a.id) FILTER (WHERE a.status = 'completed') as successful_analyses,
  COUNT(DISTINCT a.id) FILTER (WHERE a.status = 'failed') as failed_analyses,
  MAX(a.created_at) as last_activity,
  AGE(NOW(), MAX(a.created_at)) as time_since_last_activity
FROM auth.users u
LEFT JOIN analyses a ON u.id = a.user_id
GROUP BY u.id, u.email, u.created_at
ORDER BY last_activity DESC NULLS LAST;

-- After creating the view, simply query:
-- SELECT * FROM user_activity_dashboard;

-- ==================== 14. EXPORT CONVERSATION TO TEXT ====================
-- Export a full conversation in readable format
-- Replace 'CONVERSATION_UUID' with actual conversation_id
SELECT
  TO_CHAR(cm.created_at, 'YYYY-MM-DD HH24:MI:SS') as timestamp,
  CASE
    WHEN cm.role = 'user' THEN 'USER: '
    ELSE 'LLEO: '
  END || cm.content as message
FROM conversation_messages cm
WHERE cm.conversation_id = 'CONVERSATION_UUID'
ORDER BY cm.created_at ASC;

-- ==================== 15. SEARCH CONVERSATIONS BY KEYWORD ====================
-- Find conversations containing specific keywords
-- Replace 'KEYWORD' with what you're searching for
SELECT DISTINCT
  cm.conversation_id,
  u.email as user_email,
  cm.content,
  cm.created_at
FROM conversation_messages cm
JOIN auth.users u ON cm.user_id = u.id
WHERE cm.content ILIKE '%KEYWORD%'
ORDER BY cm.created_at DESC
LIMIT 50;

-- ==================== USAGE INSTRUCTIONS ====================
-- 1. Copy the query you want to run
-- 2. Go to Supabase Dashboard > SQL Editor
-- 3. Paste the query
-- 4. Replace placeholders (USER_EMAIL_HERE, CONVERSATION_UUID, KEYWORD)
-- 5. Click "Run"
--
-- For the view (Query 13), run it once to create the permanent view.
-- After that, you can simply run: SELECT * FROM user_activity_dashboard;
