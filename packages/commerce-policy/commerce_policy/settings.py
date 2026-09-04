"""Configuration: a file for the harmless parts, environment for the secrets.

The split is not stylistic. `policy.config.json` is meant to be committed —
it holds a discount cap and some column names, and a reviewer should be able
to see those in a pull request. The database URL and the API key are read
from the environment, because the moment a credential is committable somebody
commits it.
"""

import json
import os
from pathlib import Path

DEFAULTS = {
    # identity
    "merchant_id": None,
    "control_plane_url": "https://commerce-control-plane-api.vercel.app",

    # the rules, if not held in policy.rules
    "max_discount_bps": 1000,   # 10%
    "min_margin_bps": 2000,     # 20%

    # where their storefront keeps its products
    "products_table": "products",
    "sku_column": "sku",
    "price_column": "price",
    "stock_column": "stock",
    "price_is_minor_units": False,

    # logging
    "send_logs": True,
    "log_queue_size": 1000,
}

SECRET_ENV = {
    "database_url": "POLICY_DB_URL",
    "api_key": "COMMERCE_POLICY_API_KEY",
}


class ConfigError(RuntimeError):
    pass


def load(path="policy.config.json", environ=None):
    """Read the config file, then overlay the secrets from the environment."""
    environ = os.environ if environ is None else environ

    file_path = Path(path)
    if file_path.exists():
        try:
            supplied = json.loads(file_path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{file_path} is not valid JSON: {exc}") from exc
    else:
        supplied = {}

    unknown = set(supplied) - set(DEFAULTS)
    if unknown:
        # A typo in a key name would otherwise be silently ignored and the
        # merchant would run on a default they never chose.
        raise ConfigError(
            f"{file_path}: unrecognised setting(s) {', '.join(sorted(unknown))}")

    settings = {**DEFAULTS, **supplied}

    for name, env_var in SECRET_ENV.items():
        value = environ.get(env_var, "")
        # A shell pipeline can prefix a byte-order mark onto a value; int()
        # and psycopg both refuse it, with an error that names neither cause.
        settings[name] = value.lstrip("﻿").strip()

    if not settings["merchant_id"]:
        raise ConfigError(
            f'{file_path}: "merchant_id" is required. It is on your dashboard.')
    if not settings["database_url"]:
        raise ConfigError(
            "POLICY_DB_URL is not set. Point it at the database holding the "
            "policy schema, using a role that can read policy.economics.")

    for key in ("max_discount_bps", "min_margin_bps"):
        value = settings[key]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{key} must be a whole number of basis "
                              f"points (1000 = 10%), got {value!r}")
        if not 0 <= value < 10000:
            raise ConfigError(f"{key} must be between 0 and 9999 bps, "
                              f"got {value}")

    return settings
