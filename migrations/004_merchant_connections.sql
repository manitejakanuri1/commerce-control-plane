-- Level 2: a read-only connection into a merchant's own database.
--
-- Level 1 needs nothing from a merchant but a price list and a discount cap.
-- Level 2 is for merchants who want more: real margin enforcement, live stock,
-- and recommendations informed by what a shopper has bought before.
--
-- The credential is NOT stored here.
--
-- `dsn_env_var` holds the *name* of an environment variable, never its value.
-- If this database is breached, the attacker learns that a merchant has a
-- connection and what its variable is called, and cannot use it. The
-- credentials live in the deployment's secrets manager, which is the only
-- place they should ever exist.
--
-- `column_map` is per-merchant schema translation. No two shops name things
-- the same way, so the mapping is data rather than code and onboarding a
-- merchant does not mean a deploy.

CREATE TABLE IF NOT EXISTS merchant_connections (
    merchant_id     TEXT PRIMARY KEY REFERENCES merchants(id) ON DELETE CASCADE,

    -- Name of the environment variable holding the read-only DSN.
    dsn_env_var     TEXT        NOT NULL,

    -- Which of their tables hold what, and which columns mean what.
    -- See connectors/postgres.py for the shape and the defaults.
    column_map      JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- What this merchant has actually granted. Each is verified on connect
    -- rather than trusted, and a capability that fails verification is
    -- switched off rather than assumed.
    can_read_cost   BOOLEAN     NOT NULL DEFAULT FALSE,
    can_read_stock  BOOLEAN     NOT NULL DEFAULT FALSE,
    can_read_orders BOOLEAN     NOT NULL DEFAULT FALSE,

    active          BOOLEAN     NOT NULL DEFAULT TRUE,
    last_verified_at TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT dsn_env_var_shape CHECK (dsn_env_var ~ '^[A-Z][A-Z0-9_]{2,63}$')
);

ALTER TABLE merchant_connections ENABLE ROW LEVEL SECURITY;

-- Derived shopper features, never raw customer records.
--
-- A recommendation needs to know that somebody buys electronics around
-- Rs 2,000 and already owns a pair of earbuds. It does not need their name,
-- their email, or what they paid. Storing the derived form rather than the
-- source is both the safer design and less personal data to be liable for.
CREATE TABLE IF NOT EXISTS buyer_profiles (
    merchant_id        TEXT        NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,

    -- A stable pseudonym: HMAC of the merchant's customer id under a server
    -- secret. Lets a returning shopper be recognised without this table
    -- holding anything that identifies them.
    buyer_ref          TEXT        NOT NULL,

    categories         TEXT[]      NOT NULL DEFAULT '{}',
    owned_skus         TEXT[]      NOT NULL DEFAULT '{}',
    typical_low_paise  BIGINT,
    typical_high_paise BIGINT,
    order_count        INTEGER     NOT NULL DEFAULT 0,
    last_order_at      TIMESTAMPTZ,
    refreshed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (merchant_id, buyer_ref)
);

ALTER TABLE buyer_profiles ENABLE ROW LEVEL SECURITY;

CREATE INDEX IF NOT EXISTS buyer_profiles_stale_idx
    ON buyer_profiles (refreshed_at);
