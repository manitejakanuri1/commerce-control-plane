"""Operational telemetry.

Two things are worth proving here. That a shopper's typed text cannot carry
their phone number into our database, because the whole system's claim is that
it stores nothing identifying. And that recording telemetry can never break a
search, because a search that fails over a logging insert is a worse outcome
than no telemetry at all.
"""

import pytest

import db
import events


@pytest.fixture(autouse=True)
def clean_events(clean_database):
    with db.transaction() as conn:
        conn.execute("TRUNCATE events")
    yield


# --------------------------------------------------------------------------
# redaction
# --------------------------------------------------------------------------

def test_an_email_typed_into_the_search_box_is_not_stored(merchant):
    events.record(merchant, "search",
                  query="send it to priya@example.com please", results=0)
    row = db.query_one("SELECT query FROM events")

    assert "priya@example.com" not in row["query"]
    assert "[email]" in row["query"]


def test_a_phone_number_is_not_stored(merchant):
    events.record(merchant, "search", query="call me 9876543210", results=0)
    row = db.query_one("SELECT query FROM events")

    assert "9876543210" not in row["query"]
    assert "[phone]" in row["query"]


def test_an_indian_number_with_a_country_code_is_caught(merchant):
    events.record(merchant, "search", query="+91 98765 43210", results=0)
    stored = db.query_one("SELECT query FROM events")["query"]

    assert "98765" not in stored
    # Labelled correctly, not as a card. An Indian mobile with its country
    # code is 12 digits, so the card pattern has to start at 13 or it claims
    # to have caught something it did not.
    assert "[phone]" in stored


def test_a_card_number_is_not_stored(merchant):
    events.record(merchant, "search",
                  query="4111 1111 1111 1111", results=0)
    row = db.query_one("SELECT query FROM events")

    assert "4111" not in row["query"]
    assert "[card]" in row["query"]


def test_ordinary_searches_survive_redaction_intact():
    """Blunt redaction is only acceptable if it leaves real queries alone."""
    assert events.redact("waterproof shoes under 3000") == \
        "waterproof shoes under 3000"
    assert events.redact("cotton saree size 42") == "cotton saree size 42"
    assert events.redact("laptop 16gb 512gb") == "laptop 16gb 512gb"


def test_a_price_is_not_mistaken_for_a_phone_number():
    assert events.redact("under 50000") == "under 50000"


def test_a_very_long_query_is_truncated(merchant):
    events.record(merchant, "search", query="x" * 5000, results=0)
    assert len(db.query_one("SELECT query FROM events")["query"]) \
        == events.MAX_QUERY


# --------------------------------------------------------------------------
# recording cannot break anything
# --------------------------------------------------------------------------

def test_recording_against_an_unknown_merchant_returns_false_not_raises():
    """A foreign key violation here must not become a 500 on a shopper's
    search."""
    assert events.record("no-such-merchant", "search", query="shoes") is False


def test_an_unfamiliar_kind_is_still_recorded(merchant):
    """A caller adding a new event kind is a small mistake. Refusing the
    request over it would be a larger one."""
    assert events.record(merchant, "something_new", query="x") is True


def test_a_search_with_no_query_records_nothing_identifying(merchant):
    events.record(merchant, "search", results=5)
    assert db.query_one("SELECT query FROM events")["query"] is None


# --------------------------------------------------------------------------
# unmet demand — the reason this table exists
# --------------------------------------------------------------------------

def test_repeated_empty_searches_surface_as_demand(merchant):
    for _ in range(4):
        events.record(merchant, "search",
                      query="waterproof shoes under 2000", results=0)

    demand = events.unmet_demand(merchant)
    assert demand[0]["asked"] == "waterproof shoes under 2000"
    assert demand[0]["times"] == 4


def test_searches_that_found_something_are_not_demand(merchant):
    for _ in range(5):
        events.record(merchant, "search", query="cotton saree", results=7)

    assert events.unmet_demand(merchant) == []


def test_a_single_search_is_not_demand(merchant):
    """One person's typo is not a signal to change the catalog."""
    events.record(merchant, "search", query="wtaerproof shoos", results=0)
    assert events.unmet_demand(merchant) == []


def test_demand_is_grouped_case_insensitively(merchant):
    events.record(merchant, "search", query="Waterproof Shoes", results=0)
    events.record(merchant, "search", query="waterproof shoes", results=0)

    demand = events.unmet_demand(merchant)
    assert len(demand) == 1
    assert demand[0]["times"] == 2


def test_demand_is_ordered_by_how_many_people_asked(merchant):
    for _ in range(6):
        events.record(merchant, "search", query="rain jacket", results=0)
    for _ in range(2):
        events.record(merchant, "search", query="umbrella", results=0)

    demand = events.unmet_demand(merchant)
    assert [d["asked"] for d in demand] == ["rain jacket", "umbrella"]


def test_demand_is_scoped_to_one_merchant(merchant):
    with db.transaction() as conn:
        conn.execute("INSERT INTO merchants (id, name, api_key_hash) "
                     "VALUES ('other', 'Other', 'otherhash')")
    for _ in range(3):
        events.record(merchant, "search", query="rain jacket", results=0)
        events.record("other", "search", query="silk tie", results=0)

    assert [d["asked"] for d in events.unmet_demand(merchant)] \
        == ["rain jacket"]
    assert [d["asked"] for d in events.unmet_demand("other")] == ["silk tie"]


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------

def test_summary_reports_the_empty_search_rate(merchant):
    for _ in range(3):
        events.record(merchant, "search", query="saree", results=4)
    events.record(merchant, "search", query="tuxedo", results=0)

    result = events.summary(merchant)
    assert result["searches"] == 4
    assert result["searches_with_no_results"] == 1
    assert result["empty_rate"] == 0.25


def test_summary_reports_conversion_from_proposals(merchant):
    for _ in range(4):
        events.record(merchant, "propose")
    events.record(merchant, "purchase_started")

    assert events.summary(merchant)["conversion"] == 0.25


def test_summary_with_no_events_is_empty_not_an_error(merchant):
    result = events.summary(merchant)
    assert result["searches"] == 0
    assert result["empty_rate"] is None
    assert result["conversion"] is None


def test_summary_reports_median_search_time(merchant):
    for ms in (10, 20, 30):
        events.record(merchant, "search", query="saree", results=1,
                      duration_ms=ms)
    assert events.summary(merchant)["median_search_ms"] == 20


# --------------------------------------------------------------------------
# retention
# --------------------------------------------------------------------------

def test_old_events_are_pruned(merchant):
    events.record(merchant, "search", query="saree", results=1)
    db.execute("UPDATE events SET at = now() - interval '60 days'")

    assert events.prune(days=30) == 1
    assert db.query_one("SELECT count(*) AS n FROM events")["n"] == 0


def test_recent_events_survive_pruning(merchant):
    events.record(merchant, "search", query="saree", results=1)
    assert events.prune(days=30) == 0
    assert db.query_one("SELECT count(*) AS n FROM events")["n"] == 1


def test_events_are_not_append_only(merchant):
    """The opposite of audit, and deliberately so. These rows are telemetry,
    not evidence, and they must be deletable on a schedule."""
    events.record(merchant, "search", query="saree", results=1)
    assert db.execute("DELETE FROM events") == 1
