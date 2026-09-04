-- orders.buyer held a real email address.
--
-- Every other table already held a reference instead: buyer_profiles keys on
-- an HMAC, events redact contact details out of shopper text, and merchant
-- connections never select a name column at all. This table was the exception,
-- which made "we hold nothing that identifies a customer" almost true — and an
-- almost-true claim is one somebody eventually discovers the shape of.
--
-- The address was never needed. It was not sent to Razorpay (which receives
-- only the merchant id in its notes), not used for reconciliation, and not
-- read by anything. It sat in a growing list, and in the append-only audit
-- trail, waiting to be part of a breach.
--
-- Now: core.pseudonym() derives an HMAC scoped to the merchant, and the
-- original is written nowhere. HMAC rather than a bare hash because a plain
-- SHA-256 of an email is reversible by anyone holding a list of addresses.
--
-- Existing rows are not migrated. Hashing them here would need the secret,
-- which does not belong in a migration file, and preserving addresses through
-- a change whose entire purpose is to stop holding them would be absurd.

ALTER TABLE orders RENAME COLUMN buyer TO buyer_ref;

COMMENT ON COLUMN orders.buyer_ref IS
    'HMAC reference to the shopper, scoped per merchant. Never an address, '
    'never reversible without BUYER_REF_SECRET.';
