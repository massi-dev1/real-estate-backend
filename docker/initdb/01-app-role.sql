-- Non-superuser application role: RLS policies do NOT apply to superusers or
-- table owners, so the app must never connect as `postgres`.
-- Migrations (DDL) run as `postgres`; the app connects as `app_user`.
CREATE ROLE app_user LOGIN PASSWORD 'app_password';

GRANT CONNECT ON DATABASE realestate TO app_user;

\connect realestate

GRANT USAGE ON SCHEMA public TO app_user;
-- Future tables created by migrations become readable/writable by the app role.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_user;

-- Separate database for the test suite (same roles, wiped freely).
CREATE DATABASE realestate_test OWNER postgres;
GRANT CONNECT ON DATABASE realestate_test TO app_user;

\connect realestate_test

CREATE EXTENSION IF NOT EXISTS postgis;
GRANT USAGE ON SCHEMA public TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO app_user;
