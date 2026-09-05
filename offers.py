"""The band, and how an agent picks a point inside it.

Shared by the content agent and the closer, because both face the same
problem: they want to name a discount, and neither may decide one.

The engine answers a narrower question than "may I offer this?" — it answers
"what is the most anyone could offer?". That ceiling is safe to hand to an
agent, because two products with the same band may have completely different
margins, so nothing about a merchant's economics can be reconstructed from it.

The agent then picks a *tier* — a judgement about who it is talking to, which
is what models are good at — and a table turns that into a number, which is
what they are bad at. Letting a model choose the rupee figure means the same
shopper is quoted differently on a different day, and nobody can explain why.
"""

import logging

import core

log = logging.getLogger("offers")

# A tier is a share OF THE BAND, not a fixed percentage. So a thin-margin
# product protects itself with no special rule: "go to my limit" on a 4% band
# is 4%, not the 12% it would be on a saree.
TIERS = {
    1: 0,      # first visit, browsing
    2: 4000,   # returning buyer
    3: 7500,   # abandoned cart
    4: 10000,  # leaving, high-value basket
}

# Below this a discount is not worth the arithmetic — a shopper does not
# notice half a percent, and the merchant has given something away for nothing.
MIN_MEANINGFUL_BPS = 100


def band(merchant_id, lines, buyer_budget_paise=None):
    """The largest discount every rule would still allow, in bps.

    Derived by bisection against the policy engine rather than by formula.
    evaluate_policy stays the single source of truth, so this cannot return a
    ceiling the gate would then refuse — which would mean an agent quoting a
    price that dies at checkout, in front of the shopper.
    """
    cap, _ = core.merchant_limits(merchant_id)

    if not _approved(merchant_id, lines, 0, buyer_budget_paise):
        # Not sellable even at list price — out of stock, or already priced
        # below its own margin floor. There is no discount that helps.
        return 0

    low, high = 0, int(cap)
    while low < high:
        middle = (low + high + 1) // 2
        if _approved(merchant_id, lines, middle, buyer_budget_paise):
            low = middle
        else:
            high = middle - 1
    return low


def _approved(merchant_id, lines, discount_bps, buyer_budget_paise):
    try:
        return core.evaluate_policy(
            merchant_id, lines, discount_bps, buyer_budget_paise)["approved"]
    except (KeyError, ValueError):
        return False


def offer(merchant_id, lines, tier, buyer_budget_paise=None):
    """Band, tier, then the gate again on the exact number.

    Asked twice on purpose. The band said what was possible a moment ago; this
    confirms the specific figure now, against prices and stock as they are.
    """
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of "
                         f"{sorted(TIERS)}")

    ceiling = band(merchant_id, lines, buyer_budget_paise)
    proposed = (ceiling * TIERS[tier]) // 10000

    if proposed < MIN_MEANINGFUL_BPS:
        proposed = 0

    decision = core.evaluate_policy(merchant_id, lines, proposed,
                                    buyer_budget_paise)

    return {
        "band_bps": ceiling,
        "tier": tier,
        "discount_bps": proposed,
        "approved": decision["approved"],
        "checks": decision["checks"],
        # core.evaluate_policy reports per-check states rather than a summary
        # list; the package's evaluate() returns failed_rules directly. Derived
        # here so callers see one shape whichever engine answered.
        "failed_rules": [c["rule"] for c in decision["checks"]
                         if c["status"] == "fail"],
        "gross_paise": decision["gross_paise"],
        "net_paise": decision["net_paise"],
    }
