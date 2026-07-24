-- Non-superuser application role: RLS policies do NOT apply to superusers or
-- table owners, so the app must never connect as `postgres`.
-- Migrations (DDL) run as `postgres`; the app connects as `app_user`.
--
-- Written to be idempotent: compose runs it once via /docker-entrypoint-initdb.d,
-- but CI invokes it explicitly under ON_ERROR_STOP=1, where a re-run against a
-- warm database must not abort the job. Postgres has no CREATE ROLE IF NOT
-- EXISTS, hence the DO block; CREATE DATABASE cannot run inside one, hence the
-- \gexec guard below.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN PASSWORD 'app_password';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE realestate TO app_user;

\connect realestate

GRANT USAGE ON SCHEMA public TO app_user;
-- Future tables created by migrations become readable/writable by the app role.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_user;

-- Separate database for the test suite (same roles, wiped freely).
SELECT 'CREATE DATABASE realestate_test OWNER postgres'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'realestate_test')
\gexec

GRANT CONNECT ON DATABASE realestate_test TO app_user;

\connect realestate_test

CREATE EXTENSION IF NOT EXISTS postgis;
GRANT USAGE ON SCHEMA public TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_user;
