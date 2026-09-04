-- Decisions reported by policy engines running on merchants' own servers.
--
-- The engine is a package installed on their machine. We cannot reach into it
-- to fix a fault, and we cannot see their cost — which is the point. What
-- arrives here is the summary they choose to send: which product, what
-- discount was asked for, what the band allowed, which rule blocked it.
--
-- Deliberately NOT in the `audit` table. That one is hash-chained and
-- append-only because every row is something we did. These rows are things
-- somebody else says they did, over the internet, with a key that could have
-- leaked. Chaining an untrusted report would lend it a credibility it has not
-- earned, and mixing the two would let a merchant's traffic volume bury our
-- own record of a payment.
--
-- The merchant's own policy.decisions table is the authoritative chained copy.
-- This is a shadow of it, kept so the version gate and the repair agent have
-- something to read.

CREATE TABLE IF NOT EXISTS policy_decisions (
    seq             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- Always resolved from the API key, never taken from the request body.
    -- A merchant does not get to say which merchant they are.
    merchant_id     TEXT        NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,

    -- When the engine says it happened, and when it reached us. They differ
    -- when a merchant's server was offline and the queue drained late, and
    -- the gap is itself worth seeing.
    at              TIMESTAMPTZ NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

    sku             TEXT        NOT NULL,
    asked_bps       INTEGER     NOT NULL,
    allowed_bps     INTEGER     NOT NULL,
    result          TEXT        NOT NULL CHECK (result IN ('approved', 'refused')),
    failed_rules    TEXT[]      NOT NULL DEFAULT '{}',
    engine_version  TEXT        NOT NULL,

    CONSTRAINT asked_bps_sane   CHECK (asked_bps   BETWEEN 0 AND 10000),
    CONSTRAINT allowed_bps_sane CHECK (allowed_bps BETWEEN 0 AND 10000)
);

-- The three questions asked of this table: what did this merchant do lately,
-- who is on an old release, and which rule is costing them the most sales.
CREATE INDEX IF NOT EXISTS policy_decisions_merchant_idx
    ON policy_decisions (merchant_id, at DESC);

CREATE INDEX IF NOT EXISTS policy_decisions_version_idx
    ON policy_decisions (engine_version);

CREATE INDEX IF NOT EXISTS policy_decisions_refused_idx
    ON policy_decisions (merchant_id, at DESC) WHERE result = 'refused';

ALTER TABLE policy_decisions ENABLE ROW LEVEL SECURITY;
