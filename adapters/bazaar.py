"""Adapter for the Bazaar storefront (bazzer-store.vercel.app).

Bazaar is a static site: plain HTML plus one shop.js holding a PRODUCTS array.
There is no backend and no API, which makes it a fair test of the claim that
this system integrates with any storefront — it is close to the least
cooperative shape a real merchant can have.

Run it:

    python -m adapters.bazaar --file shop.js --merchant acme-electronics
    python -m adapters.bazaar --url https://bazzer-store.vercel.app/shop.js \\
        --merchant acme-electronics

What this adapter does NOT invent
---------------------------------
cost_paise. Bazaar publishes `price` and `mrp`, and `mrp` is the struck-through
"was" price, not what the merchant pays. No storefront publishes its cost; it
is commercially sensitive, and every shop keeps it out of the page.

So cost is left empty. The merchant does not have to reveal their margin — they
state their limit instead, which they worked out from that margin privately:
their discount cap, and optionally a floor price per product. Both bind just as
hard, and neither exposes what they pay.

Getting cost wrong would be worse than leaving it out: the policy engine would
enforce a fabricated margin with total confidence, approving discounts that
lose money on every sale, and it would be right to, because it was told the
wrong thing.
"""

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import catalog        # noqa: E402
import core           # noqa: E402
import db             # noqa: E402


def extract_products(source):
    """Pull the PRODUCTS array out of shop.js.

    The file is JavaScript, not JSON: keys are unquoted and strings are single
    quoted, so it is converted before parsing. Deliberately not eval'd — this
    is third-party code and running it would hand a merchant's site execution
    inside the importer.
    """
    match = re.search(r"const\s+PRODUCTS\s*=\s*(\[.*?\n\];)", source, re.DOTALL)
    if not match:
        raise ValueError("could not find the PRODUCTS array in the source")

    body = match.group(1).rstrip(";")

    # Quote bare object keys:  {id:'e1',  ->  {"id":'e1',
    body = re.sub(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:", r'\1"\2":', body)
    # Single-quoted strings to double-quoted, preserving any inner doubles.
    body = re.sub(r"'((?:[^'\\]|\\.)*)'",
                  lambda m: json.dumps(m.group(1).replace('\\"', '"')), body)
    # Trailing commas before a closing bracket.
    body = re.sub(r",(\s*[\]}])", r"\1", body)

    return json.loads(body)


def to_products(raw, floor_bps=None):
    """Map Bazaar's shape onto the catalog schema.

    Everything descriptive is folded into one description field, because that
    is what retrieval embeds. Brand, department and the bullet list all help an
    AI buyer find the right product from a vague request.

    cost_paise is left None. floor_price_paise is set only when the merchant
    states a maximum markdown, which is a number they will share.
    """
    products = []
    for item in raw:
        parts = [item.get("brand", ""), item.get("cat", "")]
        parts += item.get("bullets", [])
        description = ". ".join(p for p in parts if p)

        price_paise = int(round(float(item["price"]) * 100))
        floor = (price_paise * (10000 - floor_bps) // 10000
                 if floor_bps else None)

        products.append({
            "sku": item["id"],
            "name": item["name"],
            "description": description,
            "price_paise": price_paise,
            "cost_paise": None,
            "floor_price_paise": floor,
            "stock": int(item.get("stock", 0)),
        })
    return products


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="path to a local copy of shop.js")
    source.add_argument("--url", help="URL of shop.js")
    parser.add_argument("--merchant", required=True,
                        help="merchant id to import into")
    parser.add_argument("--max-markdown-bps", type=int, default=None,
                        help="the merchant's stated maximum markdown in basis "
                             "points, e.g. 1200 for 12%%. Sets a floor price "
                             "per product. Omit if the merchant has only given "
                             "a discount cap.")
    parser.add_argument("--replace", action="store_true",
                        help="deactivate products absent from this import")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be imported and stop")
    args = parser.parse_args()

    if args.file:
        source_text = Path(args.file).read_text(encoding="utf-8")
    else:
        import urllib.request
        with urllib.request.urlopen(args.url, timeout=30) as response:
            source_text = response.read().decode("utf-8")

    raw = extract_products(source_text)
    products = to_products(raw, args.max_markdown_bps)

    print(f"parsed {len(products)} products from Bazaar")
    print()
    print("cost_paise  : not supplied, and not invented. Bazaar does not "
          "publish cost, and no storefront does.")
    if args.max_markdown_bps:
        print(f"floor_price : set at {args.max_markdown_bps / 100:.1f}% below "
              f"list, per the merchant's stated maximum markdown.")
    else:
        print("floor_price : not set. The merchant's discount cap is the only "
              "limit binding these products.")
    print()

    for product in products[:5]:
        floor = (core.rupees(product["floor_price_paise"])
                 if product["floor_price_paise"] else "-")
        print(f"  {product['sku']:<6} {product['name'][:40]:<42} "
              f"{core.rupees(product['price_paise']):>13}  "
              f"floor {floor:>13}  stock {product['stock']:>3}")
    if len(products) > 5:
        print(f"  ... and {len(products) - 5} more")

    if args.dry_run:
        print()
        print("dry run, nothing written")
        return

    print()
    result = catalog.import_products(args.merchant, products,
                                     replace=args.replace)
    print("imported       :", result["imported"])
    print("deactivated    :", result["deactivated"])
    print("vectors synced :", result["vectors_synced"])
    db.close()


if __name__ == "__main__":
    main()
