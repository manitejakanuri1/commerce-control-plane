"""The money rules.

No I/O, no model, no network, no clock. Every function takes plain integers
and returns plain integers, which is what makes this file the one part of the
system that cannot be argued with, cannot be slow, and cannot leak.

Money is always paise, always `int`. A float here would eventually round a
sale in the customer's favour or the merchant's, and nobody would notice for
months. `2700` means Rs 27.00 and nothing else.

Percentages are basis points: 1000 bps = 10%. Integer division throughout, so
the same inputs give the same answer on every machine.
"""

# A rule reports one of three states, because "this rule is not configured" is
# a different fact from "this rule passed", and a merchant deserves to see
# which protections are actually running for them rather than a row of green
# ticks that includes checks nobody enabled.
PASS = "pass"
FAIL = "fail"
NOT_CONFIGURED = "not_configured"

# Two classes of rule, kept apart on purpose.
#
#   MERCHANT_HARD     the merchant's own economics. Nothing overrides these.
#   BUYER_CONSTRAINT  what the shopper asked for. It filters an offer; it can
#                     never authorise one.
MERCHANT_HARD = "MERCHANT_HARD"
BUYER_CONSTRAINT = "BUYER_CONSTRAINT"


def rupees(paise):
    return f"Rs {paise / 100:,.2f}"


def discounted(unit_paise, discount_bps):
    """Unit price after a discount. Integer arithmetic throughout."""
    return unit_paise - (unit_paise * discount_bps) // 10000


def _check(rule, authority, status, detail):
    return {"rule": rule, "authority": authority, "status": status,
            "passed": status != FAIL, "detail": detail}


def evaluate(lines, products, rules, discount_bps, buyer_budget_paise=None):
    """Run every rule and return all outcomes, not just the first failure.

    lines     [{"sku": "SAR-104", "qty": 1}, ...]
    products  {"SAR-104": {"price_paise": .., "cost_paise": .. or None,
                           "floor_price_paise": .. or None, "stock": ..}}
    rules     {"max_discount_bps": .., "min_margin_bps": ..}

    Only FAIL blocks. A merchant who has supplied nothing but a discount cap
    is still protected by that cap; one who has also supplied cost gets margin
    proven as well.
    """
    missing = [ln["sku"] for ln in lines if ln["sku"] not in products]
    if missing:
        raise KeyError(f"unknown sku: {', '.join(missing)}")

    max_discount_bps = int(rules["max_discount_bps"])
    min_margin_bps = int(rules["min_margin_bps"])

    gross = sum(products[ln["sku"]]["price_paise"] * ln["qty"] for ln in lines)
    net = gross - (gross * discount_bps) // 10000

    checks = [_check(
        "discount_cap", MERCHANT_HARD,
        PASS if 0 <= discount_bps <= max_discount_bps else FAIL,
        f"requested {discount_bps / 100:.2f}%, "
        f"cap {max_discount_bps / 100:.2f}%")]

    # Margin can only be proven when every line has a cost. A partial answer
    # would be a wrong one, so it is all or nothing.
    costs = [products[ln["sku"]].get("cost_paise") for ln in lines]
    if any(c is None for c in costs):
        known = sum(1 for c in costs if c is not None)
        checks.append(_check(
            "margin_floor", MERCHANT_HARD, NOT_CONFIGURED,
            f"cost known for {known} of {len(costs)} lines; margin cannot "
            f"be proven"))
    else:
        cost = sum(products[ln["sku"]]["cost_paise"] * ln["qty"]
                   for ln in lines)
        margin_bps = ((net - cost) * 10000) // net if net > 0 else -10000
        checks.append(_check(
            "margin_floor", MERCHANT_HARD,
            PASS if margin_bps >= min_margin_bps else FAIL,
            f"margin {margin_bps / 100:.2f}%, "
            f"floor {min_margin_bps / 100:.2f}%"))

    # A per-product floor reveals a derived number rather than the cost
    # itself, which is what a merchant who will not disclose cost will still
    # agree to share.
    floors = {ln["sku"]: products[ln["sku"]].get("floor_price_paise")
              for ln in lines}
    configured = [f for f in floors.values() if f is not None]
    breached = [
        f"{sku} at {rupees(discounted(products[sku]['price_paise'], discount_bps))} "
        f"is below floor {rupees(floor)}"
        for sku, floor in floors.items()
        if floor is not None
        and discounted(products[sku]["price_paise"], discount_bps) < floor]

    if not configured:
        checks.append(_check(
            "floor_price", MERCHANT_HARD, NOT_CONFIGURED,
            "no floor prices set on these products"))
    else:
        checks.append(_check(
            "floor_price", MERCHANT_HARD,
            FAIL if breached else PASS,
            "; ".join(breached) if breached
            else f"all {len(configured)} floors respected"))

    short = [f"{ln['sku']} (want {ln['qty']}, "
             f"have {products[ln['sku']].get('stock', 0)})"
             for ln in lines
             if products[ln["sku"]].get("stock", 0) < ln["qty"]]
    checks.append(_check(
        "inventory", MERCHANT_HARD,
        FAIL if short else PASS,
        "; ".join(short) if short else "all lines in stock"))

    if buyer_budget_paise is None:
        checks.append(_check("buyer_budget", BUYER_CONSTRAINT,
                             NOT_CONFIGURED, "no budget stated"))
    else:
        checks.append(_check(
            "buyer_budget", BUYER_CONSTRAINT,
            PASS if net <= buyer_budget_paise else FAIL,
            f"offer {rupees(net)}, budget {rupees(buyer_budget_paise)}"))

    return {
        "approved": all(c["status"] != FAIL for c in checks),
        "checks": checks,
        "gross_paise": gross,
        "net_paise": net,
        "discount_bps": discount_bps,
        "failed_rules": [c["rule"] for c in checks if c["status"] == FAIL],
    }


