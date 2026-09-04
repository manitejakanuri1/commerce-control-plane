"""commerce-policy — the pricing guardrail that runs on the merchant's server.

An AI agent proposes. This package decides. It holds the merchant's cost, it
reads no English, and it is the only thing between a model's suggestion and a
payment.

    from commerce_policy import PolicyEngine

    engine = PolicyEngine()

    band = engine.band([{"sku": "SAR-104", "qty": 1}])     # e.g. 1200
    bps  = engine.offer([{"sku": "SAR-104", "qty": 1}], tier=3)
    result = engine.check([{"sku": "SAR-104", "qty": 1}], bps)

    if result["approved"]:
        ...  # create the Razorpay order

Nothing here calls a model, and no cost figure ever leaves the process.
"""

from .engine import PolicyEngine
from .rules import (BUYER_CONSTRAINT, FAIL, MERCHANT_HARD, NOT_CONFIGURED,
                    PASS, TIERS, band, discounted, evaluate, offer_bps)
from .settings import ConfigError, load
from .store import PolicyStore, StoreError
from .version import __version__

__all__ = [
    "PolicyEngine", "PolicyStore", "StoreError", "ConfigError", "load",
    "evaluate", "band", "offer_bps", "discounted", "TIERS",
    "PASS", "FAIL", "NOT_CONFIGURED", "MERCHANT_HARD", "BUYER_CONSTRAINT",
    "__version__",
]
