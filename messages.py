"""Messages waiting to be delivered.

This service does not send anything. It decides what should be said, and a
worker elsewhere — an always-on box running a WhatsApp session, which a
serverless function cannot be — picks the queue up and delivers it.

The queue exists rather than a direct call because that worker will not always
be up. A receipt written while it is down waits here instead of being lost, and
a shop that reboots its server does not silently stop sending receipts.

One contact detail passes through here. Razorpay's webhook carries the payer's
phone, and a receipt has to go somewhere. It is encrypted onto the order, read
once at delivery, and destroyed — usually within a minute of payment. That is
narrower than holding nothing, and it is stated rather than glossed: a contact
detail exists here between checkout and receipt, and nowhere else, and not
afterwards.
"""

import logging
import uuid

import config
import core
import db

log = logging.getLogger("messages")

# Nothing else may be queued. 'invoice' is a receipt for a purchase already
# made and needs no consent; 'offer' is marketing and may only be queued for a
# buyer who has opted in, which is enforced where offers are created rather
# than here.
KINDS = ("invoice", "offer")

MAX_ATTEMPTS = 5
MAX_BODY = 1000


class MessageError(RuntimeError):
    pass


def _key():
    """The encryption key, from the environment.

    Reuses BUYER_REF_SECRET rather than adding a second secret to configure.
    Both protect the same thing — a shopper's identity — and one secret that
    is definitely set beats two where the second is forgotten and something
    quietly falls back to plaintext.
    """
    key = (config.BUYER_REF_SECRET or "").strip()
    if not key:
        raise MessageError(
            "BUYER_REF_SECRET is not set; refusing to store a contact detail "
            "without a key to encrypt it with.")
    return key


# --------------------------------------------------------------------------
# the contact detail, held briefly
# --------------------------------------------------------------------------

def remember_contact(order_id, contact):
    """Encrypt the payer's phone onto the order. Returns True if stored.

    Never raises. A receipt is worth less than the payment it confirms, so
    nothing in this file may turn a successful payment into an error.
    """
    if not contact:
        return False
    try:
        db.execute(
            "UPDATE orders SET buyer_contact_encrypted = "
            "pgp_sym_encrypt(%s, %s) WHERE id = %s",
            (str(contact)[:64], _key(), order_id))
        return True
    except Exception as exc:                              # noqa: BLE001
        log.warning("could not store contact for %s (%s: %s)",
                    order_id, type(exc).__name__, exc)
        return False


def take_contact(order_id):
    """Read the contact and delete it in one transaction.

    Read-and-delete together, so a worker that crashes after reading does not
    leave the number behind. Losing a receipt is recoverable; keeping a phone
    number we promised to destroy is not.
    """
    try:
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT pgp_sym_decrypt(buyer_contact_encrypted, %s) AS "
                "contact FROM orders WHERE id = %s "
                "AND buyer_contact_encrypted IS NOT NULL",
                (_key(), order_id)).fetchone()
            if row is None:
                return None
            conn.execute(
                "UPDATE orders SET buyer_contact_encrypted = NULL "
                "WHERE id = %s", (order_id,))
            return row["contact"]
    except Exception as exc:                              # noqa: BLE001
        log.warning("could not read contact for %s (%s: %s)",
                    order_id, type(exc).__name__, exc)
        return None


def forget_contact(order_id):
    """Destroy the contact without reading it.

    For an order that will never produce a receipt — failed, abandoned, swept.
    """
    return db.execute(
        "UPDATE orders SET buyer_contact_encrypted = NULL "
        "WHERE id = %s AND buyer_contact_encrypted IS NOT NULL", (order_id,))


# --------------------------------------------------------------------------
# the queue
# --------------------------------------------------------------------------

def queue(merchant_id, buyer_ref, kind, body, link=None, order_id=None,
          expires_at=None):
    """Add one message. Returns its id, or None if it was already queued.

    An invoice is unique per order. A webhook can arrive twice — Razorpay
    retries, and reconciliation may resolve a payment the webhook later
    delivers anyway — and two receipts for one purchase reads as two charges.
    """
    if kind not in KINDS:
        raise MessageError(f"unknown message kind {kind!r}")
    if not body:
        raise MessageError("a message needs a body")

    message_id = "msg_" + uuid.uuid4().hex[:16]
    try:
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO messages (id, merchant_id, buyer_ref, kind, "
                "body, link, order_id, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (message_id, merchant_id, buyer_ref, kind,
                 str(body)[:MAX_BODY], link, order_id, expires_at))
            if not cur.rowcount:
                return None
            core.audit("MESSAGE_QUEUED", {
                "message_id": message_id, "kind": kind,
                "buyer_ref": buyer_ref, "order_id": order_id,
            }, merchant_id=merchant_id, conn=conn)
        return message_id
    except Exception as exc:                              # noqa: BLE001
        log.warning("could not queue %s for %s (%s: %s)",
                    kind, merchant_id, type(exc).__name__, exc)
        return None


