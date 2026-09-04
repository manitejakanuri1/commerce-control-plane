-- Messages waiting to be delivered by a merchant's own server.
--
-- This system cannot send a WhatsApp message and is not meant to. It holds no
-- phone numbers, so there is nobody here to send to — a row names a buyer by
-- the same HMAC reference used everywhere else, and only the merchant can turn
-- that back into a person.
--
-- So the split is: we decide what should be said and whether an offer inside
-- it is allowed; their server looks up the number in its own customer table
-- and sends it from the shop's own WhatsApp. A customer sees a message from
-- the shop they bought from, which is the only sender they would trust.
--
-- The table exists rather than a direct call because a merchant's server is
-- not always up. A message written while it is offline waits here instead of
-- being lost.

CREATE TABLE IF NOT EXISTS messages (
    id           TEXT PRIMARY KEY,
    merchant_id  TEXT        NOT NULL REFERENCES merchants(id) ON DELETE CASCADE,

    -- HMAC reference. Never a phone number, never an address.
    buyer_ref    TEXT        NOT NULL,

    -- 'invoice' needs no consent: it is a receipt for a purchase already made.
    -- 'offer' is marketing and may only be queued for a buyer who opted in.
    kind         TEXT        NOT NULL CHECK (kind IN ('invoice', 'offer')),

    body         TEXT        NOT NULL,
    link         TEXT,

    -- The order this concerns, when there is one.
    order_id     TEXT        REFERENCES orders(id) ON DELETE SET NULL,

    status       TEXT        NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'sent', 'failed', 'expired')),
    attempts     INTEGER     NOT NULL DEFAULT 0,
    last_error   TEXT,

    -- An offer link expires; there is no point delivering it afterwards.
    expires_at   TIMESTAMPTZ,

    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at      TIMESTAMPTZ
);

-- The one query a merchant's worker runs, every minute.
CREATE INDEX IF NOT EXISTS messages_pending_idx
    ON messages (merchant_id, created_at)
    WHERE status = 'pending';

-- One invoice per order. A webhook can arrive twice — Razorpay retries, and
-- reconciliation may resolve the same payment the webhook later delivers —
-- and a customer receiving two receipts for one purchase would reasonably
-- assume they had been charged twice.
CREATE UNIQUE INDEX IF NOT EXISTS messages_one_invoice_per_order
    ON messages (order_id) WHERE kind = 'invoice' AND order_id IS NOT NULL;

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

-- The one piece of contact detail this system ever holds.
--
-- Razorpay's webhook carries the payer's phone. To deliver a receipt we need
-- it, and there is no way around that: a message has to go somewhere. So it is
-- held encrypted, on the order, and destroyed the moment the receipt is sent —
-- usually within a minute of payment.
--
-- pgcrypto rather than an application-side library, so the plaintext never
-- exists in a process that also handles requests, and so this needs no new
-- dependency. The key is passed per statement from the environment and is not
-- stored in the database.
--
-- The claim this leaves us able to make is narrower than before and still
-- true: a contact detail exists here between checkout and receipt, and nowhere
-- else, and not afterwards.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS buyer_contact_encrypted BYTEA;

COMMENT ON COLUMN orders.buyer_contact_encrypted IS
    'Payer phone from the Razorpay webhook, encrypted with pgcrypto and '
    'deleted as soon as the invoice is delivered. Never plaintext, never kept.';
