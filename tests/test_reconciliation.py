"""A payment is never charged twice, whatever the provider does or fails to do.

Four ways an application loses track of a payment, and the same rule applies to
all of them: silence is not failure, and the remedy is to read provider state,
never to charge again.
"""

import core
import payments
from orchestrator import payment_went_silent, resolve


def test_webhook_never_arrives_and_reconciliation_confirms(paid_order):
    result, rp_payment_id = paid_order(deliver_webhook=False)

    # The application does not know what happened.
    payment_went_silent(result.order_id, "webhook not received")
    assert core.get_order(result.order_id)["state"] == "RECONCILIATION_REQUIRED"

    resolve(result.order_id)

    order = core.get_order(result.order_id)
    assert order["state"] == "CONFIRMED"
    assert order["rp_payment_id"] == rp_payment_id
    assert payments.charge_count(result.order_id) == 1


def test_reconciliation_never_creates_a_second_charge(paid_order):
    result, _ = paid_order(deliver_webhook=False)
    payment_went_silent(result.order_id)

    # Run it repeatedly, the way a sweep on a timer would.
    for _ in range(5):
        resolve(result.order_id)

    assert payments.charge_count(result.order_id) == 1
    assert core.get_order(result.order_id)["state"] == "CONFIRMED"


def test_duplicate_webhook_is_processed_once(paid_order):
    result, rp_payment_id = paid_order(deliver_webhook=False)
    payload, raw, signature = payments.build_webhook(
        result.rp_order_id, rp_payment_id)

    first = payments.handle_webhook(payload, signature=signature, raw_body=raw)
    second = payments.handle_webhook(payload, signature=signature, raw_body=raw)
    third = payments.handle_webhook(payload, signature=signature, raw_body=raw)

    assert "confirmed" in first
    assert "duplicate" in second
    assert "duplicate" in third
    assert payments.charge_count(result.order_id) == 1


def test_out_of_order_event_triggers_reconciliation_not_a_state_change(
        paid_order):
    """A late failure event arriving after confirmation must not undo the sale.
    The system re-reads provider state instead of applying the transition."""
    result, rp_payment_id = paid_order(deliver_webhook=True)
    assert core.get_order(result.order_id)["state"] == "CONFIRMED"

    payload, raw, signature = payments.build_webhook(
        result.rp_order_id, rp_payment_id, status="failed")
    outcome = payments.handle_webhook(payload, signature=signature,
                                      raw_body=raw)

    assert "out-of-order" in outcome
    assert core.get_order(result.order_id)["state"] == "CONFIRMED"
    assert payments.charge_count(result.order_id) == 1


def test_unpaid_order_is_not_marked_failed_by_reconciliation(paid_order):
    """No payment exists at the provider. The customer simply has not paid, so
    the order stays open and the cart is not destroyed underneath them."""
    from orchestrator import start_purchase
    result = start_purchase("test-merchant", "buyer@example.com",
                            "a thunderbolt dock")
    payment_went_silent(result.order_id)

    outcome = resolve(result.order_id)

    assert "still pending" in outcome
    assert core.get_order(result.order_id)["state"] == "RECONCILIATION_REQUIRED"
    assert payments.charge_count(result.order_id) == 0


def test_failed_payment_releases_stock(paid_order):
    from orchestrator import start_purchase
    merchant = "test-merchant"
    before = core.get_products(merchant, ["DCK-001"])["DCK-001"]["stock"]

    result = start_purchase(merchant, "buyer@example.com", "a thunderbolt dock")
    held = core.get_products(merchant, ["DCK-001"])["DCK-001"]["stock"]
    assert held < before

    rp_payment_id, _ = payments.SIM.pay(result.rp_order_id, outcome="failed")
    payload, raw, signature = payments.build_webhook(
        result.rp_order_id, rp_payment_id, status="failed")
    payments.handle_webhook(payload, signature=signature, raw_body=raw)

    assert core.get_order(result.order_id)["state"] == "PAYMENT_FAILED"
    assert core.get_products(merchant, ["DCK-001"])["DCK-001"]["stock"] == before


def test_sweep_resolves_orders_without_anyone_asking(paid_order):
    """The guarantee: no order depends on a person noticing it is stuck."""
    result, _ = paid_order(deliver_webhook=False)
    payment_went_silent(result.order_id)

    outcomes = payments.sweep(stale_after_seconds=0)

    assert any(result.order_id in line for line in outcomes)
    assert core.get_order(result.order_id)["state"] == "CONFIRMED"
    assert payments.charge_count(result.order_id) == 1


def test_webhook_with_a_bad_signature_is_rejected(paid_order):
    result, rp_payment_id = paid_order(deliver_webhook=False)
    payload, raw, _ = payments.build_webhook(result.rp_order_id, rp_payment_id)

    outcome = payments.handle_webhook(payload, signature="not-the-signature",
                                      raw_body=raw)

    assert "rejected" in outcome
    assert core.get_order(result.order_id)["state"] == "AWAITING_PAYMENT"


def test_reconciliation_pressure_is_visible_to_operators(paid_order):
    for _ in range(3):
        result, _ = paid_order(deliver_webhook=False)
        payment_went_silent(result.order_id)
        resolve(result.order_id)

    assert core.reconciliation_pressure(window_minutes=15) >= 3
