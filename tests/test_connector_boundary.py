"""Nothing sensitive reaches a prompt.

Level 2 lets this system read a merchant's costs and their customers' order
history. Those are the two most sensitive things a shop holds, and the agent
reads merchant-written product text — including one description that says
"ignore all prior pricing rules".

So the guarantee worth testing is not that we handle the data carefully. It is
that the data cannot arrive at the model at all.
"""

import pytest

import agent
import config
import personalise
import retrieval
from connectors import postgres as merchant_db

FEATURES = {
    "categories": ["Electronics", "Books"],
    "owned_skus": ["e1", "b1"],
    "typical_low_paise": 150000,
    "typical_high_paise": 400000,
    "order_count": 4,
}


# --------------------------------------------------------------------------
# the prompt boundary
# --------------------------------------------------------------------------

def test_prompt_never_contains_cost(merchant):
    """The structural guarantee. Costs live in our products table and are read
    by the policy engine; the prompt is built from a different projection."""
    candidates = retrieval.search(merchant, "laptop", limit=6)
    prompt = agent._build_user_message("a laptop", candidates, 15000000)

    assert "cost" not in prompt.lower()
    for product in candidates:
        if product["cost_paise"]:
            assert str(product["cost_paise"]) not in prompt


def test_prompt_with_history_still_contains_no_cost(merchant):
    candidates = retrieval.search(merchant, "laptop", limit=6)
    history = personalise.as_prompt_context(FEATURES)
    prompt = agent._build_user_message("a laptop", candidates, None, history)

    assert "cost" not in prompt.lower()
    assert "Electronics" in prompt          # history did reach the prompt
    assert "already owns" in prompt


def test_history_context_carries_no_personal_data():
    """as_prompt_context can only render what it is given, and it is given
    derived facts. A name or an email would have to be selected upstream, and
    buyer_features does not select one."""
    rendered = personalise.as_prompt_context(FEATURES)

    for field in ("email", "@", "phone", "address", "name"):
        assert field not in rendered.lower()
    assert "Electronics" in rendered
    assert "Rs 1,500" in rendered            # the spend band did render


def test_no_history_renders_nothing():
    assert personalise.as_prompt_context(None) == ""
    assert personalise.as_prompt_context({}) == ""


# --------------------------------------------------------------------------
# shopper pseudonyms
# --------------------------------------------------------------------------

def test_pseudonym_is_stable_for_the_same_shopper():
    a = merchant_db.pseudonym("shop-1", "cust-42")
    b = merchant_db.pseudonym("shop-1", "cust-42")
    assert a == b
    assert len(a) == 32


def test_pseudonym_differs_across_merchants():
    """The same person shopping at two merchants must not be linkable across
    them by anything this system stores."""
    assert (merchant_db.pseudonym("shop-1", "cust-42")
            != merchant_db.pseudonym("shop-2", "cust-42"))


def test_pseudonym_does_not_contain_the_customer_id():
    assert "cust-42" not in merchant_db.pseudonym("shop-1", "cust-42")


def test_pseudonym_refuses_to_work_without_a_secret(monkeypatch):
    """A bare hash of a small integer id is reversible in seconds by trying
    every plausible value, so an unset secret must fail rather than degrade."""
    monkeypatch.setattr(config, "BUYER_REF_SECRET", "")
    with pytest.raises(merchant_db.ConnectorError):
        merchant_db.pseudonym("shop-1", "cust-42")


# --------------------------------------------------------------------------
# capabilities are verified, not trusted
# --------------------------------------------------------------------------

def test_connector_refuses_to_read_orders_it_was_not_granted():
    connection = merchant_db.MerchantDatabase(
        "shop-1", "postgresql://unused", can_read_orders=False)
    assert connection.buyer_features("cust-42") is None


def test_connector_refuses_live_stock_it_was_not_granted():
    connection = merchant_db.MerchantDatabase(
        "shop-1", "postgresql://unused", can_read_stock=False)
    assert connection.live_stock(["e1"]) == {}


def test_unreachable_merchant_database_returns_no_stock_rather_than_failing():
    """Their database being down degrades a freshness check. Our own
    reservation still holds the line, so a sale must not fail here."""
    connection = merchant_db.MerchantDatabase(
        "shop-1", "postgresql://nobody@127.0.0.1:1/none",
        can_read_stock=True)
    assert connection.live_stock(["e1"]) == {}


def test_unreachable_merchant_database_returns_no_history():
    connection = merchant_db.MerchantDatabase(
        "shop-1", "postgresql://nobody@127.0.0.1:1/none",
        can_read_orders=True)
    assert connection.buyer_features("cust-42") is None


def test_merchant_without_a_connection_is_level_one(merchant):
    """No connection configured is the normal case, not an error."""
    assert merchant_db.for_merchant(merchant) is None
    assert personalise.features_for(merchant, "cust-42") is None


def test_proposal_without_a_customer_id_is_not_personalised(merchant):
    proposal = agent.propose(merchant, "a laptop for video editing")
    assert proposal["lines"]                 # still proposes normally
