"""Configuration. Every setting arrives from the environment.

Nothing here has a production default that would let the service start
half-configured against real money. Missing required settings raise at import
time, in the deploy, rather than at the first payment.
"""

import os
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"


def _load_env_file():
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _get(name, default=None, required=False):
    """Read a setting, tolerating dirt the pipeline may have added.

    Deployment tooling does not always hand values over cleanly. Pushing these
    through a Windows shell prefixed every one with a byte-order mark, so
    DB_POOL_MIN arrived as "﻿0" and int() refused it at import time — the
    service would not start, and the message pointed at the wrong thing.

    A leading BOM or stray whitespace is never meaningful in a setting, so it
    is stripped here rather than left to surface as a puzzling error later.
    """
    value = os.environ.get(name, default)
    if isinstance(value, str):
        value = value.lstrip("﻿").strip()
    if required and not value:
        raise RuntimeError(
            f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


ENVIRONMENT = _get("ENVIRONMENT", "development")
IS_PRODUCTION = ENVIRONMENT == "production"

# ---------------------------------------------------------------- database
DATABASE_URL = _get("DATABASE_URL", required=IS_PRODUCTION)
DB_POOL_MIN = int(_get("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(_get("DB_POOL_MAX", "10"))

# ---------------------------------------------------------------- payments
RAZORPAY_KEY_ID = _get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = _get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = _get("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_LIVE = bool(RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET)

# ---------------------------------------------------------------- retrieval
PINECONE_API_KEY = _get("PINECONE_API_KEY", "")
PINECONE_INDEX = _get("PINECONE_INDEX", "merchant-catalog")
PINECONE_CLOUD = _get("PINECONE_CLOUD", "aws")
PINECONE_REGION = _get("PINECONE_REGION", "us-east-1")
PINECONE_EMBED_MODEL = _get("PINECONE_EMBED_MODEL", "llama-text-embed-v2")
PINECONE_ENABLED = bool(PINECONE_API_KEY)

# ---------------------------------------------------------------- model
# DeepSeek proposes what to sell. It only ever returns a proposal, so the model
# is never trusted with a price and the deterministic bundler covers any
# failure. Leave the key blank to run on the bundler alone.
DEEPSEEK_API_KEY = _get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = _get("DEEPSEEK_MODEL", "deepseek-chat")
AGENT_ENABLED = bool(DEEPSEEK_API_KEY)

# ---------------------------------------------------------------- policy
# Defaults only. Real limits live per-merchant in the merchants table; these
# apply when a merchant row has not overridden them.
DEFAULT_MAX_DISCOUNT_BPS = int(_get("DEFAULT_MAX_DISCOUNT_BPS", "1500"))
DEFAULT_MIN_MARGIN_BPS = int(_get("DEFAULT_MIN_MARGIN_BPS", "800"))
RESERVATION_TTL_SECONDS = int(_get("RESERVATION_TTL_SECONDS", "600"))

# ---------------------------------------------------------------- workers
RECONCILE_SWEEP_SECONDS = int(_get("RECONCILE_SWEEP_SECONDS", "120"))
RECONCILE_STALE_AFTER_SECONDS = int(_get("RECONCILE_STALE_AFTER_SECONDS", "180"))
EXPIRY_SWEEP_SECONDS = int(_get("EXPIRY_SWEEP_SECONDS", "60"))

# ---------------------------------------------------------------- ops
LOG_LEVEL = _get("LOG_LEVEL", "INFO")
RATE_LIMIT_PER_MINUTE = int(_get("RATE_LIMIT_PER_MINUTE", "120"))

# Storefront origins allowed to call the API from a shopper's browser.
# Comma separated. Empty means no browser may call it, which is the right
# default: a server-to-server integration never needs CORS.
ALLOWED_ORIGINS = _get("ALLOWED_ORIGINS", "")

# HMAC key used to turn a merchant's customer id into a stable pseudonym, so a
# returning shopper can be recognised without this system storing anything that
# identifies them. Required before any merchant connection may read order
# history; personalisation is refused without it rather than falling back to a
# reversible hash.
BUYER_REF_SECRET = _get("BUYER_REF_SECRET", "")

# Shared secret for the scheduled reconciliation sweep. It operates across all
# merchants, so it is deliberately not authorised by any merchant's own key.
CRON_SECRET = _get("CRON_SECRET", "")

# The oldest commerce-policy release still accepted. That package runs on the
# merchant's own server, so a fault in it cannot be patched from here; refusing
# an old release is the only lever that exists. Raise this only for a real
# fault — every merchant below it stops receiving suggestions until a person
# notices and upgrades.
MIN_ENGINE_VERSION = _get("MIN_ENGINE_VERSION", "1.0.0")

# Off in serverless, where startup runs on every cold start: migrating there
# adds latency to a request and lets instances race over the same DDL.
RUN_MIGRATIONS_ON_STARTUP = _get(
    "RUN_MIGRATIONS_ON_STARTUP", "true").lower() not in ("false", "0", "no")


def summary():
    return {
        "environment": ENVIRONMENT,
        "database": "configured" if DATABASE_URL else "missing",
        "razorpay": "live keys" if RAZORPAY_LIVE else "simulator",
        "retrieval": "pinecone" if PINECONE_ENABLED else "postgres full-text",
        "agent": (f"deepseek ({DEEPSEEK_MODEL})" if AGENT_ENABLED
                  else "deterministic bundler"),
    }
