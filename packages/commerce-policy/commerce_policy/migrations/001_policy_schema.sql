-- The policy schema.
--
-- Kept apart from the storefront's own tables so that the separation can be
-- enforced by a missing grant rather than by careful coding. See grants.sql,
-- which is applied by `commerce-policy migrate --storefront-role ... `.
--
-- Safe to re-run.

CREATE SCHEMA IF NOT EXISTS policy;

-- What a product actually costs, and how low it may ever go.
--
-- A merchant who will not disclose cost can populate floor_price_paise alone.
-- That is a derived number, it reveals nothing about their supplier, and it
-- still stops a discount from running away. Margin simply reports as
-- not_configured, which is honest.
CREATE TABLE IF NOT EXISTS policy.economics (
    sku                TEXT PRIMARY KEY,
    cost_paise         BIGINT CHECK (cost_paise >= 0),
    floor_price_paise  BIGINT CHECK (floor_price_paise > 0),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Money is stored as whole paise. A NUMERIC would invite a decimal, and a
-- decimal invites a float somewhere upstream, which eventually rounds a sale
-- in somebody's favour and is noticed months later.

CREATE TABLE IF NOT EXISTS policy.rules (
    merchant_id       TEXT PRIMARY KEY,
    max_discount_bps  INTEGER NOT NULL DEFAULT 1000
                      CHECK (max_discount_bps BETWEEN 0 AND 9999),
    min_margin_bps    INTEGER NOT NULL DEFAULT 2000
                      CHECK (min_margin_bps BETWEEN 0 AND 9999),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every decision, chained.
--
-- This is the merchant's own record, on the merchant's own server. The
-- control plane receives a copy of the safe fields; this table is the
-- original, and it outlives any relationship with us.
CREATE TABLE IF NOT EXISTS policy.decisions (
    seq             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    merchant_id     TEXT NOT NULL,
    sku             TEXT NOT NULL,
    asked_bps       INTEGER NOT NULL,
    allowed_bps     INTEGER NOT NULL,
    result          TEXT NOT NULL CHECK (result IN ('approved', 'refused')),
    failed_rules    TEXT[] NOT NULL DEFAULT '{}',
    engine_version  TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS decisions_at_idx ON policy.decisions (at DESC);

-- Append-only. The chain makes tampering detectable; this makes it awkward.
-- Neither stops somebody with superuser rights, so the honest description is
-- tamper-evident, not tamper-proof.
CREATE OR REPLACE FUNCTION policy.decisions_are_append_only()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'policy.decisions is append-only; % refused', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS decisions_append_only ON policy.decisions;
CREATE TRIGGER decisions_append_only
    BEFORE UPDATE OR DELETE ON policy.decisions
    FOR EACH ROW EXECUTE FUNCTION policy.decisions_are_append_only();
