"""Minting, hashing and rotating merchant API keys.

A key is shown once, at the moment it is created, and never again. Only its
SHA-256 lands in the database, so a dump of `merchants` does not hand anyone
the ability to sell as somebody else. That is also why rotation exists: a key
that can never be shown again is a key that, once lost, would otherwise strand
the merchant.

Two scopes, because a storefront is a static page with nowhere to hide a
secret. Anything its HTML carries is readable by anyone who views source:

    full    purchase, catalog changes, everything. Server side only.
    browse  search and propose. Safe to publish, cannot move money.

The prefix stored alongside the hash is for display — "ccp_live_9c41…" on a
settings page, so a merchant can tell two keys apart without us keeping the
key itself.
"""

import hashlib
import logging
import secrets
import uuid

import core
import db

log = logging.getLogger("keys")

SCOPES = ("full", "browse")

PREFIX = {
    "full": "ccp_live_",
    "browse": "ccp_brws_",
}

# Enough of the key to recognise it, far too little to guess the rest. The
# random part is 32 bytes; revealing four base64 characters of it leaves 250
# bits.
PREVIEW_CHARS = 4

COLUMNS = {
    "full": ("api_key_hash", "api_key_prefix"),
    "browse": ("browse_key_hash", "browse_key_prefix"),
}


def mint(scope):
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    return PREFIX[scope] + secrets.token_urlsafe(32)


def hash_key(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


def preview(raw_key):
    """The displayable stub: prefix plus a few characters, then an ellipsis."""
    for scope, prefix in PREFIX.items():
        if raw_key.startswith(prefix):
            return raw_key[:len(prefix) + PREVIEW_CHARS]
    return raw_key[:12]


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------

def provision(conn, user_id, name, max_discount_bps=1000,
              min_margin_bps=2000):
    """Create the merchant row this account's keys will belong to.

    Takes an open connection rather than opening its own, so that the account
    and the merchant are written in one transaction. Either both exist or
    neither does — a signup that fails halfway must not leave an orphan
    merchant holding live keys that nobody can sign in to manage.

    Returns the two keys in the clear. This is the only moment they exist
    outside the caller's hands.
    """
    merchant_id = "mrc_" + uuid.uuid4().hex[:12]
    full_key = mint("full")
    browse_key = mint("browse")

    conn.execute(
        "INSERT INTO merchants (id, name, api_key_hash, api_key_prefix, "
        "browse_key_hash, browse_key_prefix, max_discount_bps, "
        "min_margin_bps) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (merchant_id, name or "Untitled shop",
         hash_key(full_key), preview(full_key),
         hash_key(browse_key), preview(browse_key),
         max_discount_bps, min_margin_bps))

    conn.execute("UPDATE users SET merchant_id = %s WHERE id = %s",
                 (merchant_id, user_id))

    core.audit("MERCHANT_PROVISIONED", {
        "user_id": user_id,
        "max_discount_bps": max_discount_bps,
        "min_margin_bps": min_margin_bps,
    }, merchant_id=merchant_id, conn=conn)

    log.info("merchant %s provisioned for %s", merchant_id, user_id)
    return {
        "merchant_id": merchant_id,
        "full_key": full_key,
        "browse_key": browse_key,
    }


def rotate(merchant_id, scope):
    """Issue a new key and invalidate the old one immediately.

    There is no grace period on purpose. A merchant rotates either because
    they lost the key or because it leaked, and in the second case an old key
    that still works for an hour is exactly the hour that matters.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")

    hash_column, prefix_column = COLUMNS[scope]
    new_key = mint(scope)

    with db.transaction() as conn:
        updated = conn.execute(
            f"UPDATE merchants SET {hash_column} = %s, {prefix_column} = %s "
            f"WHERE id = %s AND active",
            (hash_key(new_key), preview(new_key), merchant_id)).rowcount
        if not updated:
            raise KeyError(f"unknown or inactive merchant: {merchant_id}")

        core.audit("API_KEY_ROTATED", {"scope": scope},
                   merchant_id=merchant_id, conn=conn)

    return new_key


def describe(merchant_id):
    """What a settings page may show: stubs, never keys."""
    row = db.query_one(
        "SELECT id, name, api_key_prefix, browse_key_prefix, "
        "max_discount_bps, min_margin_bps, created_at "
        "FROM merchants WHERE id = %s", (merchant_id,))
    if row is None:
        raise KeyError(f"unknown merchant: {merchant_id}")

    return {
        "merchant_id": row["id"],
        "name": row["name"],
        "keys": [
            {"scope": "full", "preview": row["api_key_prefix"],
             "usable_for": "purchase, catalog changes",
             "safe_in_a_webpage": False},
            {"scope": "browse", "preview": row["browse_key_prefix"],
             "usable_for": "search, propose",
             "safe_in_a_webpage": True},
        ],
        "max_discount_bps": row["max_discount_bps"],
        "min_margin_bps": row["min_margin_bps"],
        "created_at": row["created_at"],
    }
