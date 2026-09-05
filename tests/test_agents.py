"""The content agent and the closer.

Both name a discount and neither may decide one. What is worth proving is not
that they produce output — it is that they refuse in the cases where producing
output would cost the merchant money.
"""

import pytest

import closer
import content_agent
import core
import db
import offers

LAPTOP = "LAP-001"          # Rs 1,35,000, cost Rs 1,14,750  — 15% margin
DOCK = "DCK-001"            # Rs 8,500, cost Rs 6,200         — 27% margin
RARE = "RARE-01"            # one in stock


@pytest.fixture(autouse=True)
def clean(clean_database):
    with db.transaction() as conn:
        conn.execute("TRUNCATE events, policy_decisions")
    yield


def line(sku, qty=1):
    return [{"sku": sku, "qty": qty}]


# --------------------------------------------------------------------------
# the band
# --------------------------------------------------------------------------

def test_the_band_is_something_the_gate_would_approve(merchant):
    """Derived by bisection against the engine. If the two disagree, an agent
    quotes a price that dies at checkout, in front of the shopper."""
    ceiling = offers.band(merchant, line(DOCK))
    assert ceiling > 0
    assert core.evaluate_policy(merchant, line(DOCK), ceiling)["approved"]


def test_one_basis_point_above_the_band_is_refused(merchant):
    ceiling = offers.band(merchant, line(DOCK))
    assert not core.evaluate_policy(
        merchant, line(DOCK), ceiling + 1)["approved"]


def test_the_band_never_exceeds_the_merchant_cap(merchant):
    cap, _ = core.merchant_limits(merchant)
    assert offers.band(merchant, line(DOCK)) <= cap


def test_a_product_that_cannot_be_sold_at_all_gets_no_band(merchant):
    """Out of stock is not a discount problem. No reduction helps."""
    db.execute("UPDATE products SET stock = 0 WHERE merchant_id = %s "
               "AND sku = %s", (merchant, DOCK))
    assert offers.band(merchant, line(DOCK)) == 0


def test_a_thin_margin_product_gets_a_smaller_band(merchant):
    """Nothing in the configuration says laptops are special. The margin
    floor works it out."""
    assert offers.band(merchant, line(LAPTOP)) < offers.band(merchant,
                                                             line(DOCK))


def test_tier_one_never_discounts(merchant):
    assert offers.offer(merchant, line(DOCK), 1)["discount_bps"] == 0


def test_tiers_increase_with_intent(merchant):
    given = [offers.offer(merchant, line(DOCK), t)["discount_bps"]
             for t in (1, 2, 3, 4)]
    assert given == sorted(given)


def test_every_tier_produces_an_approved_offer(merchant):
    for tier in offers.TIERS:
        assert offers.offer(merchant, line(DOCK), tier)["approved"], tier


def test_an_unknown_tier_is_refused_rather_than_guessed(merchant):
    with pytest.raises(ValueError, match="unknown tier"):
        offers.offer(merchant, line(DOCK), 9)


# --------------------------------------------------------------------------
# the content agent refuses before it writes
# --------------------------------------------------------------------------

def test_out_of_stock_is_refused_before_any_copy_exists(merchant):
    """Copy for something nobody can buy is worse than no copy: somebody
    might publish it by hand."""
    db.execute("UPDATE products SET stock = 0 WHERE merchant_id = %s "
               "AND sku = %s", (merchant, DOCK))

    result = content_agent.write(merchant, DOCK)
    assert result["approved"] is False
    assert result["reason"] == "out_of_stock"
    assert result["copy"] is None


def test_a_margin_too_thin_to_advertise_is_refused(merchant):
    """The laptop earns 15%. An ad costs more than that, so the campaign
    loses money however good the writing is."""
    result = content_agent.write(merchant, LAPTOP)
    assert result["approved"] is False
    assert result["reason"] == "margin_too_thin"
    assert "15" in result["detail"]


def test_a_healthy_product_produces_copy(merchant):
    result = content_agent.write(merchant, DOCK, tier=3)
    assert result["approved"] is True
    assert result["copy"]["headline"]
    assert result["copy"]["body"]
    assert len(result["copy"]["headline"]) <= content_agent.MAX_HEADLINE
    assert len(result["copy"]["body"]) <= content_agent.MAX_BODY


def test_the_copy_carries_the_approved_discount_not_its_own(merchant):
    result = content_agent.write(merchant, DOCK, tier=3)
    expected = offers.offer(merchant, line(DOCK), 3)["discount_bps"]
    assert result["discount_bps"] == expected


def test_nothing_is_published(merchant):
    """An agent that can both write an ad and buy the placement can spend a
    merchant's budget unattended."""
    result = content_agent.write(merchant, DOCK, tier=3)
    assert "not sent" in result["publish"]


def test_a_refusal_is_audited(merchant):
    content_agent.write(merchant, LAPTOP)
    row = db.query_one(
        "SELECT detail FROM audit WHERE action = 'AD_COPY_REFUSED' "
        "ORDER BY seq DESC LIMIT 1")
    assert row["detail"]["reason"] == "margin_too_thin"


# --------------------------------------------------------------------------
# the closer decides whether contact is worth it
# --------------------------------------------------------------------------

def ref(merchant, who="priya@example.com"):
    return core.pseudonym(merchant, who)


