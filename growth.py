"""Growth findings: what the data says a merchant should do next.

No model is called in this file. Every figure is read from a row, and every
finding carries the evidence that produced it, so an owner can check the claim
rather than take it on faith.

The rule that shapes everything here: **never invent a recommendation.** A shop
with forty searches and no sales has nothing to advise on, and saying so is the
honest answer. Advice manufactured from three data points reads exactly like
advice drawn from three thousand, which is what makes it dangerous — an owner
acts on it either way.

Findings are ranked by rupees, because a list of six true observations is not a
plan and an owner with an hour to spare needs to know which one to spend it on.
"""

import logging

import events
import policy_log
import db

log = logging.getLogger("growth")

# Below this, the sample is noise. Chosen so that a single unusual afternoon
# cannot produce a recommendation; a merchant sees "not enough data yet"
# instead, which is true and, unlike a confident guess, cannot mislead them.
MIN_DECISIONS = 20
MIN_SEARCHES = 20

# Findings worth less than this are true but not worth an owner's attention.
MIN_IMPACT_PAISE = 100000        # Rs 1,000


def _finding(kind, headline, evidence, action, impact_paise=0):
    return {
        "kind": kind,
        "headline": headline,
        "evidence": evidence,          # what was counted, and from where
        "action": action,              # what to actually do
        "impact_paise": int(impact_paise),
    }


# --------------------------------------------------------------------------
# 1. the cap is tighter than the margin requires
# --------------------------------------------------------------------------

def cap_too_tight(merchant_id, days=30):
    """Sales refused on the discount cap while margin had room left.

    The most common self-inflicted wound. A merchant picks a round number for
    their cap, the margin floor would have allowed more, and the gap between
    the two is sales walking out.
    """
    report = policy_log.summary(merchant_id, days)
    if report["decisions"] < MIN_DECISIONS:
        return None

    by_rule = {r["rule"]: r["refusals"] for r in report["by_rule"]}
    blocked = by_rule.get("discount_cap", 0)
    if not blocked:
        return None

    # What the cap actually is, and what the engine would have allowed. The
    # engine reports allowed_bps on every decision, so this is measured
    # rather than assumed.
    row = db.query_one(
        "SELECT max(allowed_bps) AS headroom, avg(asked_bps)::int AS wanted "
        "FROM policy_decisions WHERE merchant_id = %s "
        "AND 'discount_cap' = ANY(failed_rules) "
        "AND at > now() - (%s || ' days')::interval",
        (merchant_id, days))
    limits = db.query_one(
        "SELECT max_discount_bps FROM merchants WHERE id = %s", (merchant_id,))
    if row is None or limits is None or not row["headroom"]:
        return None

    cap = limits["max_discount_bps"]
    headroom = row["headroom"]
    if headroom <= cap:
        return None                    # the cap is not what is binding

    value = _average_order_paise(merchant_id, days) * blocked

    return _finding(
        "cap_too_tight",
        f"Your discount cap refused {blocked} sales that your margin "
        f"could have afforded.",
        f"Cap is {cap / 100:.1f}%. On these products the margin floor "
        f"allows up to {headroom / 100:.1f}%. Shoppers asked for "
        f"{row['wanted'] / 100:.1f}% on average.",
        f"Raise the cap to {min(headroom, row['wanted'] + 100) / 100:.1f}%. "
        f"Margin stays above your floor — the engine will still refuse "
        f"anything that does not.",
        value)


# --------------------------------------------------------------------------
# 2. demand the catalog does not meet
# --------------------------------------------------------------------------

