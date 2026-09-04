"""The engine: the one object a merchant's application talks to.

It is deliberately small. Three methods, and only one of them decides
anything. Everything expensive — retrieval, ranking, writing a reply — lives
on the other side of the network, in a service that never sees a cost.

The order of operations matters and is not negotiable:

    band()   ->  ceiling, derived from cost the agent cannot see
    offer()  ->  a point beneath that ceiling, chosen from a tier label
    check()  ->  the final word, re-derived from the database

check() does not trust the band it handed out a moment ago. It reads the
figures again. A price that moved, a unit that sold, a cap an operator
tightened in between — all of it lands here, which is why this and not the
band is what a payment may be built on.
"""

import datetime
import logging

from . import rules
from .logger import DecisionLogger
from .settings import load
from .store import PolicyStore
from .version import __version__

log = logging.getLogger("commerce_policy")


class PolicyEngine:
    def __init__(self, config_path="policy.config.json", settings=None,
                 store=None, decision_logger=None):
        self.settings = settings if settings is not None else load(config_path)
        self.store = store if store is not None else PolicyStore(self.settings)
        self.logger = (decision_logger if decision_logger is not None
                       else DecisionLogger(self.settings))
        self.merchant_id = self.settings["merchant_id"]

    # ------------------------------------------------------------------

    def band(self, lines, buyer_budget_paise=None):
        """The largest discount, in bps, that every rule would still allow.

        Safe to hand to an agent: it is a ceiling, not a cost. Two products
        with the same band may have wildly different margins, so nothing about
        the merchant's economics can be reconstructed from it.
        """
        lines = _normalise(lines)
        products = self.store.products(ln["sku"] for ln in lines)
        _require_known(lines, products)
        return rules.band(lines, products, self.store.rules(),
                          buyer_budget_paise)

    def offer(self, lines, tier, buyer_budget_paise=None):
        """Band, then tier, in one call. Returns the discount to propose.

        The agent supplies a tier — a judgement about the shopper. The table
        supplies the number. Keeping those apart is what makes the same
        shopper get the same price tomorrow.
        """
        return rules.offer_bps(self.band(lines, buyer_budget_paise), tier)

    def check(self, lines, discount_bps, buyer_budget_paise=None):
        """Approve or refuse. This is the gate; nothing else is.

        Returns the full evaluation, including the rules that were not
        configured, so a merchant can see which protections are actually
        running rather than a row of ticks that includes checks nobody set up.
        """
        lines = _normalise(lines)
        discount_bps = int(discount_bps)

        products = self.store.products(ln["sku"] for ln in lines)
        _require_known(lines, products)
        merchant_rules = self.store.rules()

        result = rules.evaluate(lines, products, merchant_rules,
                                discount_bps, buyer_budget_paise)
        result["engine_version"] = __version__

        allowed = rules.band(lines, products, merchant_rules,
                             buyer_budget_paise)
        result["allowed_bps"] = allowed

        self._record(lines, discount_bps, allowed, result)
        return result

    # ------------------------------------------------------------------

    def _record(self, lines, asked_bps, allowed_bps, result):
        """Write the decision locally, then queue a summary for the control
        plane. Neither is allowed to fail the sale, so both swallow errors."""
        decision = {
            "merchant_id": self.merchant_id,
            # One row per decision, not per line. The first sku is enough to
            # find the order again; the local table holds the rest.
            "sku": lines[0]["sku"],
            "asked_bps": asked_bps,
            "allowed_bps": allowed_bps,
            "result": "approved" if result["approved"] else "refused",
            "failed_rules": result["failed_rules"],
            "engine_version": __version__,
        }
        try:
            self.store.record(decision)
        except Exception as exc:                           # noqa: BLE001
            log.warning("decision not recorded locally (%s: %s)",
                        type(exc).__name__, exc)

        decision["at"] = datetime.datetime.now(
            datetime.timezone.utc).isoformat()
        self.logger.send(decision)


# ----------------------------------------------------------------------

def _normalise(lines):
    """Accept a bare sku, a dict, or a list of either.

    Merchants integrate this by hand under time pressure. Refusing a plain
    string here would be correct and unhelpful.
    """
    if isinstance(lines, str):
        lines = [lines]
    if isinstance(lines, dict):
        lines = [lines]

    out = []
    for entry in lines:
        if isinstance(entry, str):
            out.append({"sku": entry, "qty": 1})
            continue
        sku = entry.get("sku")
        if not sku:
            raise ValueError(f"line is missing a sku: {entry!r}")
        qty = int(entry.get("qty", 1))
        if qty < 1:
            raise ValueError(f"{sku}: qty must be at least 1, got {qty}")
        out.append({"sku": str(sku), "qty": qty})

    if not out:
        raise ValueError("no lines supplied")
    return out


def _require_known(lines, products):
    missing = [ln["sku"] for ln in lines if ln["sku"] not in products]
    if missing:
        raise KeyError(
            f"not in the products table: {', '.join(missing)}. A sku the "
            f"storefront does not have cannot be priced.")
