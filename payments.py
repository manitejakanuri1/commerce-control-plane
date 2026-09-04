"""Razorpay integration and reconciliation.

Two ideas separate this from an ordinary payment client:

  1. A missing webhook is not a failed payment. It is an *unknown* payment,
     which is a different state with a different remedy.
  2. The remedy is never to charge again. It is to ask the provider what it
     already knows and adopt that answer.

Money is only ever moved by Razorpay. This service creates orders and reads
status; it never holds, routes, or settles funds. Keep it that way — holding
customer money in India requires a payment aggregator licence.
"""

import hashlib
import hmac
import json
import logging
import time
import uuid

import config
import core
import db
import messages

log = logging.getLogger("payments")


class Simulator:
    """Enough of Razorpay's behaviour to exercise every failure path locally.

    Holds authoritative payment state the way the provider does, so
    reconciliation has something truthful to interrogate in development.
    """

    def __init__(self):
        self.orders = {}
        self.payments = {}
        self.drop_next_webhook = False

    def create_order(self, amount_paise, receipt):
        rp_order_id = "order_SIM" + uuid.uuid4().hex[:12]
        self.orders[rp_order_id] = {
            "id": rp_order_id, "amount": amount_paise,
            "receipt": receipt, "status": "created",
        }
        return self.orders[rp_order_id]

    def pay(self, rp_order_id, outcome="captured"):
        rp_payment_id = "pay_SIM" + uuid.uuid4().hex[:12]
        self.payments[rp_payment_id] = {
            "id": rp_payment_id, "order_id": rp_order_id,
            "amount": self.orders[rp_order_id]["amount"],
            "status": outcome, "created_at": time.time(),
        }
        self.orders[rp_order_id]["status"] = (
            "paid" if outcome == "captured" else "attempted")
        delivered = not self.drop_next_webhook
        self.drop_next_webhook = False
        return rp_payment_id, delivered

    def payments_for_order(self, rp_order_id):
        return [p for p in self.payments.values()
                if p["order_id"] == rp_order_id]


SIM = Simulator()
_client = None
LIVE = config.RAZORPAY_LIVE

if LIVE:
    try:
        import razorpay
        _client = razorpay.Client(
            auth=(config.RAZORPAY_KEY_ID, config.RAZORPAY_KEY_SECRET))
    except Exception as exc:                    # noqa: BLE001
        log.error("razorpay client unavailable (%s); using simulator", exc)
        LIVE = False


def mode():
    return "razorpay" if LIVE else "simulator"


# --------------------------------------------------------------------------
# order creation
# --------------------------------------------------------------------------

def create_payment_order(merchant_id, order_id, amount_paise):
    if LIVE:
        rp_order = _client.order.create({
            "amount": amount_paise,
            "currency": "INR",
            "receipt": order_id,
            "payment_capture": 1,
            "notes": {"merchant_id": merchant_id},
        })
    else:
        rp_order = SIM.create_order(amount_paise, order_id)

    core.set_order_state(order_id, "AWAITING_PAYMENT",
                         rp_order_id=rp_order["id"])
    core.audit("PAYMENT_ORDER_CREATED", {
        "order_id": order_id,
        "rp_order_id": rp_order["id"],
        "amount_paise": amount_paise,
        "mode": mode(),
    }, merchant_id=merchant_id)
    return rp_order


# --------------------------------------------------------------------------
# webhook handling
# --------------------------------------------------------------------------

def verify_signature(raw_body, signature, secret=None):
    secret = secret or config.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        raise RuntimeError("RAZORPAY_WEBHOOK_SECRET is not configured")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def sign(raw_body, secret=None):
    """Produce a valid signature. Used by tests and the local simulator."""
    secret = secret or config.RAZORPAY_WEBHOOK_SECRET
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def record_webhook(payload):
    """Persist before processing.

    The insert is the idempotency guarantee: the provider retries deliveries,
    and the primary key means a repeat can only land once. Returns False when
    this event has been seen before.
    """
    event_id = payload.get("id") or payload.get("event_id")
    if not event_id:
        return False, None

    try:
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO webhook_events (event_id, payload) "
                "VALUES (%s, %s::jsonb)",
                (event_id, json.dumps(payload, default=str)))
        return True, event_id
    except Exception:                           # noqa: BLE001 - unique violation
        core.audit("WEBHOOK_DUPLICATE_IGNORED", {"event_id": event_id})
        return False, event_id


