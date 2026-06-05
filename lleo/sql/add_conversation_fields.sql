-- Migration: Add conversation support fields to analyses table
-- Date: 2025-01-09
-- Purpose: Enable tracking of multi-turn conversations and linking related analyses

-- Add conversation_id to link analyses from same conversation
ALTER TABLE analyses
ADD COLUMN IF NOT EXISTS conversation_id TEXT;

-- Add conversation_context to store full conversation history
ALTER TABLE analyses
ADD COLUMN IF NOT EXISTS conversation_context JSONB;

-- Create index for faster lookups by conversation_id
CREATE INDEX IF NOT EXISTS idx_analyses_conversation_id
ON analyses(conversation_id);

-- Add comment for documentation
COMMENT ON COLUMN analyses.conversation_id IS 'Links multiple analyses from the same conversation session';
COMMENT ON COLUMN analyses.conversation_context IS 'Stores conversation history and context as JSON';

-- Example query to find all analyses from a conversation:
-- SELECT * FROM analyses WHERE conversation_id = 'abc123-456' ORDER BY created_at;
