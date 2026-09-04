-- A displayable stub for each key.
--
-- Keys are stored hashed and shown exactly once, which is right, and which
-- leaves a merchant with two indistinguishable secrets and no way to tell
-- which one is in which config file. Storing the prefix plus four characters
-- gives them "ccp_live_9c41…" on a settings page. The random part is 32
-- bytes, so four base64 characters leaves 250 bits unguessed.
--
-- Nullable because every merchant created before this migration has a hash
-- and no prefix, and inventing one would mean re-issuing their keys.

ALTER TABLE merchants
    ADD COLUMN IF NOT EXISTS api_key_prefix    TEXT,
    ADD COLUMN IF NOT EXISTS browse_key_prefix TEXT;

-- Signing up now provisions a merchant in the same transaction as the
-- account, so this stops being a shop that exists without an owner.
CREATE INDEX IF NOT EXISTS users_merchant_idx
    ON users (merchant_id) WHERE merchant_id IS NOT NULL;