def _payer_contact(payload):
    """The payer's phone, as Razorpay reports it.

    The only contact detail this system ever touches, and the only reason it
    is touched is that a receipt has to go somewhere. It is encrypted onto the
    order and destroyed when the receipt is delivered.

    Absent when the shopper paid by a method that carries no phone, which is
    ordinary: the order still confirms and there is simply no receipt to send.
    """
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    return (entity.get("contact") or "").strip() or None


def process_webhook(payload):
    """Apply one already-recorded event. Returns a short outcome string."""
    event_id = payload.get("id") or payload.get("event_id")
    entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
    rp_order_id = entity.get("order_id")
    rp_payment_id = entity.get("id")
    status = entity.get("status")

    order = db.query_one("SELECT * FROM orders WHERE rp_order_id = %s",
                         (rp_order_id,))
    if order is None:
        core.audit("WEBHOOK_ORPHANED", {"rp_order_id": rp_order_id,
                                        "event_id": event_id})
        _mark_processed(event_id, error="no matching local order")
        return f"no local order for {rp_order_id}"

    merchant_id = order["merchant_id"]
    target = "CONFIRMED" if status == "captured" else "PAYMENT_FAILED"

    moved, current = core.set_order_state(
        order["id"], target, rp_payment_id=rp_payment_id)

    if not moved:
        # Late or out-of-order delivery. Do not force the transition; ask the
        # provider what is true now and adopt that instead.
        core.audit("WEBHOOK_OUT_OF_ORDER", {
            "order_id": order["id"],
            "current_state": current,
            "attempted": target,
            "event_id": event_id,
        }, merchant_id=merchant_id)
        outcome = reconcile(order["id"])
        _mark_processed(event_id)
        return f"{outcome} (out-of-order event)"

    if target == "CONFIRMED":
        core.commit_reservation(merchant_id, order["id"])
        # Only after the stock is committed and the transition audited. A
        # receipt for a payment that had not finished settling would be a
        # message we could not take back.
        messages.remember_contact(order["id"], _payer_contact(payload))
        messages.queue_invoice(order["id"])
        # Sent here rather than by a worker elsewhere: the Cloud API is one
        # HTTPS call, which a serverless function can make and a browser
        # session could not. Anything that fails stays queued for the sweep.
        messages.deliver_pending(merchant_id)
        result = f"order {order['id']} confirmed"
    else:
        core.release(merchant_id, order["id"])
        # This order will never produce a receipt, so nothing here needs a
        # way to reach the payer.
        messages.forget_contact(order["id"])
        result = f"order {order['id']} failed, stock released"

    _mark_processed(event_id)
    return result


def _mark_processed(event_id, error=None):
    if not event_id:
        return
    db.execute(
        "UPDATE webhook_events SET processed = %s, attempts = attempts + 1, "
        "last_error = %s WHERE event_id = %s",
        (error is None, error, event_id))


def handle_webhook(payload, signature=None, raw_body=None):
    """Full path: verify, persist, process."""
    if raw_body is not None:
        if not verify_signature(raw_body, signature):
            core.audit("WEBHOOK_REJECTED", {"reason": "signature mismatch"})
            return "rejected: signature mismatch"

    fresh, event_id = record_webhook(payload)
    if not event_id:
        return "rejected: missing event id"
    if not fresh:
        return f"ignored duplicate {event_id}"

    return process_webhook(payload)


def retry_failed_webhooks(limit=50):
    """Re-run events that were recorded but never processed cleanly."""
    rows = db.query(
        "SELECT payload FROM webhook_events WHERE NOT processed "
        "AND attempts < 10 ORDER BY received_at LIMIT %s", (limit,))
    results = []
    for row in rows:
        try:
            results.append(process_webhook(row["payload"]))
        except Exception as exc:                # noqa: BLE001
            log.exception("webhook retry failed")
            results.append(f"error: {exc}")
    return results


# --------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------

