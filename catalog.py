"""Catalog import.

A merchant's storefront is the source of what they sell; this is how it gets
in. One function, because every integration ends up in the same shape: a list
of products with a sku, a name, a price, a cost and a stock count.

The adapters in adapters/ do the platform-specific translation. Nothing
platform-specific belongs in here.

cost_paise is required and cannot be derived from a storefront. No shop
publishes what it pays for stock, so the merchant supplies it out of band. The
margin floor is meaningless without it, and a wrong cost is worse than no
system at all — it would approve discounts that lose money on every sale.
"""

import logging

import core
import db
import retrieval

log = logging.getLogger("catalog")

REQUIRED = ("sku", "name", "price_paise", "stock")
OPTIONAL_MONEY = ("cost_paise", "floor_price_paise")


class InvalidProduct(ValueError):
    pass


def _validate(product, index):
    missing = [f for f in REQUIRED if product.get(f) is None]
    if missing:
        raise InvalidProduct(
            f"product {index}: missing {', '.join(missing)}")

    sku = str(product["sku"]).strip()
    if not sku or len(sku) > 64:
        raise InvalidProduct(f"product {index}: sku must be 1-64 characters")

    for field in ("price_paise", "stock") + OPTIONAL_MONEY:
        value = product.get(field)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidProduct(
                f"product {index} ({sku}): {field} must be a whole number, "
                f"got {type(value).__name__}. Money is paise, never rupees "
                f"with a decimal point.")

    if product["price_paise"] <= 0:
        raise InvalidProduct(f"product {index} ({sku}): price must be positive")
    if product["stock"] < 0:
        raise InvalidProduct(f"product {index} ({sku}): stock cannot be negative")

    cost = product.get("cost_paise")
    if cost is not None and cost < 0:
        raise InvalidProduct(f"product {index} ({sku}): cost cannot be negative")

    floor = product.get("floor_price_paise")
    if floor is not None and floor <= 0:
        raise InvalidProduct(
            f"product {index} ({sku}): floor price must be positive")

    # Not rejected. A merchant may genuinely sell a loss leader, and it is not
    # this function's place to overrule them. The margin floor will refuse to
    # discount it later, which is the correct place for that decision.
    if cost is not None and cost > product["price_paise"]:
        log.warning("%s is priced below cost (%s < %s)", sku,
                    product["price_paise"], cost)

    return {
        "sku": sku,
        "name": str(product["name"]).strip()[:300],
        "description": str(product.get("description", "")).strip()[:4000],
        "price_paise": product["price_paise"],
        "cost_paise": cost,
        "floor_price_paise": floor,
        "stock": product["stock"],
    }


def import_products(merchant_id, products, replace=False, sync_vectors=True):
    """Upsert a merchant's catalog.

    replace=True deactivates anything not in this payload, which is what a full
    sync from a storefront means: a product that has left their catalog should
    stop being sellable here. Rows are deactivated rather than deleted so that
    existing orders keep their foreign keys.
    """
    core.get_merchant(merchant_id)

    if not products:
        raise InvalidProduct("no products supplied")
    if len(products) > 5000:
        raise InvalidProduct("import is capped at 5000 products per call")

    cleaned = [_validate(p, i) for i, p in enumerate(products)]

    seen = set()
    for product in cleaned:
        if product["sku"] in seen:
            raise InvalidProduct(f"duplicate sku in payload: {product['sku']}")
        seen.add(product["sku"])

    with db.transaction() as conn:
        for product in cleaned:
            conn.execute(
                "INSERT INTO products (merchant_id, sku, name, description, "
                "price_paise, cost_paise, floor_price_paise, stock, active) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE) "
                "ON CONFLICT (merchant_id, sku) DO UPDATE SET "
                "name = EXCLUDED.name, "
                "description = EXCLUDED.description, "
                "price_paise = EXCLUDED.price_paise, "
                "cost_paise = EXCLUDED.cost_paise, "
                "floor_price_paise = EXCLUDED.floor_price_paise, "
                "stock = EXCLUDED.stock, "
                "active = TRUE, updated_at = now()",
                (merchant_id, product["sku"], product["name"],
                 product["description"], product["price_paise"],
                 product["cost_paise"], product["floor_price_paise"],
                 product["stock"]))

        deactivated = 0
        if replace:
            cur = conn.execute(
                "UPDATE products SET active = FALSE, updated_at = now() "
                "WHERE merchant_id = %s AND active AND NOT (sku = ANY(%s))",
                (merchant_id, list(seen)))
            deactivated = cur.rowcount

        core.audit("CATALOG_IMPORTED", {
            "products": len(cleaned),
            "deactivated": deactivated,
            "replace": replace,
        }, merchant_id=merchant_id, conn=conn)

    synced = 0
    if sync_vectors:
        try:
            synced = retrieval.sync_merchant(merchant_id)
        except Exception as exc:            # noqa: BLE001
            # The catalog is already committed and sellable. A stale search
            # index degrades discovery, so it must not fail the import.
            log.warning("vector sync failed after import (%s: %s); "
                        "postgres full-text search still covers discovery",
                        type(exc).__name__, exc)

    return {
        "imported": len(cleaned),
        "deactivated": deactivated,
        "vectors_synced": synced,
    }


def browse(merchant_id, query=None, limit=20, in_stock_only=True):
    """What an AI buyer sees before it proposes anything.

    Returns prices, never costs. cost_paise is the merchant's own figure and
    has no business leaving the system.
    """
    limit = max(1, min(int(limit), 100))

    if query:
        rows = retrieval.search(merchant_id, query, limit=limit,
                                in_stock_only=in_stock_only)
    else:
        stock_clause = "AND stock > 0" if in_stock_only else ""
        rows = db.query(
            f"SELECT * FROM products WHERE merchant_id = %s AND active "
            f"{stock_clause} ORDER BY price_paise DESC LIMIT %s",
            (merchant_id, limit))

    return [{
        "sku": row["sku"],
        "name": row["name"],
        "description": row["description"],
        "price_paise": row["price_paise"],
        "in_stock": row["stock"] > 0,
    } for row in rows]