def test_a_cart_worth_chasing_gets_an_offer(merchant):
    decision = closer.evaluate(merchant, ref(merchant), line(DOCK))
    assert decision["chase"] is True
    assert decision["discount_bps"] > 0
    assert decision["offer_paise"] < decision["was_paise"]


def test_a_cart_earning_less_than_the_contact_costs_is_left_alone(merchant):
    """The question every other cart-recovery tool cannot ask, because it
    does not know the margin."""
    db.execute("UPDATE products SET price_paise = 20000, cost_paise = 15000 "
               "WHERE merchant_id = %s AND sku = %s", (merchant, DOCK))

    decision = closer.evaluate(merchant, ref(merchant), line(DOCK))
    assert decision["chase"] is False
    assert decision["reason"] == "not_worth_the_contact"
    assert "costs more than the sale" in decision["detail"]


def test_not_chasing_is_audited_with_the_arithmetic(merchant):
    """A merchant asking why nobody called about a small basket deserves the
    numbers, not silence."""
    db.execute("UPDATE products SET price_paise = 20000, cost_paise = 15000 "
               "WHERE merchant_id = %s AND sku = %s", (merchant, DOCK))
    closer.evaluate(merchant, ref(merchant), line(DOCK))

    row = db.query_one(
        "SELECT detail FROM audit WHERE action = 'CART_NOT_CHASED' "
        "ORDER BY seq DESC LIMIT 1")
    assert row["detail"]["margin_paise"] == 5000
    assert row["detail"]["threshold_paise"] == closer.MIN_MARGIN_TO_CHASE_PAISE


def test_a_high_margin_cart_earns_a_call_and_a_small_one_a_message(merchant):
    by_voice = closer.evaluate(merchant, ref(merchant), line(LAPTOP))
    assert by_voice["channel"] == "voice"

    db.execute("UPDATE products SET price_paise = 90000, cost_paise = 60000 "
               "WHERE merchant_id = %s AND sku = %s", (merchant, DOCK))
    by_message = closer.evaluate(merchant, ref(merchant), line(DOCK))
    assert by_message["channel"] == "message"


def test_no_payment_link_exists_before_she_agrees(merchant):
    """A link created earlier is a discount handed to somebody who has not
    agreed to anything, and who might have paid full price."""
    decision = closer.evaluate(merchant, ref(merchant), line(DOCK))
    assert "not created" in decision["payment_link"]


def test_the_shopper_is_a_reference_never_a_person(merchant):
    decision = closer.evaluate(merchant, ref(merchant), line(DOCK))
    assert "priya@example.com" not in str(decision)
    assert decision["history"]["buyer_ref"] == ref(merchant)


def test_history_counts_confirmed_orders_only(merchant, make_order):
    make_order("ord_hist_1")
    db.execute("UPDATE orders SET state = 'CONFIRMED', buyer_ref = %s "
               "WHERE id = %s", (ref(merchant), "ord_hist_1"))

    history = closer.history_for(merchant, ref(merchant))
    assert history["previous_orders"] == 1


def test_a_returning_buyer_is_offered_more_than_a_first_visit(merchant,
                                                             make_order):
    stranger = closer.evaluate(merchant, ref(merchant, "new@example.com"),
                               line(DOCK))

    for i in range(2):
        make_order(f"ord_ret_{i}")
        db.execute("UPDATE orders SET state = 'CONFIRMED', buyer_ref = %s "
                   "WHERE id = %s", (ref(merchant), f"ord_ret_{i}"))

    regular = closer.evaluate(merchant, ref(merchant), line(DOCK))
    assert regular["discount_bps"] > stranger["discount_bps"]
    assert regular["tier"] > stranger["tier"]


# --------------------------------------------------------------------------
# accepting on the call
# --------------------------------------------------------------------------

def test_acceptance_confirms_the_price_again(merchant):
    decision = closer.evaluate(merchant, ref(merchant), line(DOCK))
    result = closer.accepted(merchant, ref(merchant),
                             decision["discount_bps"], line(DOCK))

    assert result["ok"] is True
    assert result["quote"]["total_paise"] == decision["offer_paise"]


def test_an_offer_that_has_since_lapsed_is_not_honoured(merchant):
    """A price can move and stock can sell in the time a call takes. The
    number she agreed to must still be one the merchant can honour."""
    decision = closer.evaluate(merchant, ref(merchant), line(DOCK))
    db.execute("UPDATE products SET cost_paise = 84000 WHERE merchant_id = %s "
               "AND sku = %s", (merchant, DOCK))

    result = closer.accepted(merchant, ref(merchant),
                             decision["discount_bps"], line(DOCK))
    assert result["ok"] is False
    assert "margin_floor" in result["failed_rules"]


def test_acceptance_is_audited(merchant):
    decision = closer.evaluate(merchant, ref(merchant), line(DOCK))
    closer.accepted(merchant, ref(merchant), decision["discount_bps"],
                    line(DOCK))

    row = db.query_one(
        "SELECT detail FROM audit WHERE action = 'OFFER_ACCEPTED' "
        "ORDER BY seq DESC LIMIT 1")
    assert row["detail"]["buyer_ref"] == ref(merchant)
    assert row["detail"]["discount_bps"] == decision["discount_bps"]
