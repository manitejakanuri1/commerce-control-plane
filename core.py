"""Deterministic commerce core.

No language model is called anywhere in this file. Every figure that reaches a
customer is computed here, from the database, by ordinary Python. The agent
layer can only submit a proposal that this module accepts or rejects.

Three properties this file is responsible for:

  1. A price can only come from the products table.
  2. A merchant's economic limits cannot be exceeded by anyone, including an
     operator or the model.
  3. Stock cannot go negative under concurrency, because reservations take a
     row lock before they read.
"""

import hashlib
import hmac
import json
import logging

import config
import db

log = logging.getLogger("core")


class PolicyViolation(Exception):
    """Raised when code attempts something the policy engine already refused."""


def rupees(paise):
    return f"Rs {paise / 100:,.2f}"


# --------------------------------------------------------------------------
# merchants
# --------------------------------------------------------------------------

def get_merchant(merchant_id):
    row = db.query_one(
        "SELECT * FROM merchants WHERE id = %s AND active", (merchant_id,))
    if row is None:
        raise KeyError(f"unknown or inactive merchant: {merchant_id}")
    return row


def merchant_limits(merchant_id):
    merchant = get_merchant(merchant_id)
    return (merchant["max_discount_bps"] or config.DEFAULT_MAX_DISCOUNT_BPS,
            merchant["min_margin_bps"] or config.DEFAULT_MIN_MARGIN_BPS)


# --------------------------------------------------------------------------
# audit trail
# --------------------------------------------------------------------------

def audit(action, detail, merchant_id=None, conn=None):
    """Append one hash-chained record.

    Chaining makes an edit detectable from inside the application, and the
    table has a trigger refusing UPDATE and DELETE. Neither stops someone with
    superuser access to the database, so this is tamper-evident, not
    tamper-proof. Say it that way.
    """
    payload = json.dumps(detail, sort_keys=True, separators=(",", ":"),
                         default=str)

    def _write(connection):
        previous = connection.execute(
            "SELECT hash FROM audit ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = previous["hash"] if previous else "0" * 64
        digest = hashlib.sha256(
            f"{prev_hash}|{action}|{payload}".encode()).hexdigest()
        connection.execute(
            "INSERT INTO audit (merchant_id, action, detail, prev_hash, hash) "
            "VALUES (%s, %s, %s::jsonb, %s, %s)",
            (merchant_id, action, payload, prev_hash, digest))
        return digest

    if conn is not None:
        return _write(conn)
    with db.transaction() as connection:
        return _write(connection)


def verify_audit_chain(limit=None):
    """Recompute every link. Returns (ok, first_broken_seq)."""
    sql = "SELECT seq, action, detail, prev_hash, hash FROM audit ORDER BY seq"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = db.query(sql)

    prev_hash = "0" * 64
    for row in rows:
        payload = json.dumps(row["detail"], sort_keys=True,
                             separators=(",", ":"), default=str)
        expected = hashlib.sha256(
            f"{prev_hash}|{row['action']}|{payload}".encode()).hexdigest()
        if expected != row["hash"] or row["prev_hash"] != prev_hash:
            return False, row["seq"]
        prev_hash = row["hash"]
    return True, None


def reconciliation_pressure(window_minutes=15):
    """How often the payment path has needed rescuing recently.

    This is the audit trail behaving as a feedback loop rather than a logbook:
    repeated reconciliation means webhook delivery is degraded and somebody
    should be told.
    """
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM audit "
        "WHERE action = 'RECONCILIATION_STARTED' "
        "AND ts > now() - (%s || ' minutes')::interval",
        (window_minutes,))
    return row["n"] if row else 0


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------

def get_products(merchant_id, skus):
    if not skus:
        raise ValueError("no skus requested")
    rows = db.query(
        "SELECT * FROM products WHERE merchant_id = %s AND sku = ANY(%s) "
        "AND active", (merchant_id, list(skus)))
    found = {r["sku"]: r for r in rows}
    missing = [s for s in skus if s not in found]
    if missing:
        raise KeyError(f"unknown sku for {merchant_id}: {', '.join(missing)}")
    return found


