# Merchant Agent Commerce Control Plane

Razorpay moves the money. This service decides how the merchant safely sells.

An AI buyer can negotiate with a merchant here. It cannot negotiate away the
merchant's economics, and it cannot cause a customer to be charged twice.

Built for Razorpay AI Buildathon, Track 01 — AI Growth & Agentic Commerce.

---

## The idea in one paragraph

A language model proposes what to sell. A deterministic policy engine decides
whether that proposal is allowed. A quote engine calculates the price from the
database, never from anything the model or a product description said. Stock is
held under a row lock with a timeout. Razorpay takes the payment. When the
payment webhook goes missing — which it does — the system records that it does
not know what happened, asks Razorpay directly, and never charges again.

## Flow

```
AI buyer or human
      |
      v
API gateway            auth by merchant API key, rate limited
      |
      v
Agent                  reads untrusted catalog text, proposes skus + discount
      |                PROPOSAL ONLY - carries no prices
      v
Policy engine          discount cap | margin floor | stock | buyer budget
      |                merchant rules are authoritative; buyer budget filters
      +--- reject ---> stop, nothing reserved, nothing charged
      |
      v
Quote engine           prices read from the products table only
      |
      v
Inventory              SELECT ... FOR UPDATE, held with a TTL
      |
      v
Razorpay               order created, customer pays
      |
      +---- webhook ----+---- silence ----+
      |                                   |
      v                                   v
Confirmed                        RECONCILIATION_REQUIRED
                                          |
                                          v
                                 Ask Razorpay what is true
                                 Never create a second payment
                                          |
                                          v
                                 Confirmed / failed / still pending
      |
      v
Audit trail            hash-chained, append-only, alerts on pressure
```

## Authority boundaries

| Component | May | May never |
|---|---|---|
| Agent (LLM) | Propose skus, quantities, a discount to request | Set a price, approve a discount, confirm stock, change policy |
| Retrieval | Return candidate skus | Be a source of price or stock |
| Policy engine | Approve or reject | Invent a figure not derived from the catalog |
| Quote engine | Calculate from catalog prices | Accept a price from any caller |
| Inventory | Reserve, release, commit | Allow stock below zero |
| Razorpay | Execute payment, hold authoritative state | Be second-guessed by a local assumption |
| Reconciler | Read provider state and adopt it | Create a payment |

This service never holds, routes, or settles funds. It creates Razorpay orders
and reads payment status. Holding customer money in India requires a payment
aggregator licence; staying on this side of that line is deliberate.

## Running it

Requires Python 3.11+ and PostgreSQL.

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in DATABASE_URL at minimum
python seed.py                # migrates, creates a merchant, loads a catalog
```

`seed.py` prints an API key once. Keep it — only its hash is stored.

```bash
uvicorn app:app --reload      # API on http://127.0.0.1:8000
python workers.py             # expiry + reconciliation sweeps, separate process
```

Ask to buy something:

```bash
curl -X POST http://127.0.0.1:8000/v1/purchase \
  -H "X-API-Key: <the key seed.py printed>" \
  -H "Content-Type: application/json" \
  -d '{"buyer":"buyer@example.com",
       "request":"a laptop for video editing under 150000"}'
```

The response carries every policy check with its verdict and reasoning, not
just an approval.

### What runs without which keys

| Missing | Behaviour |
|---|---|
| `RAZORPAY_*` | Built-in simulator, with the same failure paths |
| `PINECONE_API_KEY` | PostgreSQL full-text search |
| `ANTHROPIC_API_KEY` | Deterministic bundler |
| `DATABASE_URL` | Refuses to start — there is no fallback for the ledger |

Retrieval and merchandising degrade. The money path does not.

## Tests

```bash
export TEST_DATABASE_URL=postgresql://...   # a throwaway database
pytest -v
```

The suite runs against real PostgreSQL because the properties worth proving are
database properties. `test_inventory.py` runs two threads into the same last
unit; `test_reconciliation.py` asserts `charge_count == 1` after every failure
mode.

Covered:

- Discount cap, margin floor, and their interaction
- Buyer budget classified separately from merchant authority
- Prices unchanged by a hostile product description
- Over-cap discount refused even when submitted directly, bypassing the agent
- Refused purchases leave no order and no held stock
- Two buyers, one unit, exactly one winner
- Expired holds return stock; committed holds do not
- Lost webhook, duplicate webhook, out-of-order webhook, unpaid order
- Reconciliation run five times still produces one charge
- Audit chain verification and append-only enforcement

## Retrieval

Pinecone serverless with integrated embeddings, one namespace per merchant.
PostgreSQL full-text search is the fallback and the local path.

Pinecone metadata is a search index and can lag the database, so a hit is only
ever used to look the product up again. Price and stock come from PostgreSQL.

## Design notes

**Money is integers.** Paise in `BIGINT`, no floating point anywhere.

**Rules are checked twice.** The policy engine decides, then the quote engine
re-checks the cap and the margin floor. A bug that skipped the gate still
cannot produce an out-of-policy quote.

**Silence is not failure.** A missing webhook says nothing about whether money
moved. Treating it as failure is what produces double charges and paid orders
that never ship.

**The audit trail is a feedback loop.** Repeated reconciliation raises an
alert, because it means webhook delivery is degrading before customers notice.

**Tamper-evident, not tamper-proof.** Records are hash-chained and the table
refuses UPDATE and DELETE. Someone with superuser access to the database could
still rebuild the chain. The honest word is tamper-evident.

## Known limits

- Single gateway, single currency, one merchant per API key.
- Rate limiting is in-process; move it to Redis before running replicas.
- Reconciliation covers payment state, not settlement files. Settlement
  reconciliation across many gateways is a substantially larger problem.
- Partial captures and refunds are not modelled yet.
- Pinecone sync is manual or nightly; a catalog change is not instantly
  searchable, though it is instantly priceable.
