-- ============================================================================
-- Database roles. Guardrail layer 2: the read-only role is what actually makes
-- a destructive generated query impossible, independent of any application
-- logic. The AST validator in the server is layer 1; either layer alone is
-- sufficient to block a write.
--
-- Run as a superuser against the agri_insights database.
-- ============================================================================

-- agent_ro executes every model-generated query. It has SELECT and nothing else.
--
-- The password here is a local development credential for a role that cannot
-- write anything, on a database that holds only sample data. Set it from the
-- environment before using this anywhere real.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
        CREATE ROLE agent_ro LOGIN PASSWORD 'agent_ro';
    END IF;
END
$$;

REVOKE ALL ON SCHEMA agri FROM agent_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA agri FROM agent_ro;

GRANT CONNECT ON DATABASE agri_insights TO agent_ro;
GRANT USAGE  ON SCHEMA agri TO agent_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA agri TO agent_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA agri GRANT SELECT ON TABLES TO agent_ro;

-- No write path anywhere, including the public schema and temp objects.
REVOKE CREATE ON SCHEMA public FROM agent_ro;
REVOKE TEMPORARY ON DATABASE agri_insights FROM agent_ro;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- Belt and braces: even a bug in the app's session setup cannot make this role
-- write, and a runaway query dies on its own.
ALTER ROLE agent_ro SET default_transaction_read_only = on;
ALTER ROLE agent_ro SET statement_timeout = '5s';
ALTER ROLE agent_ro SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE agent_ro SET search_path = agri, pg_catalog;