# --------------------------------------------------------------------------
# policy engine
# --------------------------------------------------------------------------
# Two classes of rule, kept deliberately apart:
#
#   MERCHANT_HARD    the merchant's own economics. Authoritative. Nothing
#                    overrides these, and they are re-checked at quote time.
#   BUYER_CONSTRAINT what the buyer asked for. Untrusted input, so it filters
#                    an offer but never authorises one.

def discounted(unit_paise, discount_bps):
    """Unit price after a discount. Integer arithmetic throughout."""
    return unit_paise - (unit_paise * discount_bps) // 10000


def evaluate_policy(merchant_id, lines, discount_bps,
                    buyer_budget_paise=None):
    """Run every check and return all outcomes, not just the first failure.

    A check reports one of three states, because "this rule is not configured"
    is different from "this rule passed" and a merchant deserves to see which
    protections are actually active for them:

        pass            enforced, and satisfied
        fail            enforced, and violated
        not_configured  the merchant has not supplied what this rule needs

    Only `fail` blocks. A merchant who has shared nothing but a discount cap is
    still protected by it; one who has also shared cost gets margin proven too.
    """
    max_discount_bps, min_margin_bps = merchant_limits(merchant_id)
    products = get_products(merchant_id, [ln["sku"] for ln in lines])

    gross = sum(products[ln["sku"]]["price_paise"] * ln["qty"] for ln in lines)
    net = gross - (gross * discount_bps) // 10000

    def check(rule, authority, status, detail):
        return {"rule": rule, "authority": authority, "status": status,
                "passed": status != "fail", "detail": detail}

    checks = [check(
        "discount_cap", "MERCHANT_HARD",
        "pass" if 0 <= discount_bps <= max_discount_bps else "fail",
        f"requested {discount_bps / 100:.2f}%, "
        f"cap {max_discount_bps / 100:.2f}%")]

    # Margin can only be proven when every line has a cost. A partial answer
    # would be a wrong one, so it is all or nothing.
    costs = [products[ln["sku"]]["cost_paise"] for ln in lines]
    if any(c is None for c in costs):
        known = sum(1 for c in costs if c is not None)
        checks.append(check(
            "margin_floor", "MERCHANT_HARD", "not_configured",
            f"cost known for {known} of {len(costs)} lines; "
            f"merchant has not supplied cost, so margin cannot be proven"))
    else:
        cost = sum(products[ln["sku"]]["cost_paise"] * ln["qty"]
                   for ln in lines)
        margin_bps = ((net - cost) * 10000) // net if net > 0 else -10000
        checks.append(check(
            "margin_floor", "MERCHANT_HARD",
            "pass" if margin_bps >= min_margin_bps else "fail",
            f"margin {margin_bps / 100:.2f}%, "
            f"floor {min_margin_bps / 100:.2f}%"))

    # Per-product floor. Reveals a derived number rather than the cost itself,
    # which is what a merchant will actually agree to share.
    floors = {ln["sku"]: products[ln["sku"]]["floor_price_paise"]
              for ln in lines}
    breached = [
        f"{sku} at {rupees(discounted(products[sku]['price_paise'], discount_bps))} "
        f"is below floor {rupees(floor)}"
        for sku, floor in floors.items() if floor is not None
        and discounted(products[sku]["price_paise"], discount_bps) < floor]
    configured = [f for f in floors.values() if f is not None]

    if not configured:
        checks.append(check(
            "floor_price", "MERCHANT_HARD", "not_configured",
            "no floor prices set on these products"))
    else:
        checks.append(check(
            "floor_price", "MERCHANT_HARD",
            "fail" if breached else "pass",
            "; ".join(breached) if breached
            else f"all {len(configured)} floors respected"))

    short = [f"{ln['sku']} (want {ln['qty']}, have "
             f"{products[ln['sku']]['stock']})"
             for ln in lines if products[ln["sku"]]["stock"] < ln["qty"]]
    checks.append(check(
        "inventory", "MERCHANT_HARD",
        "fail" if short else "pass",
        "; ".join(short) if short else "all lines in stock"))

    if buyer_budget_paise is None:
        checks.append(check("buyer_budget", "BUYER_CONSTRAINT",
                            "not_configured", "no budget stated"))
    else:
        checks.append(check(
            "buyer_budget", "BUYER_CONSTRAINT",
            "pass" if net <= buyer_budget_paise else "fail",
            f"offer {rupees(net)}, budget {rupees(buyer_budget_paise)}"))

    approved = all(c["status"] != "fail" for c in checks)
    audit("POLICY_EVALUATED", {
        "approved": approved,
        "discount_bps": discount_bps,
        "net_paise": net,
        "failed": [c["rule"] for c in checks if c["status"] == "fail"],
        "not_configured": [c["rule"] for c in checks
                           if c["status"] == "not_configured"],
    }, merchant_id=merchant_id)

    return {
        "approved": approved,
        "checks": checks,
        "gross_paise": gross,
        "net_paise": net,
        "discount_bps": discount_bps,
    }


