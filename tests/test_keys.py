"""Merchant provisioning and API keys.

The properties that matter here all fail silently if they break: a key stored
in a form that can be read back, a rotation that leaves the old key working,
and a leaked storefront key that can lock its own merchant out.
"""

import pytest

import auth
import db
import keys


@pytest.fixture(autouse=True)
def clean_accounts(clean_database):
    with db.transaction() as conn:
        conn.execute("TRUNCATE sessions, users CASCADE")
    yield


def make_account(email="owner@example.com", website_name="Bazaar"):
    _, issued = auth.sign_up(email, "correct-horse-battery", "Test Owner",
                             website_name, "https://bazaar.example")
    return issued


# --------------------------------------------------------------------------
# provisioning
# --------------------------------------------------------------------------

def test_signing_up_provisions_a_merchant_and_two_keys():
    issued = make_account()

    assert issued["merchant_id"].startswith("mrc_")
    assert issued["full_key"].startswith("ccp_live_")
    assert issued["browse_key"].startswith("ccp_brws_")
    assert issued["full_key"] != issued["browse_key"]


def test_the_account_is_linked_to_the_merchant():
    issued = make_account()
    row = db.query_one("SELECT merchant_id FROM users WHERE email_key = %s",
                       ("owner@example.com",))
    assert row["merchant_id"] == issued["merchant_id"]


def test_two_accounts_get_different_merchants_and_different_keys():
    first = make_account("a@example.com")
    second = make_account("b@example.com")

    assert first["merchant_id"] != second["merchant_id"]
    assert first["full_key"] != second["full_key"]
    assert first["browse_key"] != second["browse_key"]


def test_the_shop_name_falls_back_to_the_person_s_name():
    auth.sign_up("noshop@example.com", "correct-horse-battery", "Purna",
                 website_name=None)
    row = db.query_one(
        "SELECT m.name FROM merchants m JOIN users u ON u.merchant_id = m.id "
        "WHERE u.email_key = %s", ("noshop@example.com",))
    assert row["name"] == "Purna's shop"


def test_a_failed_signup_leaves_no_merchant_behind():
    """Account and merchant are written in one transaction. A shop with live
    keys and no owner would be unmanageable and invisible."""
    before = db.query_one("SELECT count(*) AS n FROM merchants")["n"]

    with pytest.raises(auth.AuthError):
        auth.sign_up("not-an-email", "correct-horse-battery", "Someone")

    assert db.query_one("SELECT count(*) AS n FROM merchants")["n"] == before


def test_a_duplicate_signup_does_not_provision_a_second_merchant():
    make_account("owner@example.com")
    before = db.query_one("SELECT count(*) AS n FROM merchants")["n"]

    with pytest.raises(auth.AuthError, match="already exists"):
        auth.sign_up("owner@example.com", "correct-horse-battery", "Someone")

    assert db.query_one("SELECT count(*) AS n FROM merchants")["n"] == before


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

def test_only_hashes_are_stored():
    """A database dump must not hand anyone the ability to sell as someone
    else."""
    issued = make_account()
    row = db.query_one("SELECT * FROM merchants WHERE id = %s",
                       (issued["merchant_id"],))
    stored = str(dict(row))

    assert issued["full_key"] not in stored
    assert issued["browse_key"] not in stored


def test_the_stored_preview_reveals_almost_nothing():
    issued = make_account()
    row = db.query_one(
        "SELECT api_key_prefix FROM merchants WHERE id = %s",
        (issued["merchant_id"],))

    assert issued["full_key"].startswith(row["api_key_prefix"])
    assert len(row["api_key_prefix"]) == len("ccp_live_") + 4


def test_describe_returns_stubs_and_never_a_key():
    issued = make_account()
    described = keys.describe(issued["merchant_id"])

    rendered = str(described)
    assert issued["full_key"] not in rendered
    assert issued["browse_key"] not in rendered
    assert {k["scope"] for k in described["keys"]} == {"full", "browse"}


def test_describe_says_which_key_is_safe_in_a_webpage():
    """The single most consequential thing a merchant can get wrong."""
    described = keys.describe(make_account()["merchant_id"])
    by_scope = {k["scope"]: k for k in described["keys"]}

    assert by_scope["browse"]["safe_in_a_webpage"] is True
    assert by_scope["full"]["safe_in_a_webpage"] is False


# --------------------------------------------------------------------------
# rotation
# --------------------------------------------------------------------------

def test_rotation_issues_a_different_key():
    issued = make_account()
    rotated = keys.rotate(issued["merchant_id"], "full")

    assert rotated != issued["full_key"]
    assert rotated.startswith("ccp_live_")


def test_rotation_invalidates_the_old_key_immediately():
    """No grace period. A merchant rotates because a key leaked, and an old
    key that works for another hour is exactly the hour that matters."""
    issued = make_account()
    old_hash = keys.hash_key(issued["full_key"])

    keys.rotate(issued["merchant_id"], "full")

    assert db.query_one(
        "SELECT 1 FROM merchants WHERE api_key_hash = %s", (old_hash,)) is None


def test_rotating_one_scope_leaves_the_other_working():
    issued = make_account()
    browse_hash = keys.hash_key(issued["browse_key"])

    keys.rotate(issued["merchant_id"], "full")

    assert db.query_one(
        "SELECT 1 FROM merchants WHERE browse_key_hash = %s",
        (browse_hash,)) is not None


def test_rotation_updates_the_preview_too():
    issued = make_account()
    rotated = keys.rotate(issued["merchant_id"], "browse")
    row = db.query_one("SELECT browse_key_prefix FROM merchants WHERE id = %s",
                       (issued["merchant_id"],))

    assert rotated.startswith(row["browse_key_prefix"])


def test_an_unknown_scope_is_refused():
    issued = make_account()
    with pytest.raises(ValueError, match="unknown scope"):
        keys.rotate(issued["merchant_id"], "admin")


def test_rotating_an_unknown_merchant_raises():
    with pytest.raises(KeyError):
        keys.rotate("mrc_nonexistent", "full")


# --------------------------------------------------------------------------
# the audit trail
# --------------------------------------------------------------------------

def test_provisioning_is_audited_against_the_new_merchant():
    issued = make_account()
    row = db.query_one(
        "SELECT merchant_id FROM audit WHERE action = 'MERCHANT_PROVISIONED' "
        "ORDER BY seq DESC LIMIT 1")
    assert row["merchant_id"] == issued["merchant_id"]


def test_rotation_is_audited_without_recording_the_key():
    issued = make_account()
    rotated = keys.rotate(issued["merchant_id"], "full")

    row = db.query_one(
        "SELECT detail FROM audit WHERE action = 'API_KEY_ROTATED' "
        "ORDER BY seq DESC LIMIT 1")
    assert row["detail"]["scope"] == "full"
    assert rotated not in str(row["detail"])


# --------------------------------------------------------------------------
# minting
# --------------------------------------------------------------------------

def test_keys_are_long_enough_to_be_unguessable():
    for scope in keys.SCOPES:
        minted = keys.mint(scope)
        random_part = minted[len(keys.PREFIX[scope]):]
        assert len(random_part) >= 40      # 32 bytes, base64url


def test_minting_never_repeats():
    minted = {keys.mint("full") for _ in range(200)}
    assert len(minted) == 200


def test_scopes_are_distinguishable_by_eye():
    """A merchant pasting the wrong key into a public page is the failure
    this prefix exists to prevent."""
    assert keys.mint("full").startswith("ccp_live_")
    assert keys.mint("browse").startswith("ccp_brws_")
