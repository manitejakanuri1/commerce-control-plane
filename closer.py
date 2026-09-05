"""Decides whether an abandoned cart is worth chasing, and with what.

Two questions, in this order.

**Is this cart worth the cost of reaching out at all?** A voice call costs
about three rupees. On a cart earning a hundred and twenty, the outreach loses
money before anyone answers. Every other cart-recovery tool skips this question
because it cannot answer it — it does not know the margin. This one does.

**Is the discount profitable?** Asked of the engine, after her history has
decided how large an offer she warrants.

Her history is the input, not a record. Two shoppers abandon the same cart and
are quoted differently, because one has bought twice before and one is new.

Nothing here contacts anybody. It produces an approved offer and the reason
behind it; the voice call and the payment link are separate steps, and the
link is created only once she has said yes.
"""

import logging

import core
import db
import events
import offers

log = logging.getLogger("closer")

# Below this the outreach costs more than the sale earns. A three-rupee call
# and a WhatsApp message are cheap, but not free, and a shop with four hundred
# abandoned two-hundred-rupee carts would spend more chasing them than the
# carts are worth.
MIN_MARGIN_TO_CHASE_PAISE = 20000        # Rs 200

# Above this a voice call pays for itself; below it, a message is the whole
# budget.
CALL_WORTH_IT_PAISE = 100000             # Rs 1,000


def _tier_from_history(history):
    """A judgement about who this is, expressed as a label rather than a
    number. The table in offers.py turns it into rupees."""
    if history["previous_orders"] >= 2:
        return 4, "returning buyer, abandoned again"
    if history["times_abandoned"] >= 2:
        return 3, "abandoned this cart before"
    if history["previous_orders"] == 1:
        return 3, "bought once, abandoned now"
    return 2, "first visit"


def history_for(merchant_id, buyer_ref):
    """What the audit and events already know about her.

    Reads references and counts, never a name, an address or a phone number —
    those are not selected, so no amount of downstream carelessness can put
    one in a prompt or an offer.
    """
    orders = db.query_one(
        "SELECT count(*) AS n FROM orders WHERE merchant_id = %s "
        "AND buyer_ref = %s AND state = 'CONFIRMED'",
        (merchant_id, buyer_ref))

    abandoned = db.query_one(
        "SELECT count(*) AS n FROM audit WHERE merchant_id = %s "
        "AND action = 'CART_ABANDONED' AND detail->>'buyer_ref' = %s",
        (merchant_id, buyer_ref))

    offered = db.query_one(
        "SELECT count(*) AS n FROM audit WHERE merchant_id = %s "
        "AND action = 'OFFER_APPROVED' AND detail->>'buyer_ref' = %s",
        (merchant_id, buyer_ref))

    return {
        "buyer_ref": buyer_ref,
        "previous_orders": orders["n"] if orders else 0,
        "times_abandoned": abandoned["n"] if abandoned else 0,
        "previous_offers": offered["n"] if offered else 0,
    }


def evaluate(merchant_id, buyer_ref, lines):
    """Should we chase this cart, and with what offer?

    Returns a decision either way. A cart not worth chasing is a result, not
    an absence — a merchant asking why nobody called about a Rs 200 basket
    deserves the arithmetic rather than silence.
    """
    products = core.get_products(merchant_id, [ln["sku"] for ln in lines])

    gross = sum(products[ln["sku"]]["price_paise"] * ln["qty"]
                for ln in lines)

    costs = [products[ln["sku"]].get("cost_paise") for ln in lines]
    if all(c is not None for c in costs):
        cost = sum(products[ln["sku"]]["cost_paise"] * ln["qty"]
                   for ln in lines)
        margin = gross - cost
        margin_known = True
    else:
        # Without cost there is no way to know whether contact pays for
        # itself. Treated as worth a message but never a call: a message costs
        # under a rupee, and guessing wrong there is cheap.
        margin = None
        margin_known = False

    history = history_for(merchant_id, buyer_ref)

    if margin_known and margin < MIN_MARGIN_TO_CHASE_PAISE:
        return _not_worth_it(merchant_id, buyer_ref, lines, margin, gross)

    tier, why = _tier_from_history(history)
    decision = offers.offer(merchant_id, lines, tier)

    if not decision["approved"]:
        core.audit("OFFER_REFUSED", {
            "buyer_ref": buyer_ref,
            "failed_rules": decision["failed_rules"],
            "tier": tier,
        }, merchant_id=merchant_id)
        return {"chase": False, "reason": "policy",
                "detail": "; ".join(decision["failed_rules"]),
                "checks": decision["checks"]}

    channel = ("voice" if margin_known and margin >= CALL_WORTH_IT_PAISE
               else "message")

    core.audit("OFFER_APPROVED", {
        "buyer_ref": buyer_ref,
        "band_bps": decision["band_bps"],
        "tier": tier,
        "discount_bps": decision["discount_bps"],
        "channel": channel,
        "basis": why,
    }, merchant_id=merchant_id)
    events.record(merchant_id, "closer_offer", results=len(lines),
                  tier=tier, channel=channel,
                  discount_bps=decision["discount_bps"])

    return {
        "chase": True,
        "channel": channel,
        "tier": tier,
        "why": why,
        "band_bps": decision["band_bps"],
        "discount_bps": decision["discount_bps"],
        "was_paise": decision["gross_paise"],
        "offer_paise": decision["net_paise"],
        "margin_paise": margin,
        "history": history,
        # The link does not exist yet, and that is the point. One created now
        # is a discount handed to somebody who has not agreed to anything and
        # who might have paid full price.
        "payment_link": "not created — minted when she accepts",
    }


def _not_worth_it(merchant_id, buyer_ref, lines, margin, gross):
    core.audit("CART_NOT_CHASED", {
        "buyer_ref": buyer_ref,
        "margin_paise": margin,
        "threshold_paise": MIN_MARGIN_TO_CHASE_PAISE,
    }, merchant_id=merchant_id)

    return {
        "chase": False,
        "reason": "not_worth_the_contact",
        "detail": (f"Cart is {core.rupees(gross)} and earns "
                   f"{core.rupees(margin)}. Reaching out costs more than the "
                   f"sale is worth, so nobody is contacted."),
        "margin_paise": margin,
    }


def accepted(merchant_id, buyer_ref, discount_bps, lines):
    """She said yes on the call. Confirm the price once more, then commit.

    The gate is asked again rather than trusting the figure quoted minutes
    ago: a price can move and stock can sell in the time a call takes, and the
    number she agreed to must still be one the merchant can honour.
    """
    decision = core.evaluate_policy(merchant_id, lines, discount_bps)

    if not decision["approved"]:
        core.audit("OFFER_LAPSED", {
            "buyer_ref": buyer_ref, "discount_bps": discount_bps,
            "failed_rules": decision["failed_rules"],
        }, merchant_id=merchant_id)
        return {"ok": False, "reason": "the offer no longer holds",
                "failed_rules": decision["failed_rules"]}

    quote = core.build_quote(merchant_id, lines, discount_bps)

    core.audit("OFFER_ACCEPTED", {
        "buyer_ref": buyer_ref, "discount_bps": discount_bps,
        "total_paise": quote["total_paise"],
    }, merchant_id=merchant_id)

    return {"ok": True, "quote": quote,
            "next": "create the Razorpay payment link and send it on WhatsApp"}