# --------------------------------------------------------------------------
# quote engine
# --------------------------------------------------------------------------

def build_quote(merchant_id, lines, discount_bps):
    """Compute the payable amount.

    Prices are read from the products table. No caller may pass a price in,
    which is what stops hostile catalog text or a model-invented figure from
    reaching a payment. The merchant cap is enforced again here, so a bug that
    skipped the policy engine still cannot produce an out-of-policy quote.
    """
    max_discount_bps, min_margin_bps = merchant_limits(merchant_id)
    if not 0 <= discount_bps <= max_discount_bps:
        raise PolicyViolation(
            f"discount {discount_bps} bps outside merchant cap "
            f"{max_discount_bps} bps")

    products = get_products(merchant_id, [ln["sku"] for ln in lines])

    items, gross = [], 0
    for line in lines:
        product = products[line["sku"]]
        line_total = product["price_paise"] * line["qty"]
        gross += line_total

        floor = product["floor_price_paise"]
        unit_after = discounted(product["price_paise"], discount_bps)
        if floor is not None and unit_after < floor:
            raise PolicyViolation(
                f"{product['sku']} at {rupees(unit_after)} is below its floor "
                f"of {rupees(floor)}")

        items.append({
            "sku": product["sku"],
            "name": product["name"],
            "qty": line["qty"],
            "unit_paise": product["price_paise"],
            "line_paise": line_total,
        })

    discount = (gross * discount_bps) // 10000
    total = gross - discount

    # Only provable when every line has a cost. Where it is not, the discount
    # cap and any floor prices are what protect the merchant.
    costs = [products[ln["sku"]]["cost_paise"] for ln in lines]
    margin_bps = None
    if all(c is not None for c in costs) and total > 0:
        cost = sum(products[ln["sku"]]["cost_paise"] * ln["qty"]
                   for ln in lines)
        margin_bps = ((total - cost) * 10000) // total
        if margin_bps < min_margin_bps:
            raise PolicyViolation(
                f"quote margin {margin_bps} bps below floor "
                f"{min_margin_bps} bps")

    quote = {
        "items": items,
        "gross_paise": gross,
        "discount_paise": discount,
        "total_paise": total,
        "discount_bps": discount_bps,
    }
    audit("QUOTE_BUILT", {
        "total_paise": total,
        "discount_bps": discount_bps,
        "margin_bps": margin_bps,
        "skus": [i["sku"] for i in items],
    }, merchant_id=merchant_id)
    return quote


# --------------------------------------------------------------------------
# inventory
# --------------------------------------------------------------------------

