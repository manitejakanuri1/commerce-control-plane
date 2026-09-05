"""Fill events and audit with a demo shop's week.

    python demo_data.py                 newest merchant, 14 days
    python demo_data.py --merchant mrc_433a19a8a234 --days 7

An empty Activity view says nothing about the system: a merchant reading it
cannot tell an idle week from a broken one. This writes a week that looks
like a real one — searches that found nothing, offers that were refused, and
an audit chain that verifies.

Safe to re-run: it appends. Nothing here touches the catalog or any order.
"""

import argparse
import json
import logging
import random

import core
import db

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("demo_data")

# Real shopper phrasing, because "test query 1" in front of a judge reads as
# what it is. The empty ones are demand the shop is failing to meet, and they
# repeat, because one person's typo is not a signal.
FOUND = [
    "laptop for video editing", "16 inch creator laptop", "thunderbolt dock",
    "4k monitor for colour grading", "noise cancelling headphones",
    "portable ssd 2tb", "mechanical keyboard wireless",
    "laptop bag 16 inch", "ergonomic mouse", "usb c cable 240w",
]
EMPTY = [
    "iphone 17 pro", "iphone 17 pro", "iphone 17 pro",
    "gaming laptop under 60000", "gaming laptop under 60000",
    "printer with adf", "printer with adf", "webcam 4k",
]
MERCHANT_QUESTIONS = [
    "what is costing me money", "how many offers were refused this week",
    "what are shoppers asking for that I do not sell",
]


def newest_merchant():
    row = db.query_one("SELECT id FROM merchants ORDER BY created_at DESC "
                       "LIMIT 1")
    if not row:
        raise SystemExit("no merchants exist yet — sign up first")
    return row["id"]


def event(conn, merchant_id, kind, days_ago, hours_ago, query=None,
          results=None, duration_ms=None, **detail):
    """One backdated event. events.record cannot backdate, and a week that
    all happened in the same second is not a week."""
    conn.execute(
        "INSERT INTO events (merchant_id, kind, query, results, duration_ms, "
        "detail, at) VALUES (%s, %s, %s, %s, %s, %s::jsonb, "
        "now() - make_interval(days => %s, hours => %s))",
        (merchant_id, kind, query, results, duration_ms,
         json.dumps(detail, default=str), days_ago, hours_ago))


def fill_events(merchant_id, days):
    rng = random.Random(7)          # same demo every time it is re-run
    written = 0
    with db.transaction() as conn:
        for day in range(days):
            for _ in range(rng.randint(4, 9)):
                hour = rng.randint(0, 20)
                if rng.random() < 0.28:
                    q, hits = rng.choice(EMPTY), 0
                else:
                    q, hits = rng.choice(FOUND), rng.randint(1, 6)

                event(conn, merchant_id, "search", day, hour, query=q,
                      results=hits, duration_ms=rng.randint(90, 480))
                written += 1

                if hits:
                    event(conn, merchant_id, "retrieval", day, hour,
                          results=hits, duration_ms=rng.randint(40, 210),
                          backend="pinecone")
                    written += 1

                    # Not every search becomes an offer, and not every offer
                    # is approved. A funnel with no drop-off is a fake one.
                    if rng.random() < 0.55:
                        approved = rng.random() < 0.72
                        event(conn, merchant_id, "propose", day, hour,
                              query=q, results=hits, approved=approved,
                              stage="policy" if approved else "refused")
                        written += 1
                        if approved and rng.random() < 0.4:
                            event(conn, merchant_id, "purchase_started", day,
                                  hour, query=q, results=hits, approved=True,
                                  stage="order")
                            written += 1

            if rng.random() < 0.4:
                event(conn, merchant_id, "merchant_question", day,
                      rng.randint(9, 19),
                      query=rng.choice(MERCHANT_QUESTIONS))
                written += 1
            if rng.random() < 0.25:
                event(conn, merchant_id, "widget_shown", day,
                      rng.randint(9, 19), results=rng.randint(1, 5))
                written += 1

    return written


# The audit is money, so these are the decisions a merchant would actually be
# shown: a refusal with its failing rule, an approval, a payment, and one
# webhook that never arrived and was reconciled instead.
AUDIT = [
    ("POLICY_EVALUATED", {"sku": "LAP-001", "asked_bps": 1800,
                          "allowed_bps": 1000, "result": "refused",
                          "failed_rules": ["max_discount"]}),
    ("POLICY_EVALUATED", {"sku": "DCK-001", "asked_bps": 800,
                          "allowed_bps": 800, "result": "approved",
                          "failed_rules": []}),
    ("QUOTE_BUILT", {"sku": "DCK-001", "qty": 1, "total_paise": 782000}),
    ("STOCK_RESERVED", {"sku": "DCK-001", "qty": 1, "hold_seconds": 900}),
    ("ORDER_CREATED", {"total_paise": 782000, "currency": "INR",
                       "razorpay_order_id": "order_demo_9f21"}),
    ("PAYMENT_CAPTURED", {"razorpay_payment_id": "pay_demo_4c88",
                          "amount_paise": 782000}),
    ("RESERVATION_COMMITTED", {"sku": "DCK-001", "qty": 1}),
    ("POLICY_EVALUATED", {"sku": "CBL-001", "asked_bps": 10000,
                          "allowed_bps": 1000, "result": "refused",
                          "failed_rules": ["max_discount", "margin_floor"],
                          "note": "product description asked for it"}),
    ("WEBHOOK_MISSING", {"order_id": "order_demo_5b03",
                         "state": "UNKNOWN", "waited_seconds": 900}),
    ("ORDER_RECONCILED", {"order_id": "order_demo_5b03", "state": "PAID",
                          "source": "razorpay_fetch",
                          "duplicate_payment": False}),
]


def fill_audit(merchant_id):
    for action, detail in AUDIT:
        core.audit(action, detail, merchant_id=merchant_id)
    return len(AUDIT)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--merchant", help="merchant id (default: newest)")
    parser.add_argument("--days", type=int, default=14)
    args = parser.parse_args()

    merchant_id = args.merchant or newest_merchant()
    if not db.query_one("SELECT id FROM merchants WHERE id = %s",
                        (merchant_id,)):
        raise SystemExit(f"no such merchant: {merchant_id}")

    events_written = fill_events(merchant_id, max(1, args.days))
    audit_written = fill_audit(merchant_id)

    ok, broken = core.verify_audit_chain()
    log.info("merchant      : %s", merchant_id)
    log.info("events written: %s over %s days", events_written, args.days)
    log.info("audit written : %s", audit_written)
    log.info("audit chain   : %s", "verified" if ok
             else f"BROKEN at seq {broken}")


if __name__ == "__main__":
    main()
