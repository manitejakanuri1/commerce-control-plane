-- Operational events: what shoppers asked for, and what came back.
--
-- Kept apart from `audit` on purpose. Audit rows are money decisions: rare,
-- chained, append-only, kept forever. These are the opposite — thousands a
-- day, none individually important, and worthless after a month. Putting a
-- search log in a hash-chained table costs a SHA-256 and a serialised write
-- per keystroke's worth of traffic, and buries the rows that matter under the
-- rows that do not.
--
-- The reason this table exists at all is one column: `query` on a search that
-- returned nothing. That is demand the merchant is failing to meet, and it is
-- invisible everywhere else. Razorpay cannot see it — those shoppers never
-- reached checkout. The merchant's own search cannot see it — it returned
-- "no results" and forgot.

CREATE TABLE IF NOT EXISTS events (
    seq          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    merchant_id  TEXT        NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,

    -- 'search', 'propose', 'purchase_started', 'widget_shown'
    kind         TEXT        NOT NULL,

    -- The shopper's own words, redacted before storage. Null for kinds that
    -- have no query.
    query        TEXT,

    -- How many products came back. Zero is the interesting value.
    results      INTEGER,

    duration_ms  INTEGER,
    detail       JSONB       NOT NULL DEFAULT '{}'::jsonb
);

-- The three reads: this merchant's recent activity, unmet demand, and the
-- retention sweep.
CREATE INDEX IF NOT EXISTS events_merchant_idx
    ON events (merchant_id, at DESC);

CREATE INDEX IF NOT EXISTS events_unmet_idx
    ON events (merchant_id, at DESC)
    WHERE kind = 'search' AND results = 0;

CREATE INDEX IF NOT EXISTS events_at_idx ON events (at);

-- No append-only trigger and no hash chain, deliberately. Nothing here is
-- evidence of anything; it is telemetry, and it gets deleted on a schedule.

ALTER TABLE events ENABLE ROW LEVEL SECURITY;