def reserve(merchant_id, order_id, lines):
    """Hold stock atomically. Returns (ok, reason).

    Rows are locked with FOR UPDATE in a deterministic sku order before any
    stock is read, so two buyers racing for the last unit serialise here and
    the loser is refused rather than both being told it is available. Sorting
    the skus is what prevents two concurrent multi-line orders deadlocking on
    each other.
    """
    ordered = sorted(lines, key=lambda ln: ln["sku"])
    try:
        with db.transaction() as conn:
            release_expired(conn)

            for line in ordered:
                row = conn.execute(
                    "SELECT stock FROM products "
                    "WHERE merchant_id = %s AND sku = %s AND active "
                    "FOR UPDATE",
                    (merchant_id, line["sku"])).fetchone()

                if row is None:
                    raise KeyError(f"unknown sku {line['sku']}")
                if row["stock"] < line["qty"]:
                    audit("RESERVATION_REFUSED", {
                        "order_id": order_id,
                        "sku": line["sku"],
                        "want": line["qty"],
                        "have": row["stock"],
                    }, merchant_id=merchant_id, conn=conn)
                    raise PolicyViolation(
                        f"insufficient stock for {line['sku']}: "
                        f"want {line['qty']}, have {row['stock']}")

            for line in ordered:
                conn.execute(
                    "UPDATE products SET stock = stock - %s "
                    "WHERE merchant_id = %s AND sku = %s",
                    (line["qty"], merchant_id, line["sku"]))
                conn.execute(
                    "INSERT INTO reservations "
                    "(merchant_id, order_id, sku, qty, state, expires_at) "
                    "VALUES (%s, %s, %s, %s, 'HELD', "
                    "now() + (%s || ' seconds')::interval)",
                    (merchant_id, order_id, line["sku"], line["qty"],
                     config.RESERVATION_TTL_SECONDS))

            audit("INVENTORY_RESERVED", {
                "order_id": order_id,
                "lines": [{"sku": l["sku"], "qty": l["qty"]} for l in ordered],
                "ttl_seconds": config.RESERVATION_TTL_SECONDS,
            }, merchant_id=merchant_id, conn=conn)
        return True, "reserved"

    except (PolicyViolation, KeyError) as exc:
        return False, str(exc)


def release_expired(conn=None):
    """Return stock from holds whose TTL has passed.

    Called by the background worker on a timer, and opportunistically at the
    start of every reservation so a single-process deployment still behaves.
    """
    def _run(connection):
        rows = connection.execute(
            "SELECT id, merchant_id, order_id, sku, qty FROM reservations "
            "WHERE state = 'HELD' AND expires_at < now() "
            "ORDER BY sku FOR UPDATE").fetchall()
        for row in rows:
            connection.execute(
                "UPDATE products SET stock = stock + %s "
                "WHERE merchant_id = %s AND sku = %s",
                (row["qty"], row["merchant_id"], row["sku"]))
            connection.execute(
                "UPDATE reservations SET state = 'RELEASED' WHERE id = %s",
                (row["id"],))
        if rows:
            audit("RESERVATIONS_EXPIRED", {
                "count": len(rows),
                "orders": sorted({r["order_id"] for r in rows}),
            }, conn=connection)
        return len(rows)

    if conn is not None:
        return _run(conn)
    with db.transaction() as connection:
        return _run(connection)


def release(merchant_id, order_id):
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT id, sku, qty FROM reservations "
            "WHERE order_id = %s AND state = 'HELD' ORDER BY sku FOR UPDATE",
            (order_id,)).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE products SET stock = stock + %s "
                "WHERE merchant_id = %s AND sku = %s",
                (row["qty"], merchant_id, row["sku"]))
            conn.execute(
                "UPDATE reservations SET state = 'RELEASED' WHERE id = %s",
                (row["id"],))
        if rows:
            audit("INVENTORY_RELEASED",
                  {"order_id": order_id, "lines": len(rows)},
                  merchant_id=merchant_id, conn=conn)
        return len(rows)


def commit_reservation(merchant_id, order_id):
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE reservations SET state = 'COMMITTED' "
            "WHERE order_id = %s AND state = 'HELD'", (order_id,))
        if cur.rowcount:
            audit("INVENTORY_COMMITTED",
                  {"order_id": order_id, "lines": cur.rowcount},
                  merchant_id=merchant_id, conn=conn)
        return cur.rowcount


# --------------------------------------------------------------------------
# orders
# --------------------------------------------------------------------------

