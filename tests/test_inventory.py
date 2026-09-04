"""Stock cannot go negative, and a hold cannot be lost.

The concurrency test here is the reason the test suite needs a real database.
SELECT ... FOR UPDATE is what makes two buyers racing for the last unit
serialise; nothing about that behaviour can be observed against a mock.
"""

import threading

import core
import db
from orchestrator import start_purchase

MERCHANT = "test-merchant"
RARE = [{"sku": "RARE-01", "qty": 1}]


def stock_of(sku):
    return core.get_products(MERCHANT, [sku])[sku]["stock"]


def test_reservation_reduces_stock(merchant, make_order):
    make_order("ORD-TEST-1")
    before = stock_of("DCK-001")
    ok, _ = core.reserve(merchant, "ORD-TEST-1", [{"sku": "DCK-001", "qty": 2}])
    assert ok
    assert stock_of("DCK-001") == before - 2


def test_release_returns_stock(merchant, make_order):
    make_order("ORD-TEST-2")
    before = stock_of("DCK-001")
    core.reserve(merchant, "ORD-TEST-2", [{"sku": "DCK-001", "qty": 3}])
    core.release(merchant, "ORD-TEST-2")
    assert stock_of("DCK-001") == before


def test_committed_stock_is_not_returned(merchant, make_order):
    make_order("ORD-TEST-3")
    before = stock_of("DCK-001")
    core.reserve(merchant, "ORD-TEST-3", [{"sku": "DCK-001", "qty": 1}])
    core.commit_reservation(merchant, "ORD-TEST-3")
    core.release(merchant, "ORD-TEST-3")
    assert stock_of("DCK-001") == before - 1


def test_reservation_beyond_stock_is_refused(merchant, make_order):
    make_order("ORD-TEST-4")
    ok, reason = core.reserve(merchant, "ORD-TEST-4",
                              [{"sku": "RARE-01", "qty": 2}])
    assert not ok
    assert "insufficient stock" in reason
    assert stock_of("RARE-01") == 1


def test_partial_failure_reserves_nothing(merchant, make_order):
    """One unavailable line must not leave the other lines held."""
    make_order("ORD-TEST-5")
    before_dock = stock_of("DCK-001")
    ok, _ = core.reserve(merchant, "ORD-TEST-5", [
        {"sku": "DCK-001", "qty": 1},
        {"sku": "RARE-01", "qty": 5},
    ])
    assert not ok
    assert stock_of("DCK-001") == before_dock


def test_two_buyers_race_for_the_last_unit(merchant, make_order):
    """Both threads reach the reservation at the same moment. Exactly one may
    win, and stock must land on zero rather than minus one."""
    order_ids = [make_order(f"ORD-RACE-{i}") for i in range(2)]
    barrier = threading.Barrier(2)
    outcomes = []
    lock = threading.Lock()

    def attempt(order_id):
        barrier.wait()
        ok, reason = core.reserve(merchant, order_id, RARE)
        with lock:
            outcomes.append((order_id, ok, reason))

    threads = [threading.Thread(target=attempt, args=(order_id,))
               for order_id in order_ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    winners = [o for o in outcomes if o[1]]
    losers = [o for o in outcomes if not o[1]]

    assert len(outcomes) == 2
    assert len(winners) == 1, f"expected one winner, got {outcomes}"
    assert len(losers) == 1
    assert stock_of("RARE-01") == 0


def test_expired_hold_returns_stock(merchant, make_order):
    make_order("ORD-TEST-6")
    before = stock_of("DCK-001")
    core.reserve(merchant, "ORD-TEST-6", [{"sku": "DCK-001", "qty": 2}])
    assert stock_of("DCK-001") == before - 2

    # Age the hold past its TTL.
    db.execute("UPDATE reservations SET expires_at = now() - interval '1 hour' "
               "WHERE order_id = %s", ("ORD-TEST-6",))

    released = core.release_expired()

    assert released >= 1
    assert stock_of("DCK-001") == before


def test_expiry_does_not_touch_committed_holds(merchant, make_order):
    make_order("ORD-TEST-7")
    before = stock_of("DCK-001")
    core.reserve(merchant, "ORD-TEST-7", [{"sku": "DCK-001", "qty": 1}])
    core.commit_reservation(merchant, "ORD-TEST-7")
    db.execute("UPDATE reservations SET expires_at = now() - interval '1 hour' "
               "WHERE order_id = %s", ("ORD-TEST-7",))

    core.release_expired()

    assert stock_of("DCK-001") == before - 1


def test_database_refuses_negative_stock(merchant):
    """Last line of defence. Even if application logic were wrong, the column
    constraint stops the row from being written."""
    try:
        db.execute("UPDATE products SET stock = -1 WHERE merchant_id = %s "
                   "AND sku = %s", (merchant, "RARE-01"))
        raised = False
    except Exception:                           # noqa: BLE001
        raised = True
    assert raised, "database accepted negative stock"


def test_idempotent_purchase_returns_the_same_order(merchant):
    key = "checkout-attempt-1"
    first = start_purchase(merchant, "buyer@example.com", "a thunderbolt dock",
                           idempotency_key=key)
    second = start_purchase(merchant, "buyer@example.com", "a thunderbolt dock",
                            idempotency_key=key)

    assert first.ok and second.ok
    assert second.order_id == first.order_id
    assert second.stage == "already_created"
