"""Commerce Orchestrator.

Coordinates the specialists and executes nothing itself:

    proposal   agent, untrusted, carries no prices
    policy     deterministic gate, merchant rules are final
    quote      prices read from the database
    inventory  stock held under a row lock, with a TTL
    order      local record
    payment    Razorpay order created
    webhook    confirmation, or silence
    reconcile  when silence happens, ask the provider, never charge again
"""

import logging
import uuid

import agent
import core
import db
import payments

log = logging.getLogger("orchestrator")


class Result:
    def __init__(self, ok, stage, message, **extra):
        self.ok = ok
        self.stage = stage
        self.message = message
        self.__dict__.update(extra)

    def as_dict(self):
        return {k: v for k, v in self.__dict__.items()}

    def __repr__(self):
        return (f"<Result {'ok' if self.ok else 'stopped'} "
                f"at {self.stage}: {self.message}>")


def new_order_id():
    return "ORD-" + uuid.uuid4().hex[:10].upper()


def propose_offer(merchant_id, request, budget_paise=None,
                  forced_discount_bps=None):
    """Everything start_purchase does, stopping before anything is committed.

    No stock is held and no payment order is created, so this is safe to expose
    to a storefront key. It is what lets a shop show an offer without being
    able to spend on the merchant's behalf.
    """
    proposal = agent.propose(merchant_id, request, budget_paise)
    if forced_discount_bps is not None:
        proposal["discount_bps"] = forced_discount_bps

    decision = core.evaluate_policy(
        merchant_id, proposal["lines"], proposal["discount_bps"],
        proposal.get("budget_paise"))

    if not decision["approved"]:
        failed = [c for c in decision["checks"] if c["status"] == "fail"]
        return Result(
            False, "policy",
            "; ".join(f"{c['rule']} failed ({c['detail']})" for c in failed),
            proposal=proposal, decision=decision)

    quote = core.build_quote(merchant_id, proposal["lines"],
                             proposal["discount_bps"])
    return Result(True, "proposed",
                  f"payable {core.rupees(quote['total_paise'])}",
                  proposal=proposal, decision=decision, quote=quote)


def start_purchase(merchant_id, buyer, request, budget_paise=None,
                   idempotency_key=None, forced_discount_bps=None):
    """Run intent through to a payable Razorpay order.

    forced_discount_bps bypasses the agent entirely and submits a discount
    directly. It exists so tests can prove the gate holds no matter where a
    request originated — a compromised agent, a bug, or a hostile caller.
    """
    if idempotency_key:
        existing = db.query_one(
            "SELECT * FROM orders WHERE merchant_id = %s "
            "AND idempotency_key = %s", (merchant_id, idempotency_key))
        if existing:
            return Result(True, "already_created",
                          f"idempotent replay of {existing['id']}",
                          order_id=existing["id"],
                          rp_order_id=existing["rp_order_id"])

    order_id = new_order_id()
    proposal = agent.propose(merchant_id, request, budget_paise)
    if forced_discount_bps is not None:
        proposal["discount_bps"] = forced_discount_bps

    decision = core.evaluate_policy(
        merchant_id, proposal["lines"], proposal["discount_bps"],
        proposal.get("budget_paise"))

    if not decision["approved"]:
        failed = [c for c in decision["checks"] if c["status"] == "fail"]
        return Result(
            False, "policy",
            "; ".join(f"{c['rule']} failed ({c['detail']})" for c in failed),
            order_id=order_id, proposal=proposal, decision=decision)

    quote = core.build_quote(merchant_id, proposal["lines"],
                             proposal["discount_bps"])

    # The order row must exist before stock is held against it: reservations
    # carry a foreign key to orders, so that a hold can never outlive or
    # reference an order that was never recorded.
    core.create_order(merchant_id, order_id, buyer, quote,
                      idempotency_key=idempotency_key)

    reserved, reason = core.reserve(merchant_id, order_id, proposal["lines"])
    if not reserved:
        core.set_order_state(order_id, "PAYMENT_FAILED")
        return Result(False, "inventory", reason, order_id=order_id,
                      proposal=proposal, decision=decision, quote=quote)

    try:
        rp_order = payments.create_payment_order(
            merchant_id, order_id, quote["total_paise"])
    except Exception:
        # Never strand stock because the payment leg failed.
        core.release(merchant_id, order_id)
        core.set_order_state(order_id, "PAYMENT_FAILED")
        log.exception("payment order creation failed for %s", order_id)
        raise

    return Result(True, "awaiting_payment",
                  f"payable {core.rupees(quote['total_paise'])}",
                  order_id=order_id, proposal=proposal, decision=decision,
                  quote=quote, rp_order_id=rp_order["id"])


def payment_went_silent(order_id, reason="webhook not received"):
    """The application has lost track. Record that, do not guess."""
    return payments.mark_unknown(order_id, reason)


def resolve(order_id):
    return payments.reconcile(order_id)
