"""Catalog retrieval.

Pinecone serverless with integrated embeddings is the production path, and
PostgreSQL full-text search is the fallback. The fallback is not decoration:
if Pinecone is unreachable the storefront must degrade to worse search rather
than stop selling, so every call here is wrapped and falls through.

Everything this module returns is UNTRUSTED. Product text is merchant-supplied
and can be hostile. The only field downstream code may act on is the sku.
Prices are always read again by core.build_quote.
"""

import logging

import config
import db

log = logging.getLogger("retrieval")

_index = None
_unavailable_logged = False

NAMESPACE_PREFIX = "merchant-"


def _client():
    from pinecone import Pinecone
    return Pinecone(api_key=config.PINECONE_API_KEY)


def index():
    """Lazily connect, creating the index on first use.

    Uses an integrated-embedding index so Pinecone runs the embedding model.
    That removes a second vendor from the request path, which is one fewer
    thing to fail while a customer is waiting.
    """
    global _index
    if _index is not None:
        return _index
    if not config.PINECONE_ENABLED:
        return None

    pc = _client()
    existing = {i["name"] for i in pc.list_indexes()}
    if config.PINECONE_INDEX not in existing:
        log.info("creating pinecone index %s", config.PINECONE_INDEX)
        pc.create_index_for_model(
            name=config.PINECONE_INDEX,
            cloud=config.PINECONE_CLOUD,
            region=config.PINECONE_REGION,
            embed={
                "model": config.PINECONE_EMBED_MODEL,
                "field_map": {"text": "text"},
            },
        )
    _index = pc.Index(config.PINECONE_INDEX)
    return _index


def namespace(merchant_id):
    """One namespace per merchant. This is the tenant boundary in Pinecone —
    a query in one namespace cannot return another merchant's catalog."""
    return f"{NAMESPACE_PREFIX}{merchant_id}"


def _record(product):
    return {
        "_id": product["sku"],
        "text": f"{product['name']}. {product['description']}",
        "sku": product["sku"],
        "price_paise": int(product["price_paise"]),
        "in_stock": product["stock"] > 0,
    }


def sync_merchant(merchant_id, batch_size=96):
    """Make the index match the catalog — additions and removals both.

    Upsert alone is not a sync. This only ever added, so a product that left
    the catalog stayed searchable forever: the index drifted to 31 vectors
    against 5 real products, and a shopper could be shown something that no
    longer existed and be sent to a checkout that would fail.

    Removals run after the upserts, so at no point is a live product missing
    from the index. A delete that fails is logged rather than raised — a stale
    vector is a bad search result, and losing the import that triggered this
    would be worse.
    """
    target = index()
    if target is None:
        log.info("pinecone disabled, skipping sync for %s", merchant_id)
        return 0

    rows = db.query(
        "SELECT sku, name, description, price_paise, stock FROM products "
        "WHERE merchant_id = %s AND active", (merchant_id,))

    ns = namespace(merchant_id)
    sent = 0
    for start in range(0, len(rows), batch_size):
        batch = [_record(r) for r in rows[start:start + batch_size]]
        target.upsert_records(namespace=ns, records=batch)
        sent += len(batch)

    removed = _remove_absent(target, ns, {str(r["sku"]) for r in rows})
    log.info("synced %s products to pinecone namespace %s (%s removed)",
             sent, ns, removed)
    return sent


def _remove_absent(target, ns, keep):
    """Delete vectors whose sku is no longer in the catalog.

    The id listing is paginated and the SDK has changed its shape before, so
    both a plain iterable of ids and pages of ids are handled.
    """
    try:
        stale = [vector_id for vector_id in _all_ids(target, ns)
                 if vector_id not in keep]
    except Exception as exc:                    # noqa: BLE001
        log.warning("could not list vectors in %s (%s: %s); stale entries "
                    "may remain", ns, type(exc).__name__, exc)
        return 0

    for start in range(0, len(stale), 1000):
        try:
            target.delete(ids=stale[start:start + 1000], namespace=ns)
        except Exception as exc:                # noqa: BLE001
            log.warning("pinecone delete failed in %s: %s", ns, exc)
            return start
    return len(stale)


def _all_ids(target, ns):
    for page in target.list(namespace=ns):
        # A page is a list of ids; some SDK versions yield the ids directly.
        if isinstance(page, str):
            yield page
        else:
            yield from page


def prune_namespaces(dry_run=True):
    """Delete namespaces belonging to merchants that no longer exist.

    A deleted merchant leaves its whole namespace behind. Nothing can read it
    — every search is scoped to a live merchant's own namespace, so this is
    cost and clutter rather than a correctness problem. Worth saying that
    plainly: it looked at first like shoppers could be shown products that had
    been removed, and they cannot.

    Defaults to dry_run because the failure mode is deleting a live merchant's
    catalog from the index over a mistaken id, which is silent until someone
    searches.
    """
    target = index()
    if target is None:
        return {"orphans": [], "deleted": 0, "dry_run": dry_run}

    live = {namespace(row["id"])
            for row in db.query("SELECT id FROM merchants")}
    stats = target.describe_index_stats()
    present = (stats.get("namespaces") or {})

    orphans = [
        {"namespace": name, "vectors": info.get("vector_count", 0)}
        for name, info in present.items()
        if name and name not in live
    ]

    deleted = 0
    if not dry_run:
        for orphan in orphans:
            try:
                target.delete(delete_all=True, namespace=orphan["namespace"])
                deleted += 1
                log.info("deleted orphaned namespace %s (%s vectors)",
                         orphan["namespace"], orphan["vectors"])
            except Exception as exc:            # noqa: BLE001
                log.warning("could not delete namespace %s: %s",
                            orphan["namespace"], exc)

    return {"orphans": orphans, "deleted": deleted, "dry_run": dry_run}


