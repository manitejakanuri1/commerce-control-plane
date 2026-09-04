"""Operational telemetry: searches, proposals, and what they returned.

Three rules hold this file together.

**Recording never fails a request.** Every write is wrapped. A shopper's search
must not break because a telemetry insert deadlocked, and there is nothing here
worth an error page.

**Shopper text is redacted before it is stored.** A search box is a text field,
and people type things into text fields — an email address, a phone number,
once in a while a card. The whole system's claim is that it holds nothing
identifying, and storing raw search strings would quietly make that false.

**Nothing here is evidence.** No hash chain, no append-only trigger, deleted
after a month. `audit` is for money; this is for noticing patterns.
"""

import json
import logging
import re

import db

log = logging.getLogger("events")

KINDS = ("search", "propose", "purchase_started", "widget_shown")

MAX_QUERY = 200
RETENTION_DAYS = 30

# Redaction, in the order applied. Deliberately blunt: a false positive costs
# one useless word in a demand report, while a false negative puts a real
# person's phone number in a table we promised holds none.
REDACTIONS = (
    (re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+"), "[email]"),
    # 13-19 digits: card-shaped, checked before phone so it wins. The lower
    # bound is 13 rather than 12 because an Indian mobile with its country
    # code is exactly 12 digits, and "+91 98765 43210" reported back to a
    # merchant as "[card]" is a redaction that lies about what it caught.
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[card]"),
    # 8-14 characters of digits and separators: a phone with or without +91.
    (re.compile(r"\+?\d[\d\s-]{6,12}\d"), "[phone]"),
)


def redact(text):
    """Strip anything that looks like a way to contact or bill a person."""
    if not text:
        return None
    cleaned = str(text).strip()[:MAX_QUERY]
    for pattern, replacement in REDACTIONS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned or None


def record(merchant_id, kind, query=None, results=None, duration_ms=None,
           **detail):
    """Write one event. Returns True if it landed, False if it did not.

    A caller that checks the return value is doing something unusual; the
    normal use is to ignore it, which is why nothing here raises.
    """
    if kind not in KINDS:
        # Not an exception: a new kind added by a caller is a small mistake,
        # and refusing the whole request over telemetry would be a larger one.
        log.warning("unknown event kind %r, recording anyway", kind)

    try:
        db.execute(
            "INSERT INTO events (merchant_id, kind, query, results, "
            "duration_ms, detail) VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
            (merchant_id, kind, redact(query), results, duration_ms,
             json.dumps(detail, default=str)))
        return True
    except Exception as exc:                              # noqa: BLE001
        log.debug("event not recorded (%s: %s)", type(exc).__name__, exc)
        return False


# --------------------------------------------------------------------------
# what the merchant should see
# --------------------------------------------------------------------------

def unmet_demand(merchant_id, days=30, limit=20, min_asks=2):
    """Searches that returned nothing, grouped by what was asked for.

    The single most actionable thing this system knows. A merchant cannot
    learn it anywhere else: their own search returned "no results" and forgot,
    and the payment processor never saw these shoppers at all because they
    never reached checkout.

    min_asks defaults to 2 so one person's typo does not read as demand.
    """
    days = max(1, min(int(days), 365))
    limit = max(1, min(int(limit), 100))

    rows = db.query(
        "SELECT lower(query) AS asked, count(*) AS times, max(at) AS last_at "
        "FROM events WHERE merchant_id = %s AND kind = 'search' "
        "AND results = 0 AND query IS NOT NULL "
        "AND at > now() - (%s || ' days')::interval "
        "GROUP BY lower(query) HAVING count(*) >= %s "
        "ORDER BY times DESC, last_at DESC LIMIT %s",
        (merchant_id, days, min_asks, limit))

    return [dict(r) for r in rows]


def summary(merchant_id, days=7):
    days = max(1, min(int(days), 365))

    totals = db.query_one(
        "SELECT count(*) FILTER (WHERE kind = 'search') AS searches, "
        "count(*) FILTER (WHERE kind = 'search' AND results = 0) AS empty, "
        "count(*) FILTER (WHERE kind = 'propose') AS proposals, "
        "count(*) FILTER (WHERE kind = 'purchase_started') AS purchases, "
        "percentile_disc(0.5) WITHIN GROUP (ORDER BY duration_ms) "
        "  FILTER (WHERE kind = 'search') AS median_search_ms, "
        "max(at) AS last_seen "
        "FROM events WHERE merchant_id = %s "
        "AND at > now() - (%s || ' days')::interval",
        (merchant_id, days))

    searches = (totals["searches"] or 0) if totals else 0
    empty = (totals["empty"] or 0) if totals else 0
    proposals = (totals["proposals"] or 0) if totals else 0
    purchases = (totals["purchases"] or 0) if totals else 0

    return {
        "days": days,
        "searches": searches,
        "searches_with_no_results": empty,
        # The number to act on. High means the catalog is missing what people
        # come here wanting, not that the search is broken.
        "empty_rate": round(empty / searches, 3) if searches else None,
        "proposals": proposals,
        "purchases_started": purchases,
        "conversion": round(purchases / proposals, 3) if proposals else None,
        "median_search_ms": totals["median_search_ms"] if totals else None,
        "last_seen": totals["last_seen"] if totals else None,
    }


# --------------------------------------------------------------------------

def prune(days=RETENTION_DAYS):
    """Delete events past their useful life.

    Called from the ops sweep, which already runs on a schedule. Without this
    the table grows without limit, and the rows nobody will ever read are the
    ones paying for the storage.
    """
    days = max(1, int(days))
    deleted = db.execute(
        "DELETE FROM events WHERE at < now() - (%s || ' days')::interval",
        (days,))
    if deleted:
        log.info("pruned %s events older than %s days", deleted, days)
    return deleted
