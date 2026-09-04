-- Two changes, both about what a merchant has to hand over before they can
-- sell through this system.
--
-- 1. cost_paise becomes optional.
--
--    Requiring it meant every merchant had to disclose their buying prices
--    before onboarding, and no storefront publishes cost because it is
--    commercially sensitive. Importing the Bazaar catalog exposed this: the
--    adapter had to invent a 28% margin, and an invented cost is worse than no
--    cost, because the policy engine would enforce it with total confidence.
--
--    A merchant does not need to reveal their margin. They need to state their
--    limit, which they worked out from that margin privately. So:
--
--      max_discount_bps   always enforced, needs nothing sensitive
--      floor_price_paise  optional per product, a derived number
--      cost_paise         optional, and only then can margin be proven
--
--    A merchant sharing nothing but a discount cap still gets an enforced
--    boundary. One sharing cost gets a stronger one. Neither is blocked.
--
-- 2. A second, weaker API key.
--
--    A storefront is static: any key its pages carry is readable by anyone who
--    views source. Such a key must be able to browse and ask for a proposal,
--    and must not be able to create a payment.

ALTER TABLE products ALTER COLUMN cost_paise DROP NOT NULL;

ALTER TABLE products
    ADD COLUMN IF NOT EXISTS floor_price_paise BIGINT;

DO $$
BEGIN
    ALTER TABLE products ADD CONSTRAINT floor_price_sane
        CHECK (floor_price_paise IS NULL OR floor_price_paise > 0);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Browse-and-propose only. Never purchase. Null means the merchant has not
-- issued one, and no storefront key exists for them.
ALTER TABLE merchants
    ADD COLUMN IF NOT EXISTS browse_key_hash TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS merchants_browse_key_idx
    ON merchants (browse_key_hash) WHERE browse_key_hash IS NOT NULL;