def remove_product(merchant_id, sku):
    target = index()
    if target is None:
        return
    try:
        target.delete(ids=[sku], namespace=namespace(merchant_id))
    except Exception as exc:                    # noqa: BLE001
        log.warning("pinecone delete failed for %s: %s", sku, exc)


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------

def search(merchant_id, query, limit=6, in_stock_only=True):
    """Return candidate products. Never raises; degrades instead."""
    if config.PINECONE_ENABLED:
        hits = _search_pinecone(merchant_id, query, limit, in_stock_only)
        if hits:
            return hits
    return _search_postgres(merchant_id, query, limit, in_stock_only)


def _hit_ids(response):
    """Pull sku ids out of a search response.

    The SDK returns typed objects (SearchRecordsResponse -> SearchResult ->
    Hit), while the REST shape is nested dicts. Both are handled because the
    client library has already changed this once.
    """
    result = getattr(response, "result", None)
    if result is None and isinstance(response, dict):
        result = response.get("result", {})
    if result is None:
        return []

    hits = getattr(result, "hits", None)
    if hits is None and isinstance(result, dict):
        hits = result.get("hits", [])
    if not hits:
        return []

    ids = []
    for hit in hits:
        identifier = getattr(hit, "id", None)
        if identifier is None and isinstance(hit, dict):
            identifier = hit.get("_id") or hit.get("id")
        if identifier:
            ids.append(identifier)
    return ids


def _search_pinecone(merchant_id, query, limit, in_stock_only):
    global _unavailable_logged
    target = index()
    if target is None:
        return []

    try:
        query_spec = {"inputs": {"text": query}, "top_k": limit}
        if in_stock_only:
            query_spec["filter"] = {"in_stock": True}

        response = target.search(namespace=namespace(merchant_id),
                                 query=query_spec)
        skus = _hit_ids(response)
        if not skus:
            return []
        _unavailable_logged = False
        return _hydrate(merchant_id, skus)

    except Exception as exc:                    # noqa: BLE001
        # Search degrading must never take the storefront down with it. The
        # exception type is logged because this handler once hid a parsing
        # bug of ours behind a message claiming Pinecone was unavailable.
        if not _unavailable_logged:
            log.warning("pinecone search failed (%s: %s), "
                        "falling back to postgres",
                        type(exc).__name__, exc)
            _unavailable_logged = True
        return []


def _search_postgres(merchant_id, query, limit, in_stock_only):
    stock_clause = "AND stock > 0" if in_stock_only else ""
    rows = db.query(
        f"""
        SELECT *, ts_rank(search_tsv, websearch_to_tsquery('english', %s)) AS rank
        FROM products
        WHERE merchant_id = %s AND active {stock_clause}
          AND search_tsv @@ websearch_to_tsquery('english', %s)
        ORDER BY rank DESC
        LIMIT %s
        """,
        (query, merchant_id, query, limit))

    if rows:
        return [dict(r) for r in rows]

    # Nothing matched the phrasing. Return the merchant's headline stock so the
    # agent always has something legitimate to propose.
    return [dict(r) for r in db.query(
        f"SELECT * FROM products WHERE merchant_id = %s AND active "
        f"{stock_clause} ORDER BY price_paise DESC LIMIT %s",
        (merchant_id, limit))]


def _hydrate(merchant_id, skus):
    """Read the authoritative product rows for skus Pinecone returned.

    Pinecone metadata is a search index and can lag the database, so it is
    never the source of price or stock.
    """
    rows = db.query(
        "SELECT * FROM products WHERE merchant_id = %s AND sku = ANY(%s) "
        "AND active", (merchant_id, skus))
    by_sku = {r["sku"]: dict(r) for r in rows}
    return [by_sku[s] for s in skus if s in by_sku]


def as_context(products):
    """Render candidates for the model prompt.

    This text is merchant-supplied and reaches the model verbatim. It is
    labelled untrusted at the call site for the same reason.
    """
    return "\n".join(
        f"- sku={p['sku']} | {p['name']} | price_paise={p['price_paise']} "
        f"| stock={p['stock']}\n  {p['description']}"
        for p in products)


def backend_name():
    """Which index a search would use right now.

    Recorded against every retrieval, because "search got slower" and "search
    silently fell back to Postgres full-text" look identical from the outside
    and have completely different fixes.
    """
    return "pinecone" if config.PINECONE_ENABLED else "postgres"


def health():
    if not config.PINECONE_ENABLED:
        return {"backend": "postgres", "ok": True}
    try:
        target = index()
        stats = target.describe_index_stats()
        return {"backend": "pinecone", "ok": True,
                "vectors": stats.get("total_vector_count", 0)}
    except Exception as exc:                    # noqa: BLE001
        return {"backend": "pinecone", "ok": False, "error": str(exc)}