def missing_stock(merchant_id, days=30):
    """What shoppers searched for and found nothing.

    Invisible to everyone else. The merchant's own search returned "no
    results" and forgot; the payment processor never saw these people, because
    they left before checkout.
    """
    summary = events.summary(merchant_id, days)
    if summary["searches"] < MIN_SEARCHES:
        return None

    demand = events.unmet_demand(merchant_id, days, limit=5)
    if not demand:
        return None

    asked = sum(d["times"] for d in demand)
    top = demand[0]

    # Deliberately conservative: value it at one order per two people who
    # asked. Assuming everyone would have bought turns a real signal into an
    # inflated number an owner will eventually catch us on.
    value = _average_order_paise(merchant_id, days) * (asked // 2)

    return _finding(
        "missing_stock",
        f"{asked} shoppers searched for things you do not stock.",
        "Most asked: " + "; ".join(
            f'"{d["asked"]}" ({d["times"]}x)' for d in demand),
        f'Stock something matching "{top["asked"]}". '
        f"{top['times']} people asked for it this month and left with "
        f"nothing.",
        value)


# --------------------------------------------------------------------------
# 3. stock that never moves
# --------------------------------------------------------------------------

def dead_stock(merchant_id, days=90):
    """In the catalog, in stock, never sold and never even proposed.

    Two different problems wearing the same face, so both are reported: an
    item the agent never proposes is a discovery problem, and an item proposed
    but never bought is a price or description problem.
    """
    rows = db.query(
        """
        SELECT p.sku, p.name, p.price_paise, p.stock
        FROM products p
        WHERE p.merchant_id = %s AND p.active AND p.stock > 0
          AND NOT EXISTS (
              SELECT 1 FROM reservations r
              JOIN orders o ON o.id = r.order_id
              WHERE r.sku = p.sku AND r.merchant_id = p.merchant_id
                AND r.state = 'COMMITTED' AND o.state = 'CONFIRMED'
                AND o.created_at > now() - (%s || ' days')::interval)
        ORDER BY p.price_paise * p.stock DESC
        LIMIT 10
        """,
        (merchant_id, days))
    if not rows:
        return None

    tied_up = sum(r["price_paise"] * r["stock"] for r in rows)
    if tied_up < MIN_IMPACT_PAISE:
        return None

    top = rows[0]
    return _finding(
        "dead_stock",
        f"{len(rows)} products have not sold in {days} days.",
        "; ".join(f"{r['name']} ({r['stock']} in stock)" for r in rows[:5]),
        f"Set a floor price on {top['name']} so the agent can discount it, "
        f"or drop it from the catalog. It is holding "
        f"Rs {top['price_paise'] * top['stock'] / 100:,.0f}.",
        # Not the full tied-up value: freeing it is worth the margin, not the
        # shelf price, and we do not know the cost.
        tied_up // 4)


# --------------------------------------------------------------------------
# 4. winners that ran out
# --------------------------------------------------------------------------

def out_of_stock_demand(merchant_id, days=30):
    """Sales refused because stock had run out.

    The one refusal that is unambiguously lost money: the shopper wanted it,
    the price was fine, and there was nothing on the shelf.
    """
    report = policy_log.summary(merchant_id, days)
    if report["decisions"] < MIN_DECISIONS:
        return None

    by_rule = {r["rule"]: r["refusals"] for r in report["by_rule"]}
    blocked = by_rule.get("inventory", 0)
    if not blocked:
        return None

    rows = db.query(
        "SELECT sku, count(*) AS refusals FROM policy_decisions "
        "WHERE merchant_id = %s AND 'inventory' = ANY(failed_rules) "
        "AND at > now() - (%s || ' days')::interval "
        "GROUP BY sku ORDER BY refusals DESC LIMIT 5",
        (merchant_id, days))

    value = _average_order_paise(merchant_id, days) * blocked

    return _finding(
        "out_of_stock",
        f"{blocked} sales were refused because stock had run out.",
        "; ".join(f"{r['sku']} ({r['refusals']}x)" for r in rows),
        f"Restock {rows[0]['sku']} first. Shoppers wanted it at a price you "
        f"had already approved.",
        value)


# --------------------------------------------------------------------------
# 5 and 6. discovery and conversion, both read from events
# --------------------------------------------------------------------------

def search_gap(merchant_id, days=30):
    summary = events.summary(merchant_id, days)
    if summary["searches"] < MIN_SEARCHES or summary["empty_rate"] is None:
        return None
    if summary["empty_rate"] < 0.30:
        return None

    empty = summary["searches_with_no_results"]
    value = _average_order_paise(merchant_id, days) * (empty // 4)

    return _finding(
        "search_gap",
        f"{summary['empty_rate'] * 100:.0f}% of searches return nothing.",
        f"{empty} of {summary['searches']} searches found no products.",
        "Your catalog is missing what people arrive wanting. Check the "
        "unmet demand list before adding anything else.",
        value)


def conversion_leak(merchant_id, days=30):
    summary = events.summary(merchant_id, days)
    if not summary["proposals"] or summary["proposals"] < MIN_SEARCHES:
        return None
    if summary["conversion"] is None or summary["conversion"] >= 0.10:
        return None

    lost = summary["proposals"] - summary["purchases_started"]
    value = _average_order_paise(merchant_id, days) * (lost // 10)

    return _finding(
        "conversion_leak",
        f"Only {summary['conversion'] * 100:.0f}% of offers become checkouts.",
        f"{summary['proposals']} offers shown, "
        f"{summary['purchases_started']} reached checkout.",
        "Shoppers are seeing offers and leaving. Usually price, sometimes a "
        "product description that does not answer the question they asked.",
        value)


# --------------------------------------------------------------------------

def product_performance(merchant_id, days=90, limit=10):
    """What is selling and what is not, per product.

    Units come from committed reservations on confirmed orders, which is the
    only place this system records what actually left the shelf. Revenue is an
    estimate: list price times quantity, less the order's discount. Line-item
    prices are not stored, so an order that mixed a discounted product with a
    full-price one attributes the discount across both. Labelled as an
    estimate everywhere it is shown, because a merchant comparing it against
    their own books will find the difference and should know why.
    """
    rows = db.query(
        """
        SELECT r.sku,
               p.name,
               sum(r.qty)                       AS units,
               count(DISTINCT o.id)             AS orders,
               sum(p.price_paise * r.qty
                   - (p.price_paise * r.qty * o.discount_bps) / 10000
               )::bigint                        AS revenue_paise
        FROM reservations r
        JOIN orders   o ON o.id = r.order_id
        JOIN products p ON p.sku = r.sku AND p.merchant_id = r.merchant_id
        WHERE r.merchant_id = %s
          AND r.state = 'COMMITTED' AND o.state = 'CONFIRMED'
          AND o.created_at > now() - (%s || ' days')::interval
        GROUP BY r.sku, p.name
        ORDER BY units DESC
        """,
        (merchant_id, days))

    sold = [dict(r) for r in rows]

    # Never sold is a different fact from sold-least, and the action differs:
    # one is a discovery or pricing problem, the other may just be a slow
    # month. Reporting them together would blur that.
    never = db.query(
        """
        SELECT p.sku, p.name, p.price_paise, p.stock
        FROM products p
        WHERE p.merchant_id = %s AND p.active AND p.stock > 0
          AND NOT EXISTS (
              SELECT 1 FROM reservations r
              JOIN orders o ON o.id = r.order_id
              WHERE r.sku = p.sku AND r.merchant_id = p.merchant_id
                AND r.state = 'COMMITTED' AND o.state = 'CONFIRMED'
                AND o.created_at > now() - (%s || ' days')::interval)
        ORDER BY p.price_paise * p.stock DESC
        LIMIT %s
        """,
        (merchant_id, days, limit))

    return {
        "days": days,
        "best": sold[:limit],
        "worst": list(reversed(sold))[:limit] if len(sold) > 1 else [],
        "never_sold": [dict(r) for r in never],
        "products_sold": len(sold),
        "units_total": sum(s["units"] for s in sold),
        "revenue_paise": sum(s["revenue_paise"] or 0 for s in sold),
        "revenue_is_estimated": True,
    }


FINDINGS = (cap_too_tight, missing_stock, dead_stock, out_of_stock_demand,
            search_gap, conversion_leak)


def plan(merchant_id, days=30, limit=3):
    """Every finding that holds, ranked by rupees, worst first.

    Returns a dict rather than a bare list because "there is not enough data
    to say anything" is a real answer and needs somewhere to live. Six true
    observations are not a plan; an owner with an hour needs to know which one
    to spend it on.
    """
    found = []
    for check in FINDINGS:
        try:
            result = check(merchant_id, days)
        except Exception as exc:                          # noqa: BLE001
            # One failing query must not cost the merchant the other five.
            log.warning("growth finding %s failed (%s: %s)",
                        check.__name__, type(exc).__name__, exc)
            continue
        if result:
            found.append(result)

    found.sort(key=lambda f: f["impact_paise"], reverse=True)

    # A shop with no confirmed orders and no catalog has no average order
    # value, so every finding prices at zero. Filtering on that would have
    # reported "nothing is costing you money" to a merchant who had just had
    # eighteen sales refused — the finding was real, only its rupee value was
    # unknown. So: filter by value when we have one, and show the findings
    # unpriced when we do not.
    priced = bool(_average_order_paise(merchant_id, days))
    shown = ([f for f in found if f["impact_paise"] >= MIN_IMPACT_PAISE]
             if priced else found)

    decisions = policy_log.summary(merchant_id, days)["decisions"]
    searches = events.summary(merchant_id, days)["searches"]
    enough = decisions >= MIN_DECISIONS or searches >= MIN_SEARCHES

    return {
        "days": days,
        "findings": shown[:limit],
        "enough_data": enough,
        # False means the rupee figures are absent, not that they are zero.
        "impact_priced": priced,
        "decisions_seen": decisions,
        "searches_seen": searches,
        "note": None if enough else (
            f"Not enough activity yet to advise on. {searches} searches and "
            f"{decisions} pricing decisions in {days} days; roughly "
            f"{MIN_SEARCHES} of either is where the numbers start meaning "
            f"something."),
        "total_impact_paise": sum(f["impact_paise"] for f in shown[:limit]),
    }


# --------------------------------------------------------------------------

def _average_order_paise(merchant_id, days):
    """What a confirmed order is typically worth here.

    Every impact figure is a count multiplied by this, so it is the one number
    that decides whether the ranking is meaningful. Falls back to the median
    catalog price when nothing has sold yet — an estimate a merchant can
    sanity-check at a glance, rather than a zero that silently ranks every
    finding as worthless.
    """
    row = db.query_one(
        "SELECT avg(total_paise)::bigint AS average FROM orders "
        "WHERE merchant_id = %s AND state = 'CONFIRMED' "
        "AND created_at > now() - (%s || ' days')::interval",
        (merchant_id, days))
    if row and row["average"]:
        return int(row["average"])

    fallback = db.query_one(
        "SELECT percentile_disc(0.5) WITHIN GROUP (ORDER BY price_paise) "
        "AS median FROM products WHERE merchant_id = %s AND active",
        (merchant_id,))
    return int(fallback["median"]) if fallback and fallback["median"] else 0
