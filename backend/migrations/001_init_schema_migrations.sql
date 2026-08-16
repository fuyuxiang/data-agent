-- S1 Task 8: Initialize schema migrations tracking table
-- Migration: Create _meta.schema_migrations for version tracking
-- Date: 2026-08-14

BEGIN;

-- Create schema_migrations table to track applied migrations
CREATE TABLE IF NOT EXISTS _meta.schema_migrations (
    version INT PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create index for efficient lookups
CREATE INDEX IF NOT EXISTS idx_schema_migrations_applied_at
  ON _meta.schema_migrations(applied_at DESC);

-- Add comment for clarity
COMMENT ON TABLE _meta.schema_migrations IS 'Tracks database migration versions applied to this instance';
COMMENT ON COLUMN _meta.schema_migrations.version IS 'Sequential version number (e.g., 001, 002, 003)';
COMMENT ON COLUMN _meta.schema_migrations.name IS 'Migration name extracted from filename (e.g., init_schema_migrations)';
COMMENT ON COLUMN _meta.schema_migrations.applied_at IS 'Timestamp when migration was applied';

COMMIT;
