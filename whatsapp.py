"""Sending through the WhatsApp Cloud API.

An earlier version drove WhatsApp Web through a headless browser on a box the
merchant had to keep running. It required Chrome, a QR scan, and an always-on
server that a saree shop does not have — and it impersonated WhatsApp Web,
which meant the number carrying a shop's receipts could be banned for using
it. It also would not start against a current Chrome, which is how the whole
approach came to be reconsidered.

This is WhatsApp's own API. A token and a phone number id are the entire
integration: one HTTPS call per message, no browser, no session, nothing for
the merchant to keep alive, and no reason for Meta to object.

That also removes the worker. A serverless function could not hold a browser
session, so delivery had to live elsewhere; an HTTPS call has no such problem
and runs in the same request that confirms the payment.
"""

import json
import logging
import urllib.error
import urllib.request

import config
import db

log = logging.getLogger("whatsapp")

GRAPH = "https://graph.facebook.com/v21.0"
TIMEOUT_SECONDS = 10


class WhatsAppError(RuntimeError):
    pass


def _key():
    key = (config.BUYER_REF_SECRET or "").strip()
    if not key:
        raise WhatsAppError(
            "BUYER_REF_SECRET is not set; refusing to store a token without "
            "a key to encrypt it with.")
    return key


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------

def connect(merchant_id, access_token, phone_number_id, display_number=None):
    """Store a merchant's Cloud API credentials, after proving they work.

    Verified before it is saved. A token accepted here and found to be wrong
    later fails at the worst moment — silently, on somebody's receipt — and
    the merchant would have had every reason to think they were connected.
    """
    access_token = (access_token or "").strip()
    phone_number_id = (phone_number_id or "").strip()
    if not access_token or not phone_number_id:
        raise WhatsAppError("both a token and a phone number id are required")

    number = verify(access_token, phone_number_id)

    db.execute(
        "INSERT INTO whatsapp_sessions (merchant_id, status, provider, "
        "phone_number_id, access_token_encrypted, connected_number, "
        "updated_at) VALUES (%s, 'connected', 'cloud_api', %s, "
        "pgp_sym_encrypt(%s, %s), %s, now()) "
        "ON CONFLICT (merchant_id) DO UPDATE SET status = 'connected', "
        "provider = 'cloud_api', phone_number_id = EXCLUDED.phone_number_id, "
        "access_token_encrypted = EXCLUDED.access_token_encrypted, "
        "connected_number = EXCLUDED.connected_number, updated_at = now()",
        (merchant_id, phone_number_id, access_token, _key(),
         display_number or number))

    return {"connected": True, "number": display_number or number}


def verify(access_token, phone_number_id):
    """Ask Meta who this token speaks for. Returns the display number."""
    request = urllib.request.Request(
        f"{GRAPH}/{phone_number_id}?fields=display_phone_number,verified_name",
        headers={"Authorization": f"Bearer {access_token}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = _meta_error(exc)
        raise WhatsAppError(
            f"Meta rejected these credentials: {detail}") from exc
    except Exception as exc:                              # noqa: BLE001
        raise WhatsAppError(
            f"could not reach Meta ({type(exc).__name__})") from exc

    return body.get("display_phone_number") or phone_number_id


def credentials(merchant_id):
    """Decrypt a merchant's token, or None if they have not connected."""
    row = db.query_one(
        "SELECT phone_number_id, "
        "pgp_sym_decrypt(access_token_encrypted, %s) AS token "
        "FROM whatsapp_sessions WHERE merchant_id = %s "
        "AND status = 'connected' AND access_token_encrypted IS NOT NULL",
        (_key(), merchant_id))
    if row is None:
        return None
    return row["token"], row["phone_number_id"]


def disconnect(merchant_id):
    return db.execute(
        "UPDATE whatsapp_sessions SET status = 'stopped', "
        "access_token_encrypted = NULL, updated_at = now() "
        "WHERE merchant_id = %s", (merchant_id,))


# --------------------------------------------------------------------------
# sending
# --------------------------------------------------------------------------

def send(merchant_id, to, body):
    """Deliver one message. Raises WhatsAppError with Meta's own reason.

    The reason is passed through rather than flattened into "send failed",
    because Meta's messages name the actual problem — an unverified recipient,
    a session window that has closed, a template that needs approval — and
    each one has a different fix.
    """
    creds = credentials(merchant_id)
    if creds is None:
        raise WhatsAppError("this merchant has not connected WhatsApp")
    token, phone_number_id = creds

    payload = json.dumps({
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _normalise(to),
        "type": "text",
        "text": {"preview_url": True, "body": body[:4096]},
    }).encode()

    request = urllib.request.Request(
        f"{GRAPH}/{phone_number_id}/messages", data=payload, method="POST",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as r:
            result = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise WhatsAppError(_meta_error(exc)) from exc
    except Exception as exc:                              # noqa: BLE001
        raise WhatsAppError(f"{type(exc).__name__}: {exc}") from exc

    messages = result.get("messages") or []
    return messages[0].get("id") if messages else None


def _normalise(contact):
    """Digits only, with a country code.

    Meta wants the number without a plus. A number too short to be real is
    refused rather than guessed at: a guessed number reaches a stranger, and a
    receipt naming somebody's purchase is not a message to send to a stranger.
    """
    digits = "".join(c for c in str(contact or "") if c.isdigit())
    if len(digits) == 10:
        digits = config.DEFAULT_COUNTRY_CODE + digits
    if not 11 <= len(digits) <= 15:
        raise WhatsAppError(f"{contact!r} is not a usable phone number")
    return digits


def _meta_error(exc):
    """Meta's own words, when it gave any."""
    try:
        body = json.loads(exc.read())
        error = body.get("error") or {}
        parts = [error.get("message"), error.get("error_user_title"),
                 error.get("error_user_msg")]
        detail = " — ".join(p for p in parts if p)
        return detail or f"HTTP {exc.code}"
    except Exception:                                     # noqa: BLE001
        return f"HTTP {exc.code}"
