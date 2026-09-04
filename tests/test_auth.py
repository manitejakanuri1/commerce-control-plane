"""Accounts and sessions.

The properties worth proving here are the ones that fail quietly: a password
that is stored recoverably, a sign-out that does not sign anyone out, and an
error message that reveals who has an account.
"""

import pytest

import auth
import db


@pytest.fixture(autouse=True)
def clean_accounts(clean_database):
    with db.transaction() as conn:
        conn.execute("TRUNCATE sessions, users CASCADE")
    yield


def make_user(email="owner@example.com", password="correct-horse-battery"):
    auth.sign_up(email, password, "Test Owner", "Bazaar",
                 "https://bazaar.example")
    return email, password


# --------------------------------------------------------------------------
# passwords
# --------------------------------------------------------------------------

def test_password_is_not_stored_anywhere_readable():
    email, password = make_user()
    row = db.query_one("SELECT * FROM users WHERE email_key = %s",
                       (email.lower(),))

    assert password not in row["password_hash"]
    assert password not in row["password_salt"]
    assert password not in str(dict(row))


def test_each_account_gets_its_own_salt():
    """Shared salts let one cracked password reveal every account that reused
    it, and make a precomputed table worth building."""
    make_user("a@example.com", "correct-horse-battery")
    make_user("b@example.com", "correct-horse-battery")

    rows = db.query("SELECT password_hash, password_salt FROM users "
                    "ORDER BY email_key")
    assert rows[0]["password_salt"] != rows[1]["password_salt"]
    assert rows[0]["password_hash"] != rows[1]["password_hash"]


def test_short_password_is_refused():
    with pytest.raises(auth.AuthError, match="at least"):
        auth.sign_up("short@example.com", "abc123", "Someone")


def test_malformed_email_is_refused():
    with pytest.raises(auth.AuthError, match="email"):
        auth.sign_up("not-an-email", "correct-horse-battery", "Someone")


def test_duplicate_email_is_refused_regardless_of_case():
    make_user("Owner@Example.com")
    with pytest.raises(auth.AuthError, match="already exists"):
        auth.sign_up("owner@EXAMPLE.com", "correct-horse-battery", "Someone")


# --------------------------------------------------------------------------
# signing in
# --------------------------------------------------------------------------

def test_sign_in_returns_a_session_and_a_user():
    email, password = make_user()
    token, user = auth.sign_in(email, password)

    assert len(token) > 30
    assert user["email"] == email
    assert "password_hash" not in user
    assert "password_salt" not in user


def test_sign_in_is_case_insensitive_on_email():
    make_user("Owner@Example.com")
    token, _ = auth.sign_in("OWNER@example.COM", "correct-horse-battery")
    assert token


def test_wrong_password_is_refused():
    email, _ = make_user()
    with pytest.raises(auth.AuthError):
        auth.sign_in(email, "not-the-password")


def test_unknown_email_and_wrong_password_give_the_same_message():
    """Different messages would let anyone check which addresses have
    accounts here."""
    email, _ = make_user()

    with pytest.raises(auth.AuthError) as unknown:
        auth.sign_in("nobody@example.com", "correct-horse-battery")
    with pytest.raises(auth.AuthError) as wrong:
        auth.sign_in(email, "not-the-password")

    assert str(unknown.value) == str(wrong.value)


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

def test_token_resolves_to_the_user():
    email, password = make_user()
    token, _ = auth.sign_in(email, password)

    user = auth.user_for_token(token)
    assert user is not None
    assert user["email"] == email


def test_only_the_token_hash_is_stored():
    """A leaked database must not hand over live sessions."""
    email, password = make_user()
    token, _ = auth.sign_in(email, password)

    rows = db.query("SELECT token_hash FROM sessions")
    assert len(rows) == 1
    assert rows[0]["token_hash"] != token
    assert token not in rows[0]["token_hash"]


def test_sign_out_actually_ends_the_session():
    """The reason sessions are rows rather than signed tokens: a signed token
    stays valid until it expires, whatever the server later decides."""
    email, password = make_user()
    token, _ = auth.sign_in(email, password)
    assert auth.user_for_token(token) is not None

    assert auth.sign_out(token) is True
    assert auth.user_for_token(token) is None


def test_expired_session_is_refused():
    email, password = make_user()
    token, _ = auth.sign_in(email, password)

    db.execute("UPDATE sessions SET expires_at = now() - interval '1 hour'")
    assert auth.user_for_token(token) is None


def test_garbage_token_is_refused():
    assert auth.user_for_token("not-a-real-token") is None
    assert auth.user_for_token("") is None
    assert auth.user_for_token(None) is None


def test_deactivated_account_cannot_use_an_existing_session():
    email, password = make_user()
    token, _ = auth.sign_in(email, password)

    db.execute("UPDATE users SET active = FALSE WHERE email_key = %s",
               (email.lower(),))
    assert auth.user_for_token(token) is None


def test_signing_out_one_session_leaves_others_alone():
    email, password = make_user()
    first, _ = auth.sign_in(email, password)
    second, _ = auth.sign_in(email, password)

    auth.sign_out(first)

    assert auth.user_for_token(first) is None
    assert auth.user_for_token(second) is not None
