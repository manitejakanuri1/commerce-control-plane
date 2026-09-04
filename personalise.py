"""Returning-shopper features.

Sits between a merchant's order history and the agent, and exists so that the
two never touch directly.

What goes in: rows from a merchant's orders table.
What comes out: which categories somebody buys, roughly what they spend, and
which skus they already own.

What cannot come out, because it is never selected: a name, an email, a phone
number, an address, or what any individual order was worth.

Profiles are cached in our own database so a shopper's history is read from the
merchant once rather than on every search, and so the merchant's database being
briefly unreachable degrades recommendations instead of breaking checkout.
"""

import logging

import db
from connectors import postgres as merchant_db

log = logging.getLogger("personalise")

REFRESH_AFTER_HOURS = 24


def _cached(merchant_id, buyer_ref):
    return db.query_one(
        "SELECT * FROM buyer_profiles "
        "WHERE merchant_id = %s AND buyer_ref = %s "
        "AND refreshed_at > now() - (%s || ' hours')::interval",
        (merchant_id, buyer_ref, REFRESH_AFTER_HOURS))


def _store(merchant_id, features):
    db.execute(
        "INSERT INTO buyer_profiles (merchant_id, buyer_ref, categories, "
        "owned_skus, typical_low_paise, typical_high_paise, order_count, "
        "last_order_at, refreshed_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now()) "
        "ON CONFLICT (merchant_id, buyer_ref) DO UPDATE SET "
        "categories = EXCLUDED.categories, "
        "owned_skus = EXCLUDED.owned_skus, "
        "typical_low_paise = EXCLUDED.typical_low_paise, "
        "typical_high_paise = EXCLUDED.typical_high_paise, "
        "order_count = EXCLUDED.order_count, "
        "last_order_at = EXCLUDED.last_order_at, "
        "refreshed_at = now()",
        (merchant_id, features["buyer_ref"], features["categories"],
         features["owned_skus"], features["typical_low_paise"],
         features["typical_high_paise"], features["order_count"],
         features["last_order_at"]))


def features_for(merchant_id, customer_id):
    """Derived history for one shopper, or None.

    None is a normal outcome, not a failure: a first-time shopper, a merchant
    on Level 1, or a merchant whose database is momentarily unreachable. The
    agent proposes without history in all three cases.
    """
    if not customer_id:
        return None

    connection = merchant_db.for_merchant(merchant_id)
    if connection is None or not connection.can_read_orders:
        return None

    try:
        buyer_ref = merchant_db.pseudonym(merchant_id, customer_id)
    except merchant_db.ConnectorError as exc:
        log.warning("personalisation disabled: %s", exc)
        return None

    row = _cached(merchant_id, buyer_ref)
    if row is not None:
        return {
            "categories": list(row["categories"]),
            "owned_skus": list(row["owned_skus"]),
            "typical_low_paise": row["typical_low_paise"],
            "typical_high_paise": row["typical_high_paise"],
            "order_count": row["order_count"],
        }

    features = connection.buyer_features(customer_id)
    if features is None:
        return None

    _store(merchant_id, features)
    return {
        "categories": features["categories"],
        "owned_skus": features["owned_skus"],
        "typical_low_paise": features["typical_low_paise"],
        "typical_high_paise": features["typical_high_paise"],
        "order_count": features["order_count"],
    }


def as_prompt_context(features):
    """Render features for the model.

    Every line here is a derived fact. If a field that identified somebody ever
    appeared in this output, it would have had to be selected upstream, and
    connectors.postgres.buyer_features does not select one.
    """
    if not features:
        return ""

    lines = ["", "RETURNING SHOPPER (derived from their order history, "
                 "no personal data):"]
    if features.get("categories"):
        lines.append(f"- usually buys: {', '.join(features['categories'])}")
    low, high = (features.get("typical_low_paise"),
                 features.get("typical_high_paise"))
    if low and high:
        lines.append(f"- typically spends: Rs {low / 100:,.0f} to "
                     f"Rs {high / 100:,.0f}")
    if features.get("owned_skus"):
        owned = ", ".join(features["owned_skus"][:12])
        lines.append(f"- already owns: {owned}")
        lines.append("- do not propose something they already own unless they "
                     "ask for another")
    return "\n".join(lines)
