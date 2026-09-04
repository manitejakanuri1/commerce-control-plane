# commerce-policy

The pricing guardrail that runs on your server, not ours.

An AI shopping agent proposes a price. This package decides whether that price
may be charged. It holds your cost, it reads no English, and it is the only
thing standing between a language model's suggestion and a payment.

```
pip install commerce-policy
```

## Why it runs on your machine

Your supplier costs never leave it. We index your product names and prices —
which are already printed on your website — and we never receive a cost
figure, a margin, or a customer record. There is nothing for us to lose in a
breach because we were never given it.

The separation is enforced by a missing database grant rather than by careful
coding, so a bug in your website cannot read a cost either:

```bash
commerce-policy migrate \
  --storefront-role  your_web_app \
  --engine-role      policy_engine
```

That revokes all access to the `policy` schema from the role your public site
runs as. After it, `SELECT cost_paise FROM policy.economics` fails for your
website even if someone injects it.

## Use

```python
from commerce_policy import PolicyEngine

engine = PolicyEngine()
cart = [{"sku": "SAR-104", "qty": 1}]

# 1. What is the most we could ever discount this? Safe to hand to an agent:
#    it is a ceiling, not a cost.
band = engine.band(cart)                     # e.g. 1200  (12%)

# 2. The agent judged this shopper — a label, not a number.
bps = engine.offer(cart, tier=3)             # 900  (75% of the band)

# 3. The gate. Re-reads the database; does not trust step 1.
result = engine.check(cart, bps)

if result["approved"]:
    create_razorpay_order(...)
else:
    print(result["failed_rules"])            # ['margin_floor']
```

`check()` is the only method that decides anything. Never build a payment on
`band()` — a price can move, a unit can sell, and a cap can be tightened in
between.

## The five rules

| Rule | Authority | Blocks when |
|---|---|---|
| `discount_cap` | merchant | the discount exceeds your cap |
| `margin_floor` | merchant | profit after the discount falls below your floor |
| `floor_price` | merchant | a unit price falls below that product's floor |
| `inventory` | merchant | stock is short |
| `buyer_budget` | buyer | the total exceeds what the shopper stated |

Every rule reports one of three states, not two:

- `pass` — enforced, and satisfied
- `fail` — enforced, and violated. **Only this blocks.**
- `not_configured` — you have not supplied what this rule needs

The third exists because a green tick for a check that never ran is worse than
a red one. A shop that shares only a discount cap is still protected by that
cap, and can see plainly that margin is unproven.

## Sharing cost is optional

| You supply | Enforced |
|---|---|
| a discount cap | the cap |
| + a floor price per product | the cap, the floor |
| + real cost | the cap, the floor, true margin |

A floor price is a derived number — *"never sell this below Rs 2,400"*. It
reveals nothing about your supplier and still stops a discount running away.

```bash
commerce-policy set-cost SAR-104 --floor 240000      # paise, always
commerce-policy set-cost SAR-104 --cost 190000       # if you're willing
```

## Tiers

The agent supplies a tier — a judgement about the shopper. The table supplies
the number. Letting a model pick the rupee figure means the same shopper gets
a different price tomorrow and nobody can explain why.

| Tier | Shopper | Share of the band |
|---|---|---|
| 1 | first visit, browsing | 0% |
| 2 | returning buyer | 40% |
| 3 | abandoned cart | 75% |
| 4 | leaving, high-value basket | 100% |

A tier is a share **of the band**, never a fixed percentage. So a thin-margin
product protects itself with no special rule: on a saree with a 12% band,
tier 4 is 12%; on a shirt earning Rs 40 on Rs 800, tier 4 is 0%.

## The audit trail

Every decision is appended to `policy.decisions` on your server, hash-chained
so an edit is detectable, with a trigger that refuses `UPDATE` and `DELETE`.
Neither stops a superuser, so the honest description is tamper-evident, not
tamper-proof.

```bash
commerce-policy verify
```

Checks the connection, the schema, your grants, whether the role can write
when it should not, and recomputes the whole chain.

## What we receive

Exactly these fields, and the list is enforced in code
(`logger.SAFE_FIELDS`):

`merchant_id`, `sku`, `asked_bps`, `allowed_bps`, `result`, `failed_rules`,
`engine_version`, `at`

No cost. No price. No customer. Logging is queued and dropped under pressure —
it can never delay or fail a sale. Your own `policy.decisions` table is the
authoritative record; ours is a copy of the harmless part.

Set `"send_logs": false` to turn it off entirely. The engine works without us.

## Configuration

`policy.config.json` is meant to be committed — a reviewer should see your
discount cap in a pull request:

```json
{
  "merchant_id": "bazaar_001",
  "max_discount_bps": 1000,
  "min_margin_bps": 2000,
  "products_table": "products",
  "sku_column": "sku",
  "price_column": "price",
  "stock_column": "stock"
}
```

Secrets are read from the environment, because anything committable eventually
gets committed:

```
POLICY_DB_URL=postgresql://policy_engine:...@localhost/shop
COMMERCE_POLICY_API_KEY=ccp_live_...
```

If `policy.rules` has a row for your merchant, it wins over the file — so an
operator can tighten a cap without a deploy.

## Versions

The engine sends its version with every request. Because this runs on your
server, we cannot push a fix to it; the only lever we have is to refuse a
release we have found a fault in, and tell you why:

```
commerce-policy 1.0.0 is no longer accepted.
Run: pip install -U commerce-policy
```

## What is not in here

No model, no API key for one, no prompt, no retrieval, no Razorpay code, no
customer data. Roughly four hundred lines of integer arithmetic and a log
sender.

Money is `int` paise everywhere. `270000` means Rs 2,700.00 and nothing else.
A float would eventually round a sale in someone's favour and nobody would
notice for months.

## Tests

```
pytest packages/commerce-policy
```

Everything in `rules.py` is a pure function of integers, so the part that
decides whether money moves is provable offline, at a desk, in under a second.
