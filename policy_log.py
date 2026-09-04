"""Receiving decisions from policy engines running on merchants' servers.

The engine is a package on somebody else's machine. Three consequences shape
this file.

**Nothing it sends is trusted.** `merchant_id` is resolved from the API key and
the value in the body is ignored, because a merchant must not be able to write
into another merchant's log by editing a field. Every other field is bounded
before it reaches the database.

**A failure here must not break their shop.** The engine queues and forgets;
it never waits for us. So this endpoint may be slow, may be down, and may
reject a record — none of which can stop a sale. That is also why a rejected
record returns a plain error rather than anything the engine would retry.

**The version gate is the only lever we have.** We cannot push a fix to code
running on their server. All we can do is refuse to answer a release we have
found a fault in, and say so in a way a person will read.
"""

import logging

import config
import db

log = logging.getLogger("policy_log")

# Releases below this are refused with 426. Raise it only when a genuine fault
# is found: every merchant on an older version stops receiving agent
# suggestions until somebody notices and upgrades, which is a real cost and
# should buy a real fix.
MIN_ENGINE_VERSION = config.MIN_ENGINE_VERSION

# Names a merchant's engine may report. An unrecognised rule is kept rather
# than rejected — a newer engine may know a rule this deployment does not —
# but it is truncated, because the field is free text arriving over a network.
MAX_RULE_NAME = 40
MAX_RULES = 10
MAX_SKU = 64


class VersionTooOld(Exception):
    """The reporting engine is a release we no longer accept."""


class InvalidRecord(ValueError):
    pass


def parse_version(text):
    """Turn "1.2.0" into (1, 2, 0). Anything unparseable sorts lowest.

    A version we cannot read is treated as ancient rather than as current,
    so a malformed or absent header cannot be used to slip past the gate.
    """
    parts = []
    for piece in str(text or "").split("."):
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def check_version(reported):
    if parse_version(reported) < parse_version(MIN_ENGINE_VERSION):
        raise VersionTooOld(
            f"commerce-policy {reported or 'unknown'} is no longer accepted; "
            f"{MIN_ENGINE_VERSION} or newer is required. "
            f"Run: pip install -U commerce-policy")


# --------------------------------------------------------------------------

def record(merchant_id, payload, engine_version=None):
    """Store one reported decision.

    merchant_id comes from the caller's API key. Any merchant_id in the
    payload is discarded — silently would hide a misconfiguration, so a
    mismatch is logged and the key still wins.
    """
    claimed = payload.get("merchant_id")
    if claimed and claimed != merchant_id:
        log.warning(
            "policy log claimed merchant %s but the key belongs to %s; "
            "using the key", claimed, merchant_id)

    version = (engine_version or payload.get("engine_version") or "").strip()
    check_version(version)

    result = payload.get("result")
    if result not in ("approved", "refused"):
        raise InvalidRecord(
            f"result must be 'approved' or 'refused', got {result!r}")

    sku = str(payload.get("sku") or "").strip()[:MAX_SKU]
    if not sku:
        raise InvalidRecord("sku is required")

    asked = _bps(payload.get("asked_bps"), "asked_bps")
    allowed = _bps(payload.get("allowed_bps"), "allowed_bps")

    rules = payload.get("failed_rules") or []
    if not isinstance(rules, list):
        raise InvalidRecord("failed_rules must be a list of rule names")
    rules = [str(r)[:MAX_RULE_NAME] for r in rules[:MAX_RULES]]

    # A refusal with no reason is a bug in the reporting engine, not a
    # decision worth storing as though it were meaningful.
    if result == "refused" and not rules:
        raise InvalidRecord("a refused decision must name the rule that "
                            "blocked it")

    at = payload.get("at")

    db.execute(
        "INSERT INTO policy_decisions (merchant_id, at, sku, asked_bps, "
        "allowed_bps, result, failed_rules, engine_version) "
        "VALUES (%s, COALESCE(%s::timestamptz, now()), %s, %s, %s, %s, %s, %s)",
        (merchant_id, at, sku, asked, allowed, result, rules,
         version or "unknown"))

    return {"stored": True}


def _bps(value, field):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InvalidRecord(f"{field} must be a whole number of basis points")
    if not 0 <= number <= 10000:
        raise InvalidRecord(f"{field} must be between 0 and 10000, "
                            f"got {number}")
    return number


# --------------------------------------------------------------------------

def summary(merchant_id, days=7):
    """What this merchant's engine has been deciding.

    Enough to answer the question a merchant actually has — "why am I not
    selling more?" — without a dashboard existing yet.
    """
    days = max(1, min(int(days), 90))

    totals = db.query_one(
        "SELECT count(*) AS decisions, "
        "count(*) FILTER (WHERE result = 'approved') AS approved, "
        "count(*) FILTER (WHERE result = 'refused')  AS refused, "
        "max(at) AS last_seen "
        "FROM policy_decisions WHERE merchant_id = %s "
        "AND at > now() - (%s || ' days')::interval",
        (merchant_id, days))

    by_rule = db.query(
        "SELECT rule, count(*) AS refusals FROM policy_decisions, "
        "unnest(failed_rules) AS rule "
        "WHERE merchant_id = %s AND at > now() - (%s || ' days')::interval "
        "GROUP BY rule ORDER BY refusals DESC",
        (merchant_id, days))

    versions = db.query(
        "SELECT engine_version, count(*) AS decisions, max(at) AS last_seen "
        "FROM policy_decisions WHERE merchant_id = %s "
        "AND at > now() - (%s || ' days')::interval "
        "GROUP BY engine_version ORDER BY last_seen DESC",
        (merchant_id, days))

    decisions = totals["decisions"] if totals else 0
    refused = totals["refused"] if totals else 0

    return {
        "days": days,
        "decisions": decisions,
        "approved": totals["approved"] if totals else 0,
        "refused": refused,
        # The number a merchant should look at first. A high refusal rate on
        # one rule is usually a cap set tighter than the margin requires, not
        # shoppers asking for the impossible.
        "refusal_rate": round(refused / decisions, 3) if decisions else None,
        "top_refusal": by_rule[0]["rule"] if by_rule else None,
        "by_rule": [dict(r) for r in by_rule],
        "engine_versions": [dict(v) for v in versions],
        "last_seen": totals["last_seen"] if totals else None,
        "min_engine_version": MIN_ENGINE_VERSION,
    }
