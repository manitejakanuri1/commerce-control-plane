-- The state of a merchant's WhatsApp link, as their delivery worker reports it.
--
-- The worker runs on a box we cannot reach and holds the WhatsApp session. The
-- only way a merchant sees a QR code in this application is if the worker
-- sends one here, so this table is a mailbox between two processes that have
-- no other way to talk.
--
-- A WhatsApp QR expires in about twenty seconds and the worker emits a fresh
-- one. So a stored code is only meaningful next to the time it arrived: a QR
-- rendered from a stale row looks perfectly scannable and simply will not
-- work, which is a worse outcome than showing nothing. Staleness is decided
-- when the row is read, not by a job that deletes it.

CREATE TABLE IF NOT EXISTS whatsapp_sessions (
    merchant_id      TEXT PRIMARY KEY
                     REFERENCES merchants(id) ON DELETE CASCADE,

    -- 'waiting'    the worker is up and asking to be scanned
    -- 'connected'  linked; receipts can go out
    -- 'stopped'    the worker shut down cleanly
    status           TEXT        NOT NULL DEFAULT 'waiting'
                     CHECK (status IN ('waiting', 'connected', 'stopped')),

    -- Base64 PNG of the current code. Null once connected: keeping it would
    -- leave a scannable image for a session somebody already linked.
    qr               TEXT,

    -- The shop's own number, once linked, so a merchant can confirm at a
    -- glance that they scanned with the right phone. Never a customer's.
    connected_number TEXT,

    worker_version   TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE whatsapp_sessions ENABLE ROW LEVEL SECURITY;