VALID_TRANSITIONS = {
    "CREATED": {"AWAITING_PAYMENT", "PAYMENT_FAILED"},
    "AWAITING_PAYMENT": {"CONFIRMED", "PAYMENT_FAILED",
                         "RECONCILIATION_REQUIRED"},
    "RECONCILIATION_REQUIRED": {"CONFIRMED", "PAYMENT_FAILED",
                                "RECONCILIATION_REQUIRED"},
    "CONFIRMED": set(),
    "PAYMENT_FAILED": set(),
}


def pseudonym(merchant_id, identifier):
    """A stable reference to a person that identifies nobody.

    HMAC rather than a bare hash. A plain SHA-256 of an email address is
    reversible in practice: an attacker hashes every address they already have
    and matches. HMAC under a secret they do not hold cannot be attacked that
    way, so the reference is stable for us and useless to anyone else.

    Scoped per merchant, so the same shopper at two shops produces two
    different references and the two shops cannot be joined against each other.
    """
    secret = (config.BUYER_REF_SECRET or "").encode()
    if not secret:
        raise RuntimeError(
            "BUYER_REF_SECRET is not set; refusing to derive a buyer "
            "reference without it. A bare hash of an email address is "
            "reversible by anyone holding a list of addresses.")
    return hmac.new(secret, f"{merchant_id}:{identifier}".encode(),
                    hashlib.sha256).hexdigest()[:32]


def create_order(merchant_id, order_id, buyer, quote, idempotency_key=None):
    """Record an order against a reference to the buyer, never the buyer.

    `buyer` arrives as whatever the caller uses to identify a shopper — an
    email, usually. It is turned into an HMAC reference here and the original
    is not written anywhere: not to this table, not to the audit trail, and
    not to the payment provider, which is sent only the merchant id.

    Every other table in this system already worked this way. This one did
    not, which made "we hold nothing that identifies a customer" almost true —
    and a claim that is almost true is one a merchant will eventually discover
    the shape of at the worst moment.
    """
    buyer_ref = pseudonym(merchant_id, buyer)

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO orders (id, merchant_id, buyer_ref, total_paise, "
            "discount_bps, state, idempotency_key) "
            "VALUES (%s, %s, %s, %s, %s, 'CREATED', %s)",
            (order_id, merchant_id, buyer_ref, quote["total_paise"],
             quote["discount_bps"], idempotency_key))
        audit("ORDER_CREATED", {
            "order_id": order_id,
            "buyer_ref": buyer_ref,
            "total_paise": quote["total_paise"],
        }, merchant_id=merchant_id, conn=conn)


def set_order_state(order_id, state, **fields):
    """Move an order, refusing transitions the state machine does not allow.

    Returns (ok, current_state). A rejected transition is not an error; it is
    usually a late or out-of-order provider event, and the caller should
    reconcile instead of forcing the change.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT merchant_id, state FROM orders WHERE id = %s FOR UPDATE",
            (order_id,)).fetchone()
        if row is None:
            return False, None

        current = row["state"]
        if state != current and state not in VALID_TRANSITIONS.get(current, set()):
            audit("ORDER_TRANSITION_REFUSED", {
                "order_id": order_id,
                "from": current,
                "to": state,
            }, merchant_id=row["merchant_id"], conn=conn)
            return False, current

        assignments = ["state = %s"]
        values = [state]
        for key, value in fields.items():
            assignments.append(f"{key} = %s")
            values.append(value)
        values.append(order_id)

        conn.execute(
            f"UPDATE orders SET {', '.join(assignments)} WHERE id = %s", values)
        audit("ORDER_STATE", {"order_id": order_id, "state": state, **fields},
              merchant_id=row["merchant_id"], conn=conn)
        return True, state


def get_order(order_id):
    return db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))


def unresolved_orders(stale_after_seconds):
    """Orders the reconciliation sweep should look at."""
    return db.query(
        "SELECT * FROM orders "
        "WHERE state IN ('AWAITING_PAYMENT', 'RECONCILIATION_REQUIRED') "
        "AND updated_at < now() - (%s || ' seconds')::interval "
        "ORDER BY updated_at LIMIT 200",
        (stale_after_seconds,))
