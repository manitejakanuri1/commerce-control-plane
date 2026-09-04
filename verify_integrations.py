"""Prove the external integrations actually work.

Creates a real order in Razorpay test mode and a real Pinecone index, then
searches it. Everything it creates is disposable.
"""

import json
import time

import config
import core
import db
import payments
import retrieval

MERCHANT = "acme-electronics"

print("=" * 62)
print("CONFIG")
print("=" * 62)
print(json.dumps(config.summary(), indent=2))

# ---------------------------------------------------------------- razorpay
print()
print("=" * 62)
print("RAZORPAY")
print("=" * 62)
print("mode:", payments.mode())

if payments.LIVE:
    try:
        rp_order = payments._client.order.create({
            "amount": 850000,
            "currency": "INR",
            "receipt": "verify-" + str(int(time.time())),
            "payment_capture": 1,
            "notes": {"purpose": "integration check"},
        })
        print("order created at Razorpay:")
        print("  id      :", rp_order["id"])
        print("  amount  :", core.rupees(rp_order["amount"]))
        print("  status  :", rp_order["status"])
        print("  receipt :", rp_order["receipt"])
        print()
        print("Visible in your dashboard under Transactions > Orders.")
    except Exception as exc:                    # noqa: BLE001
        print("FAILED:", exc)
else:
    print("not live - keys missing or client failed to load")

# ---------------------------------------------------------------- pinecone
print()
print("=" * 62)
print("PINECONE")
print("=" * 62)
print("enabled:", config.PINECONE_ENABLED)

if config.PINECONE_ENABLED:
    try:
        print("connecting and creating index if absent "
              "(first run can take a minute)...")
        index = retrieval.index()
        print("index ready:", config.PINECONE_INDEX)

        count = retrieval.sync_merchant(MERCHANT)
        print("products upserted:", count)

        print("waiting for the index to become queryable...")
        hits = []
        for attempt in range(12):
            time.sleep(5)
            hits = retrieval._search_pinecone(
                MERCHANT, "laptop for video editing", 3, True)
            if hits:
                break
            print(f"  not indexed yet (attempt {attempt + 1}/12)")

        if hits:
            print("semantic search returned:")
            for product in hits:
                print(f"  - {product['sku']}  {product['name']}  "
                      f"{core.rupees(product['price_paise'])}")
        else:
            print("no hits yet - indexing can lag; postgres fallback covers "
                  "this, rerun later to confirm")

        print()
        print("health:", retrieval.health())
    except Exception as exc:                    # noqa: BLE001
        print("FAILED:", type(exc).__name__, exc)
else:
    print("not enabled - using postgres full-text search")

# ---------------------------------------------------------------- fallback
print()
print("=" * 62)
print("POSTGRES FALLBACK (always available)")
print("=" * 62)
rows = retrieval._search_postgres(MERCHANT, "laptop for video editing", 3, True)
for product in rows:
    print(f"  - {product['sku']}  {product['name']}  "
          f"{core.rupees(product['price_paise'])}")

db.close()
