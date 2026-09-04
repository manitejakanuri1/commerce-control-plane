"""The rules, with no database and no network.

Everything in rules.py is a pure function of integers, which is the point:
the part of the system that decides whether money moves can be proven at a
desk, offline, in a second.

    pytest packages/commerce-policy
"""

import pytest

from commerce_policy import rules

RULES = {"max_discount_bps": 1000, "min_margin_bps": 2000}

# Rs 3,000 saree bought for Rs 1,900, four in stock.
SAREE = {"price_paise": 300000, "cost_paise": 190000,
         "floor_price_paise": 240000, "stock": 4}

# Rs 800 shirt bought for Rs 760. Almost no room to move.
SHIRT = {"price_paise": 80000, "cost_paise": 76000,
         "floor_price_paise": None, "stock": 10}

ONE_SAREE = [{"sku": "SAR-104", "qty": 1}]
PRODUCTS = {"SAR-104": SAREE, "SHT-01": SHIRT}


def approved(discount_bps, lines=ONE_SAREE, products=None, budget=None):
    return rules.evaluate(lines, products or PRODUCTS, RULES,
                          discount_bps, budget)


# --------------------------------------------------------------------------
# money
# --------------------------------------------------------------------------

def test_full_price_is_approved():
    assert approved(0)["approved"]


def test_discount_within_every_limit_is_approved():
    result = approved(800)
    assert result["approved"]
    assert result["net_paise"] == 276000        # Rs 2,760


def test_discount_above_the_cap_is_refused():
    result = approved(1500)
    assert not result["approved"]
    assert result["failed_rules"] == ["discount_cap"]


def test_every_rule_is_reported_not_just_the_first_failure():
    """A merchant who sees only the first refusal fixes one thing, retries,
    and discovers the next one. Show all of them."""
    result = approved(1500)
    assert len(result["checks"]) == 5
    assert {c["rule"] for c in result["checks"]} == {
        "discount_cap", "margin_floor", "floor_price", "inventory",
        "buyer_budget"}


def test_money_is_integers_all_the_way_through():
    result = approved(333)
    assert isinstance(result["net_paise"], int)
    assert isinstance(result["gross_paise"], int)


# --------------------------------------------------------------------------
# three states, not two
# --------------------------------------------------------------------------

def test_missing_cost_reports_not_configured_rather_than_passing():
    """The dangerous failure is a green tick for a check that never ran."""
    products = {"SAR-104": {**SAREE, "cost_paise": None}}
    result = approved(800, products=products)

    margin = next(c for c in result["checks"] if c["rule"] == "margin_floor")
    assert margin["status"] == rules.NOT_CONFIGURED
    assert result["approved"]           # the cap and the floor still protect


def test_a_shop_with_only_a_cap_is_still_protected_by_it():
    products = {"SAR-104": {**SAREE, "cost_paise": None,
                            "floor_price_paise": None}}
    assert not approved(1500, products=products)["approved"]


def test_margin_is_all_or_nothing_across_lines():
    """A margin proven on some lines and guessed on others is a wrong
    answer wearing a right answer's clothes."""
    products = {"SAR-104": SAREE, "SHT-01": {**SHIRT, "cost_paise": None}}
    lines = [{"sku": "SAR-104", "qty": 1}, {"sku": "SHT-01", "qty": 1}]
    result = rules.evaluate(lines, products, RULES, 500)

    margin = next(c for c in result["checks"] if c["rule"] == "margin_floor")
    assert margin["status"] == rules.NOT_CONFIGURED


# --------------------------------------------------------------------------
# the floor and the stock
# --------------------------------------------------------------------------

def test_floor_price_blocks_a_discount_the_cap_would_allow():
    products = {"SAR-104": {**SAREE, "floor_price_paise": 290000}}
    result = approved(800, products=products)
    assert not result["approved"]
    assert result["failed_rules"] == ["floor_price"]


def test_out_of_stock_is_refused():
    products = {"SAR-104": {**SAREE, "stock": 0}}
    assert not approved(0, products=products)["approved"]


def test_partial_stock_is_refused():
    lines = [{"sku": "SAR-104", "qty": 5}]        # four in stock
    assert not approved(0, lines=lines)["approved"]


# --------------------------------------------------------------------------
# the buyer's budget is not the merchant's rule
# --------------------------------------------------------------------------

def test_budget_filters_an_offer():
    assert not approved(0, budget=250000)["approved"]


def test_budget_never_authorises_what_the_merchant_refuses():
    """A stated budget is untrusted input. It must not widen a cap."""
    result = approved(1500, budget=1000000)
    assert not result["approved"]
    assert "discount_cap" in result["failed_rules"]


