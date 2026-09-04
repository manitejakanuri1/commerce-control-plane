"""Accounts and sessions for the command centre.

Standard library only. scrypt is in hashlib and is a proper key-derivation
function, so there is no reason to add a dependency for this.

Two decisions worth stating, because both are easy to get wrong quietly:

**Passwords are never compared directly.** They are stretched with scrypt under
a per-user salt and compared in constant time, so timing does not leak how much
of a guess was right.

**Sessions are rows, not signed tokens.** A self-contained token stays valid
until it expires no matter what the server later decides, which means signing
out does not really sign you out. A row can be deleted. Only the token's hash
is stored, so a leaked database does not hand over live sessions either.
"""

import hashlib
import hmac
import logging
import re
import secrets
import uuid

import db
import keys

log = logging.getLogger("auth")

SESSION_DAYS = 14
MIN_PASSWORD = 10

# Deliberately loose. Anything stricter rejects addresses that are perfectly
# valid; delivery is the real test of an email, not a pattern.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ~100ms per attempt on a modern CPU: slow enough to make offline guessing
# expensive, fast enough that signing in feels instant.
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}


class AuthError(Exception):
    """Raised for anything a caller is allowed to be told about."""


def _hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, **SCRYPT)
    return key.hex(), salt.hex()


def _verify_password(password, stored_hash, stored_salt):
    candidate, _ = _hash_password(password, bytes.fromhex(stored_salt))
    return hmac.compare_digest(candidate, stored_hash)


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

def sign_up(email, password, name, website_name=None, website_url=None):
    """Create the account and the merchant it manages, in one transaction.

    Both, or neither. An earlier version created only the account and left
    `merchant_id` null until a separate setup step, on the reasoning that a
    half-finished signup should not strand a merchant row. In practice it
    stranded the opposite thing: an account with no merchant has no API keys,
    and every downstream feature — the integration prompt, the widget, the
    logs — has nothing to address. One transaction removes the half-finished
    state that concern was about, so there is no longer a reason to defer.

    Returns (user_id, keys). The keys are in the clear here and nowhere else
    ever again.
    """
    email = (email or "").strip()
    name = (name or "").strip()

    if not EMAIL_RE.match(email):
        raise AuthError("that does not look like an email address")
    if len(password or "") < MIN_PASSWORD:
        raise AuthError(
            f"password must be at least {MIN_PASSWORD} characters")
    if not name:
        raise AuthError("name is required")

    email_key = email.lower()
    if db.query_one("SELECT 1 FROM users WHERE email_key = %s", (email_key,)):
        raise AuthError("an account with that email already exists")

    password_hash, password_salt = _hash_password(password)
    user_id = "usr_" + uuid.uuid4().hex[:16]

    shop_name = (website_name or "").strip() or f"{name}'s shop"

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO users (id, email, email_key, name, website_name, "
            "website_url, password_hash, password_salt) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (user_id, email, email_key, name,
             (website_name or "").strip() or None,
             (website_url or "").strip() or None,
             password_hash, password_salt))

        issued = keys.provision(conn, user_id, shop_name)

    log.info("account created %s with merchant %s",
             user_id, issued["merchant_id"])
    return user_id, issued


def sign_in(email, password, user_agent=None, ip=None):
    """Authenticate and open a session.

    The same message is returned whether the address is unknown or the password
    is wrong, so this cannot be used to discover who has an account. The hash is
    still computed for a missing user, so the two paths take the same time.
    """
    email_key = (email or "").strip().lower()
    row = db.query_one(
        "SELECT * FROM users WHERE email_key = %s AND active", (email_key,))

    if row is None:
        # Burn the same work as a real verification would.
        _hash_password(password or "")
        raise AuthError("email or password is incorrect")

    if not _verify_password(password or "", row["password_hash"],
                            row["password_salt"]):
        raise AuthError("email or password is incorrect")

    token = secrets.token_urlsafe(32)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at, "
            "user_agent, ip) VALUES (%s, %s, now() + (%s || ' days')::interval,"
            " %s, %s)",
            (_token_hash(token), row["id"], SESSION_DAYS,
             (user_agent or "")[:300], (ip or "")[:64]))
        conn.execute("UPDATE users SET last_login_at = now() WHERE id = %s",
                     (row["id"],))

    return token, _public(row)


def sign_out(token):
    if not token:
        return False
    return db.execute("DELETE FROM sessions WHERE token_hash = %s",
                      (_token_hash(token),)) > 0


def user_for_token(token):
    """Resolve a session token, or None. Expired rows are swept as they are
    encountered, so nothing accumulates unbounded."""
    if not token:
        return None

    row = db.query_one(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = %s AND s.expires_at > now() AND u.active",
        (_token_hash(token),))
    if row is None:
        db.execute("DELETE FROM sessions WHERE expires_at < now()")
        return None
    return _public(row)


def _public(row):
    """What may leave this module. Never the hash, never the salt."""
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "website_name": row["website_name"],
        "website_url": row["website_url"],
        "merchant_id": row["merchant_id"],
    }
