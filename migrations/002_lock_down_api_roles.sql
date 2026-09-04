-- Close the PostgREST exposure.
--
-- Supabase publishes every table in `public` through PostgREST, reachable with
-- the publishable key, which is public by design. On a fresh project the
-- `anon` and `authenticated` roles are granted full DML on new tables, and Row
-- Level Security is off until you turn it on.
--
-- For this service that combination is fatal rather than merely untidy. The
-- policy engine reads its limits from `merchants` and its prices from
-- `products`. If those tables are writable from the public API, the gate
-- still runs correctly and simply enforces whatever an attacker stored:
--
--     UPDATE products  SET price_paise    = 1;
--     UPDATE merchants SET max_discount_bps = 10000;
--
-- Every guarantee the architecture makes is downstream of this data, so the
-- data must not be reachable except through the application.
--
-- This service does not use PostgREST at all. It connects as the database
-- owner over the pooler and enforces tenancy in SQL, so revoking these grants
-- removes an attack surface it never used.

REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM anon, authenticated;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM anon, authenticated;
REVOKE USAGE ON SCHEMA public FROM anon, authenticated;

-- Future tables must not silently reappear on the public API. Without this,
-- the next migration that creates a table reopens the hole.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON TABLES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON SEQUENCES FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE ALL ON FUNCTIONS FROM anon, authenticated;

-- Second layer. With RLS enabled and no policies defined, every row is denied
-- to any role that is not the table owner or a BYPASSRLS role. If a future
-- grant is made by mistake, this still refuses the read.
ALTER TABLE merchants        ENABLE ROW LEVEL SECURITY;
ALTER TABLE products         ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders           ENABLE ROW LEVEL SECURITY;
ALTER TABLE reservations     ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_events   ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit            ENABLE ROW LEVEL SECURITY;
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
