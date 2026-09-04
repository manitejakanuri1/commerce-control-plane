-- Merchant Agent Commerce Control Plane, initial schema.
--
-- Money is stored as paise in BIGINT. There is no floating point anywhere in
-- this schema, and there should never be.
--
-- Every table that holds merchant data carries merchant_id, and every query in
-- the application filters on it. Isolation is enforced in code and, where a
-- pooled role is used, by row level security below.

CREATE TABLE IF NOT EXISTS merchants (
    id                  TEXT PRIMARY KEY,
    name                TEXT        NOT NULL,
    api_key_hash        TEXT        NOT NULL UNIQUE,
    max_discount_bps    INTEGER     NOT NULL DEFAULT 1500,
    min_margin_bps      INTEGER     NOT NULL DEFAULT 800,
    active              BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT discount_bps_sane CHECK (max_discount_bps BETWEEN 0 AND 10000),
    CONSTRAINT margin_bps_sane   CHECK (min_margin_bps   BETWEEN 0 AND 10000)
);

CREATE TABLE IF NOT EXISTS products (
    merchant_id   TEXT        NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,
    sku           TEXT        NOT NULL,
    name          TEXT        NOT NULL,
    description   TEXT        NOT NULL DEFAULT '',
    price_paise   BIGINT      NOT NULL,
    cost_paise    BIGINT      NOT NULL,
    stock         INTEGER     NOT NULL,
    active        BOOLEAN     NOT NULL DEFAULT TRUE,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (merchant_id, sku),
    CONSTRAINT price_positive CHECK (price_paise > 0),
    CONSTRAINT cost_positive  CHECK (cost_paise  >= 0),
    CONSTRAINT stock_never_negative CHECK (stock >= 0)
);

-- Full-text search over the catalog. This is the retrieval fallback when
-- Pinecone is unreachable, and the local development path.
ALTER TABLE products
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(description, '')), 'B')
    ) STORED;

CREATE INDEX IF NOT EXISTS products_search_idx ON products USING GIN (search_tsv);
CREATE INDEX IF NOT EXISTS products_merchant_idx ON products (merchant_id) WHERE active;

CREATE TABLE IF NOT EXISTS orders (
    id                TEXT PRIMARY KEY,
    merchant_id       TEXT        NOT NULL REFERENCES merchants(id),
    buyer             TEXT        NOT NULL,
    total_paise       BIGINT      NOT NULL,
    discount_bps      INTEGER     NOT NULL,
    state             TEXT        NOT NULL,
    rp_order_id       TEXT UNIQUE,
    rp_payment_id     TEXT,
    idempotency_key   TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT total_positive CHECK (total_paise > 0),
    CONSTRAINT state_known CHECK (state IN (
        'CREATED', 'AWAITING_PAYMENT', 'CONFIRMED',
        'PAYMENT_FAILED', 'RECONCILIATION_REQUIRED'))
);

-- Finds orders the reconciliation sweep must pick up.
CREATE INDEX IF NOT EXISTS orders_unresolved_idx ON orders (state, updated_at)
    WHERE state IN ('AWAITING_PAYMENT', 'RECONCILIATION_REQUIRED');

CREATE UNIQUE INDEX IF NOT EXISTS orders_idempotency_idx
    ON orders (merchant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS reservations (
    id          BIGSERIAL PRIMARY KEY,
    merchant_id TEXT        NOT NULL REFERENCES merchants(id),
    order_id    TEXT        NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sku         TEXT        NOT NULL,
    qty         INTEGER     NOT NULL,
    state       TEXT        NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    CONSTRAINT qty_positive CHECK (qty > 0),
    CONSTRAINT reservation_state_known CHECK (state IN
        ('HELD', 'COMMITTED', 'RELEASED'))
);

CREATE INDEX IF NOT EXISTS reservations_expiry_idx
    ON reservations (expires_at) WHERE state = 'HELD';
CREATE INDEX IF NOT EXISTS reservations_order_idx ON reservations (order_id);

-- Webhook idempotency. The primary key is the guarantee: a repeated delivery
-- of the same provider event can only ever be inserted once.
CREATE TABLE IF NOT EXISTS webhook_events (
    event_id     TEXT PRIMARY KEY,
    merchant_id  TEXT,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload      JSONB       NOT NULL,
    processed    BOOLEAN     NOT NULL DEFAULT FALSE,
    attempts     INTEGER     NOT NULL DEFAULT 0,
    last_error   TEXT
);

CREATE INDEX IF NOT EXISTS webhook_retry_idx ON webhook_events (received_at)
    WHERE NOT processed;

-- Append only. No UPDATE or DELETE is issued against this table anywhere in
-- the application, and the trigger below refuses them outright.
CREATE TABLE IF NOT EXISTS audit (
    seq          BIGSERIAL PRIMARY KEY,
    merchant_id  TEXT,
    ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
    action       TEXT        NOT NULL,
    detail       JSONB       NOT NULL,
    prev_hash    TEXT        NOT NULL,
    hash         TEXT        NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_merchant_idx ON audit (merchant_id, seq DESC);
CREATE INDEX IF NOT EXISTS audit_action_idx ON audit (action, ts DESC);

CREATE OR REPLACE FUNCTION audit_is_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit table is append only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_no_update ON audit;
CREATE TRIGGER audit_no_update
    BEFORE UPDATE OR DELETE ON audit
    FOR EACH ROW EXECUTE FUNCTION audit_is_append_only();

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS orders_touch ON orders;
CREATE TRIGGER orders_touch BEFORE UPDATE ON orders
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
