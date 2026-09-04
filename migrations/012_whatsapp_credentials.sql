-- WhatsApp Cloud API credentials, per merchant.
--
-- Replaces the browser-driven session this table used to describe. That
-- approach needed a merchant to run an always-on box with Chrome on it, and
-- the library that drove it would not start against a current Chrome at all.
-- More to the point, it impersonated WhatsApp Web, so the number carrying a
-- shop's receipts could be banned for using it.
--
-- The Cloud API is WhatsApp's own. A token and a phone number id are the
-- whole integration: no browser, no QR, no server for the merchant to keep
-- alive, and no reason for Meta to object.
--
-- The token is encrypted at rest with pgcrypto under the same key as the
-- payer contacts. It grants the ability to send messages as that merchant's
-- business, so a database dump must not hand it over in the clear.

ALTER TABLE whatsapp_sessions
    ADD COLUMN IF NOT EXISTS access_token_encrypted BYTEA,
    ADD COLUMN IF NOT EXISTS phone_number_id        TEXT,
    ADD COLUMN IF NOT EXISTS provider               TEXT NOT NULL DEFAULT 'cloud_api';

-- The QR was the browser session's, and there is no longer a browser.
ALTER TABLE whatsapp_sessions DROP COLUMN IF EXISTS qr;

-- Rows describing the old browser sessions cannot be migrated — there is no
-- token to derive from a scanned session — and leaving them would show a
-- merchant as connected when nothing can send.
DELETE FROM whatsapp_sessions WHERE access_token_encrypted IS NULL;

COMMENT ON COLUMN whatsapp_sessions.access_token_encrypted IS
    'WhatsApp Cloud API token, encrypted. Sends as the merchant''s business.';