def mark_unknown(order_id, reason):
    """Called when the application stops being sure what happened.

    Deliberately not PAYMENT_FAILED. Silence says nothing about whether money
    moved, and treating silence as failure is what produces double charges and
    abandoned paid orders.
    """
    core.set_order_state(order_id, "RECONCILIATION_REQUIRED")
    order = core.get_order(order_id)
    core.audit("PAYMENT_STATE_UNKNOWN", {"order_id": order_id,
                                         "reason": reason},
               merchant_id=order["merchant_id"] if order else None)
    return "RECONCILIATION_REQUIRED"


def fetch_authoritative_status(rp_order_id):
    """Ask the provider what it holds. The only source of truth."""
    if not rp_order_id:
        return []
    if LIVE:
        return _client.order.payments(rp_order_id).get("items", [])
    return SIM.payments_for_order(rp_order_id)


def reconcile(order_id):
    """Resolve an order whose payment state the application does not know.

    Reads only. This function never creates a payment, and that is the whole
    point of it.
    """
    order = core.get_order(order_id)
    if order is None:
        return f"unknown order {order_id}"

    merchant_id = order["merchant_id"]
    core.audit("RECONCILIATION_STARTED", {
        "order_id": order_id,
        "state_before": order["state"],
    }, merchant_id=merchant_id)

    try:
        payments = fetch_authoritative_status(order["rp_order_id"])
    except Exception as exc:                    # noqa: BLE001
        # Provider unreachable. Leave the order unresolved and try again on the
        # next sweep. Guessing here is what causes double charges.
        log.warning("reconciliation could not reach provider: %s", exc)
        core.audit("RECONCILIATION_DEFERRED", {
            "order_id": order_id, "error": str(exc),
        }, merchant_id=merchant_id)
        return f"order {order_id} deferred, provider unreachable"

    captured = [p for p in payments if p.get("status") == "captured"]

    if captured:
        payment = captured[0]
        core.commit_reservation(merchant_id, order_id)
        core.set_order_state(order_id, "CONFIRMED",
                             rp_payment_id=payment["id"])
        core.audit("RECONCILIATION_RESOLVED", {
            "order_id": order_id, "outcome": "CONFIRMED",
            "payments_seen": len(payments),
            "duplicate_charge_created": False,
        }, merchant_id=merchant_id)
        return f"order {order_id} confirmed by reconciliation"

    if payments:
        core.release(merchant_id, order_id)
        core.set_order_state(order_id, "PAYMENT_FAILED")
        core.audit("RECONCILIATION_RESOLVED", {
            "order_id": order_id, "outcome": "PAYMENT_FAILED",
            "payments_seen": len(payments),
            "duplicate_charge_created": False,
        }, merchant_id=merchant_id)
        return f"order {order_id} failed, verified against provider"

    # No payment exists at the provider at all. The customer never paid. The
    # hold stays until its TTL so the cart is not destroyed underneath them.
    core.audit("RECONCILIATION_RESOLVED", {
        "order_id": order_id, "outcome": "STILL_PENDING",
        "payments_seen": 0, "duplicate_charge_created": False,
    }, merchant_id=merchant_id)
    return f"order {order_id} still pending, no payment exists at provider"


def sweep(stale_after_seconds=None):
    """Reconcile every order that has been stuck longer than the threshold.

    This is what makes reconciliation a guarantee rather than a demo: no order
    depends on someone noticing it.
    """
    # `is None`, not `or`: a caller passing 0 means "sweep everything now",
    # and 0 is falsy, so `or` would silently substitute the default instead.
    stale_after = (config.RECONCILE_STALE_AFTER_SECONDS
                   if stale_after_seconds is None else stale_after_seconds)
    orders = core.unresolved_orders(stale_after)
    return [reconcile(o["id"]) for o in orders]


def charge_count(order_id):
    """Payments existing at the provider for this order. Tests assert this
    never exceeds one, whatever sequence of failures occurred."""
    order = core.get_order(order_id)
    if not order:
        return 0
    return len(fetch_authoritative_status(order["rp_order_id"]))


def build_webhook(rp_order_id, rp_payment_id, status="captured",
                  event_id=None):
    payload = {
        "id": event_id or ("evt_" + uuid.uuid4().hex[:14]),
        "event": "payment.captured" if status == "captured"
                 else "payment.failed",
        "payload": {"payment": {"entity": {
            "id": rp_payment_id,
            "order_id": rp_order_id,
            "status": status,
        }}},
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return payload, raw, sign(raw)
