-- Create the read-only DB role used for executing generated SQL.
--
-- Layered defence: the AST guardrails in `app/security/whitelist.py` are
-- layer one; this role is layer two. Even if the AST is bypassed, the
-- warehouse role physically cannot INSERT, UPDATE, DELETE, or DDL.
--
-- Apply once per environment:
--   psql "$SAMPLE_DATABASE_URL_ADMIN" -f scripts/create_reader_role.sql
--
-- Then point the runtime at the new role:
--   SAMPLE_DATABASE_URL=postgresql+psycopg2://data_agent_reader:***@host/db

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'data_agent_reader') THEN
        CREATE ROLE data_agent_reader LOGIN PASSWORD 'change-me';
    END IF;
END $$;

-- Sample business data lives under its own schema; grant only what the
-- generated queries need: USAGE on the schema, SELECT on tables.
GRANT CONNECT ON DATABASE current_database() TO data_agent_reader;
GRANT USAGE ON SCHEMA sample TO data_agent_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sample TO data_agent_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA sample
    GRANT SELECT ON TABLES TO data_agent_reader;

-- Strip any privilege that would let a mis-connection escape read-only:
-- CREATE on schema lets the role add tables; TEMPORARY on database lets it
-- stage rows for COPY-style injection. Both are explicitly REVOKEd so even
-- a future grant in code cannot resurrect them silently.
REVOKE CREATE ON SCHEMA public FROM data_agent_reader;
REVOKE CREATE ON SCHEMA sample FROM data_agent_reader;
REVOKE TEMPORARY ON DATABASE current_database() FROM data_agent_reader;