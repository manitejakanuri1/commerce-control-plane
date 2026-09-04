"""The merchant's economics cannot be exceeded by anyone.

These tests exist because every other safety property in the system rests on
them. If a price can be influenced from outside, nothing else matters.
"""

import pytest

import core
from core import PolicyViolation

LAPTOP = [{"sku": "LAP-001", "qty": 1}]
BUNDLE = [{"sku": "LAP-001", "qty": 1}, {"sku": "DCK-001", "qty": 1}]


def test_discount_within_both_limits_is_approved(merchant):
    """5% clears the 15% cap and still leaves 10.5% margin, above the 8%
    floor. The two rules bind at different points, which is the whole reason
    both exist."""
    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=500)
    assert decision["approved"], [c for c in decision["checks"]
                                  if not c["passed"]]


def test_discount_above_cap_is_refused(merchant):
    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=1600)
    assert not decision["approved"]
    failed = {c["rule"] for c in decision["checks"] if not c["passed"]}
    assert "discount_cap" in failed


def test_full_discount_is_refused(merchant):
    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=10000)
    assert not decision["approved"]


def test_margin_floor_refuses_a_discount_the_cap_would_allow(merchant):
    """The laptop carries a thin margin, so a discount inside the 15 percent
    cap still breaks the 8 percent margin floor. Both rules are needed."""
    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=1400)
    cap = next(c for c in decision["checks"] if c["rule"] == "discount_cap")
    margin = next(c for c in decision["checks"] if c["rule"] == "margin_floor")
    assert cap["passed"]
    assert not margin["passed"]
    assert not decision["approved"]


def test_every_check_reports_even_when_one_fails(merchant):
    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=9000)
    assert {c["rule"] for c in decision["checks"]} == {
        "discount_cap", "margin_floor", "floor_price", "inventory",
        "buyer_budget"}
    assert all(c["status"] in ("pass", "fail", "not_configured")
               for c in decision["checks"])


def test_buyer_budget_is_not_merchant_authority(merchant):
    """A buyer's stated budget filters an offer. It is never the same class of
    rule as the merchant's own limits, and the response says so."""
    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=0,
                                    buyer_budget_paise=100_00_000)
    by_rule = {c["rule"]: c for c in decision["checks"]}
    assert by_rule["buyer_budget"]["authority"] == "BUYER_CONSTRAINT"
    assert by_rule["discount_cap"]["authority"] == "MERCHANT_HARD"
    assert by_rule["margin_floor"]["authority"] == "MERCHANT_HARD"
    assert not by_rule["buyer_budget"]["passed"]


def test_inventory_shortfall_is_refused(merchant):
    decision = core.evaluate_policy(
        merchant, [{"sku": "RARE-01", "qty": 5}], discount_bps=0)
    inventory = next(c for c in decision["checks"] if c["rule"] == "inventory")
    assert not inventory["passed"]


def test_quote_uses_catalog_price_only(merchant):
    quote = core.build_quote(merchant, LAPTOP, discount_bps=0)
    assert quote["total_paise"] == 13500000
    assert quote["items"][0]["unit_paise"] == 13500000


def test_quote_refuses_a_discount_beyond_the_cap(merchant):
    """Enforced a second time here on purpose. A bug that skipped the policy
    engine must still be unable to produce an out-of-policy quote."""
    with pytest.raises(PolicyViolation):
        core.build_quote(merchant, LAPTOP, discount_bps=5000)


def test_quote_refuses_a_capped_discount_that_breaks_margin(merchant):
    with pytest.raises(PolicyViolation):
        core.build_quote(merchant, LAPTOP, discount_bps=1400)


def test_discount_arithmetic_stays_in_integers(merchant):
    quote = core.build_quote(merchant, BUNDLE, discount_bps=500)
    gross = 13500000 + 850000
    assert quote["gross_paise"] == gross
    assert quote["discount_paise"] == gross * 500 // 10000
    assert quote["total_paise"] == gross - quote["discount_paise"]
    assert all(isinstance(quote[k], int) for k in
               ("gross_paise", "discount_paise", "total_paise"))


def test_unknown_sku_is_rejected(merchant):
    with pytest.raises(KeyError):
        core.build_quote(merchant, [{"sku": "NOPE-999", "qty": 1}], 0)


def test_another_merchants_sku_is_not_visible(merchant):
    """Tenant isolation. Catalog lookups are always scoped by merchant."""
    with pytest.raises(KeyError):
        core.get_products("someone-else", ["LAP-001"])


# --------------------------------------------------------------------------
# a merchant who will not disclose cost is still protected
# --------------------------------------------------------------------------
# No storefront publishes what it pays for stock. Requiring cost would block
# onboarding, so the rules a merchant *will* share have to bind on their own.

def test_margin_is_reported_unconfigured_when_cost_is_unknown(merchant):
    import db
    db.execute("UPDATE products SET cost_paise = NULL "
               "WHERE merchant_id = %s AND sku = %s", (merchant, "LAP-001"))

    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=500)
    margin = next(c for c in decision["checks"] if c["rule"] == "margin_floor")

    assert margin["status"] == "not_configured"
    assert margin["passed"] is True          # cannot block what it cannot judge
    assert decision["approved"]


def test_discount_cap_still_binds_without_cost(merchant):
    """The cap needs nothing sensitive, so it protects a merchant who has
    shared nothing else."""
    import db
    db.execute("UPDATE products SET cost_paise = NULL "
               "WHERE merchant_id = %s AND sku = %s", (merchant, "LAP-001"))

    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=9000)
    assert not decision["approved"]
    failed = {c["rule"] for c in decision["checks"] if c["status"] == "fail"}
    assert failed == {"discount_cap"}


def test_floor_price_refuses_a_discount_the_cap_allows(merchant):
    """A floor is a derived number a merchant will share when they will not
    share cost. It has to bind as hard as margin would."""
    import db
    db.execute("UPDATE products SET cost_paise = NULL, "
               "floor_price_paise = %s "
               "WHERE merchant_id = %s AND sku = %s",
               (13000000, merchant, "LAP-001"))          # Rs 1,30,000 floor

    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=1000)
    cap = next(c for c in decision["checks"] if c["rule"] == "discount_cap")
    floor = next(c for c in decision["checks"] if c["rule"] == "floor_price")

    assert cap["status"] == "pass"           # 10% is inside the 15% cap
    assert floor["status"] == "fail"         # but 1,21,500 is below the floor
    assert not decision["approved"]


def test_quote_refuses_to_breach_a_floor(merchant):
    """Enforced again at quote time, like the cap and the margin floor."""
    import db
    db.execute("UPDATE products SET cost_paise = NULL, "
               "floor_price_paise = %s "
               "WHERE merchant_id = %s AND sku = %s",
               (13000000, merchant, "LAP-001"))

    with pytest.raises(PolicyViolation):
        core.build_quote(merchant, LAPTOP, discount_bps=1000)


def test_floor_reported_unconfigured_when_none_set(merchant):
    decision = core.evaluate_policy(merchant, LAPTOP, discount_bps=0)
    floor = next(c for c in decision["checks"] if c["rule"] == "floor_price")
    assert floor["status"] == "not_configured"


def test_quote_succeeds_with_no_cost_and_no_floor(merchant):
    """The minimum a merchant can share: a price, a stock count, and their
    discount cap. That has to be enough to sell."""
    import db
    db.execute("UPDATE products SET cost_paise = NULL "
               "WHERE merchant_id = %s AND sku = %s", (merchant, "LAP-001"))

    quote = core.build_quote(merchant, LAPTOP, discount_bps=1000)
    assert quote["total_paise"] == 13500000 - 1350000