# --------------------------------------------------------------------------
# the band
# --------------------------------------------------------------------------

def band(lines, products, rules, buyer_budget_paise=None):
    """The largest discount, in bps, that every rule would still allow.

    This is what lets an agent personalise an offer without ever seeing a
    cost: it is handed a ceiling and chooses somewhere beneath it.

    The formulas below are derived, but the answer is then *verified* against
    evaluate() and walked down until it passes. evaluate() stays the single
    source of truth, so a rounding error in the derivation can only ever make
    the band smaller — never larger than what would actually be approved.

    The contract, exactly: a non-zero return is guaranteed to be approved by
    evaluate(). Zero means only "do not discount" — it does not promise the
    order is sellable. A product already priced beneath its own margin floor,
    or one that is out of stock, returns zero here and is still refused by
    check(). Widening the guarantee to cover zero would mean this function
    deciding whether a sale may happen, and that decision belongs to the gate.
    """
    if not lines:
        return 0

    cap = int(rules["max_discount_bps"])
    limits = [cap]

    gross = sum(products[ln["sku"]]["price_paise"] * ln["qty"] for ln in lines)
    if gross <= 0:
        return 0

    # Margin. net must stay at or above cost / (1 - min_margin).
    costs = [products[ln["sku"]].get("cost_paise") for ln in lines]
    if all(c is not None for c in costs):
        cost = sum(products[ln["sku"]]["cost_paise"] * ln["qty"]
                   for ln in lines)
        min_margin_bps = int(rules["min_margin_bps"])
        denominator = 10000 - min_margin_bps
        if denominator <= 0:
            return 0
        # ceil division: a floor here would permit a net one paisa too low.
        net_min = -((-cost * 10000) // denominator)
        limits.append(max(0, ((gross - net_min) * 10000) // gross))

    # Floor price, per line. The tightest line binds the whole order.
    for line in lines:
        product = products[line["sku"]]
        floor = product.get("floor_price_paise")
        if floor is not None and product["price_paise"] > 0:
            limits.append(max(0, ((product["price_paise"] - floor) * 10000)
                              // product["price_paise"]))

    # The shopper's own budget is a ceiling too, but a soft one: it shapes
    # what to offer, it does not authorise anything the merchant rules refuse.
    if buyer_budget_paise is not None:
        limits.append(max(0, ((gross - buyer_budget_paise) * 10000) // gross)
                      if buyer_budget_paise < gross else 0)

    candidate = max(0, min(limits))

    # Walk down until evaluate() actually agrees. Integer floors mean the
    # derived figure can land one or two bps high; anything more than that is
    # a bug in this function, so the loop is bounded rather than open-ended.
    for _ in range(4):
        if candidate <= 0:
            return 0
        if evaluate(lines, products, rules, candidate,
                    buyer_budget_paise)["approved"]:
            return candidate
        candidate -= 1
    return 0


# --------------------------------------------------------------------------
# tiers
# --------------------------------------------------------------------------
# The agent picks a tier — a label, which is a judgement. The table picks the
# number — which is arithmetic. Letting a model choose the rupee figure means
# the same shopper gets a different price on a different day, and no one can
# explain why.
#
# A tier is a share OF THE BAND, not a fixed percentage. So a thin-margin
# product protects itself: "go to my limit" on a 4% band is 4%, not 12%.

TIERS = {
    1: 0,      # first visit, browsing        -> list price
    2: 4000,   # returning buyer              -> 40% of the band
    3: 7500,   # abandoned cart               -> 75% of the band
    4: 10000,  # leaving, high-value basket   -> the whole band
}


def offer_bps(band_bps, tier):
    """Turn a band and a tier into the discount to actually offer."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; expected one of "
                         f"{sorted(TIERS)}")
    return (band_bps * TIERS[tier]) // 10000