def test_budget_and_cap_are_different_authorities():
    result = approved(0)
    budget = next(c for c in result["checks"] if c["rule"] == "buyer_budget")
    cap = next(c for c in result["checks"] if c["rule"] == "discount_cap")
    assert budget["authority"] == rules.BUYER_CONSTRAINT
    assert cap["authority"] == rules.MERCHANT_HARD


# --------------------------------------------------------------------------
# the band
# --------------------------------------------------------------------------

def test_a_non_zero_band_is_always_something_evaluate_would_approve():
    """The band is derived by formula and then verified. If the two ever
    disagree, an agent is handed a ceiling the gate then refuses, and every
    shopper sees an offer that dies at checkout.

    Zero is the one exception, and deliberately so — see band()'s contract."""
    for products in ({"SAR-104": SAREE},
                     {"SAR-104": {**SAREE, "cost_paise": None}},
                     {"SAR-104": {**SAREE, "floor_price_paise": None}},
                     {"SAR-104": {**SAREE, "cost_paise": 299000}},
                     {"SAR-104": {**SAREE, "stock": 0}}):
        allowed = rules.band(ONE_SAREE, products, RULES)
        if allowed:
            assert rules.evaluate(ONE_SAREE, products, RULES,
                                  allowed)["approved"], products


def test_a_zero_band_does_not_mean_the_order_is_sellable():
    """A saree bought for Rs 2,990 and listed at Rs 3,000 is beneath its own
    margin floor before any discount. The band is zero, and the gate still
    refuses — the band must not be mistaken for permission."""
    products = {"SAR-104": {**SAREE, "cost_paise": 299000}}

    assert rules.band(ONE_SAREE, products, RULES) == 0
    assert not rules.evaluate(ONE_SAREE, products, RULES, 0)["approved"]


def test_one_bp_above_the_band_is_refused():
    allowed = rules.band(ONE_SAREE, PRODUCTS, RULES)
    assert not approved(allowed + 1)["approved"]


def test_the_band_never_exceeds_the_cap():
    generous = {"max_discount_bps": 500, "min_margin_bps": 0}
    assert rules.band(ONE_SAREE, PRODUCTS, generous) == 500


def test_a_thin_margin_product_gets_a_small_band_with_no_extra_rule():
    """The shirt earns Rs 40 on Rs 800. Nothing in the config says shirts are
    special; the margin floor works it out."""
    lines = [{"sku": "SHT-01", "qty": 1}]
    assert rules.band(lines, PRODUCTS, RULES) == 0


def test_a_product_already_below_the_margin_floor_gets_no_band():
    products = {"SAR-104": {**SAREE, "cost_paise": 295000}}
    assert rules.band(ONE_SAREE, products, RULES) == 0


# --------------------------------------------------------------------------
# tiers
# --------------------------------------------------------------------------

def test_a_tier_is_a_share_of_the_band_not_a_fixed_percentage():
    """This is what stops 'go to my limit' from meaning 12% on a product
    that can only afford 4%."""
    saree_band = rules.band(ONE_SAREE, PRODUCTS, RULES)
    shirt_band = rules.band([{"sku": "SHT-01", "qty": 1}], PRODUCTS, RULES)

    assert rules.offer_bps(saree_band, 4) == saree_band
    assert rules.offer_bps(shirt_band, 4) == shirt_band    # 0, and safely so


def test_tier_one_never_discounts():
    assert rules.offer_bps(1000, 1) == 0


def test_tiers_increase_with_intent():
    band = 1000
    offers = [rules.offer_bps(band, t) for t in (1, 2, 3, 4)]
    assert offers == sorted(offers)
    assert offers[-1] == band


def test_an_unknown_tier_is_refused_rather_than_guessed():
    with pytest.raises(ValueError, match="unknown tier"):
        rules.offer_bps(1000, 7)


def test_every_tier_produces_an_approvable_offer():
    allowed = rules.band(ONE_SAREE, PRODUCTS, RULES)
    for tier in rules.TIERS:
        offer = rules.offer_bps(allowed, tier)
        assert approved(offer)["approved"], tier


# --------------------------------------------------------------------------
# arithmetic
# --------------------------------------------------------------------------

def test_discounted_rounds_in_the_merchant_s_favour():
    """Integer division truncates the discount, never the price. Over a
    million orders the difference belongs to somebody, and it should be the
    party that did not choose the rounding."""
    assert rules.discounted(999, 3333) == 999 - (999 * 3333) // 10000


def test_unknown_sku_raises_rather_than_being_skipped():
    with pytest.raises(KeyError):
        rules.evaluate([{"sku": "NOPE", "qty": 1}], PRODUCTS, RULES, 0)
