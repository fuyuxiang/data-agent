-- S1 Task 8: Add tenant and state tracking columns
-- Migration: Add tenant_id and state_version to conversations, enforce user_id FK
-- Date: 2026-08-13

BEGIN;

-- conversations table: add tenant tracking and state versioning
ALTER TABLE _meta.conversations
  ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(64) DEFAULT 'default' NOT NULL,
  ADD COLUMN IF NOT EXISTS state_version INT DEFAULT 0 NOT NULL;

-- Add foreign key constraint from conversations.user_id to users.id
ALTER TABLE _meta.conversations
  ADD CONSTRAINT fk_conversations_user_id
    FOREIGN KEY (user_id) REFERENCES _meta.users(id) ON DELETE CASCADE;

-- Add indexes for query performance
CREATE INDEX IF NOT EXISTS idx_conversations_user_id
  ON _meta.conversations(user_id);

CREATE INDEX IF NOT EXISTS idx_conversations_tenant_id
  ON _meta.conversations(tenant_id);

CREATE INDEX IF NOT EXISTS idx_conversations_created_at
  ON _meta.conversations(created_at DESC);

COMMIT;