def pending(merchant_id, limit=50):
    """What the delivery worker should send next.

    Includes the contact, decrypted, because the worker needs somewhere to
    send it — and the row is the only place that number exists. Expired offers
    are skipped: delivering a link that has already lapsed wastes a message and
    gives the shopper a dead button.
    """
    limit = max(1, min(int(limit), 200))

    rows = db.query(
        "SELECT m.id, m.buyer_ref, m.kind, m.body, m.link, m.order_id, "
        "m.attempts, "
        "pgp_sym_decrypt(o.buyer_contact_encrypted, %s) AS contact "
        "FROM messages m LEFT JOIN orders o ON o.id = m.order_id "
        "WHERE m.merchant_id = %s AND m.status = 'pending' "
        "AND m.attempts < %s "
        "AND (m.expires_at IS NULL OR m.expires_at > now()) "
        "ORDER BY m.created_at LIMIT %s",
        (_key(), merchant_id, MAX_ATTEMPTS, limit))

    return [dict(r) for r in rows]


def mark_sent(merchant_id, message_id):
    """Delivered. Destroys the contact along with it.

    The number is deleted here rather than by the worker, so forgetting to
    delete it is not something a worker author can do.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE messages SET status = 'sent', sent_at = now() "
            "WHERE id = %s AND merchant_id = %s AND status = 'pending' "
            "RETURNING order_id", (message_id, merchant_id)).fetchone()
        if row is None:
            return False
        if row["order_id"]:
            conn.execute(
                "UPDATE orders SET buyer_contact_encrypted = NULL "
                "WHERE id = %s", (row["order_id"],))
        core.audit("MESSAGE_SENT", {"message_id": message_id},
                   merchant_id=merchant_id, conn=conn)
    return True


def deliver_pending(merchant_id, limit=25):
    """Send what is waiting. Returns a count of each outcome.

    Sending is an HTTPS call now rather than a browser session, so it happens
    here rather than in a worker on a box somebody has to keep running. Called
    right after a payment confirms, and again from the ops sweep so a message
    written during an outage is not stranded.

    Never raises. This is called from the webhook path, and a receipt is worth
    less than the payment it confirms.
    """
    import whatsapp

    counts = {"sent": 0, "failed": 0}
    try:
        waiting = pending(merchant_id, limit)
    except Exception as exc:                              # noqa: BLE001
        log.warning("could not read the queue for %s (%s: %s)",
                    merchant_id, type(exc).__name__, exc)
        return counts

    for message in waiting:
        if not message.get("contact"):
            mark_failed(merchant_id, message["id"], "no contact on this order")
            counts["failed"] += 1
            continue
        try:
            whatsapp.send(merchant_id, message["contact"], message["body"]
                          + (f"\n\n{message['link']}" if message["link"]
                             else ""))
        except Exception as exc:                          # noqa: BLE001
            mark_failed(merchant_id, message["id"], str(exc))
            counts["failed"] += 1
            continue
        mark_sent(merchant_id, message["id"])
        counts["sent"] += 1

    return counts


def mark_failed(merchant_id, message_id, error=""):
    """One delivery attempt failed. Retried until MAX_ATTEMPTS.

    The contact is kept until the message either sends or gives up, because a
    retry needs somewhere to send to.
    """
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE messages SET attempts = attempts + 1, last_error = %s, "
            "status = CASE WHEN attempts + 1 >= %s THEN 'failed' "
            "         ELSE 'pending' END "
            "WHERE id = %s AND merchant_id = %s "
            "RETURNING attempts, status, order_id",
            (str(error)[:300], MAX_ATTEMPTS, message_id, merchant_id)
        ).fetchone()
        if row is None:
            return False
        if row["status"] == "failed" and row["order_id"]:
            # Given up. The number has no further purpose here.
            conn.execute(
                "UPDATE orders SET buyer_contact_encrypted = NULL "
                "WHERE id = %s", (row["order_id"],))
    return True


# --------------------------------------------------------------------------
# what an invoice says
# --------------------------------------------------------------------------

def invoice_body(order, items, merchant_name):
    lines = [f"{merchant_name}", ""]
    for item in items:
        qty = f" x{item['qty']}" if item.get("qty", 1) > 1 else ""
        lines.append(f"{item['name']}{qty}  {core.rupees(item['line_paise'])}")

    lines += ["",
              f"Paid  {core.rupees(order['total_paise'])}",
              f"Order {order['id']}",
              "",
              "Thank you. Keep this message as your receipt."]
    return "\n".join(lines)


def queue_invoice(order_id):
    """Queue the receipt for a confirmed order.

    Called after the order is committed and audited, so a receipt is never
    sent for a payment that did not finish settling. Returns the message id,
    or None — which covers a duplicate webhook, a missing order, and any
    failure, none of which may disturb the payment that just succeeded.
    """
    order = db.query_one("SELECT * FROM orders WHERE id = %s", (order_id,))
    if order is None or order["state"] != "CONFIRMED":
        return None

    merchant = db.query_one("SELECT name FROM merchants WHERE id = %s",
                            (order["merchant_id"],))
    items = db.query(
        "SELECT r.sku, r.qty, p.name, p.price_paise * r.qty AS line_paise "
        "FROM reservations r JOIN products p "
        "ON p.sku = r.sku AND p.merchant_id = r.merchant_id "
        "WHERE r.order_id = %s ORDER BY r.sku", (order_id,))

    return queue(
        order["merchant_id"], order["buyer_ref"], "invoice",
        invoice_body(order, [dict(i) for i in items],
                     merchant["name"] if merchant else "Your order"),
        order_id=order_id)
