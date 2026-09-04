"""Reports arriving from policy engines on merchants' own servers.

Everything this module handles was written by software we cannot inspect,
posted over the internet, authorised by a key that may have leaked. The tests
worth having are the ones where trusting it would be quiet and expensive.
"""

import pytest

import db
import policy_log


@pytest.fixture(autouse=True)
def clean_reports(clean_database):
    with db.transaction() as conn:
        conn.execute("TRUNCATE policy_decisions")
    yield


def report(**overrides):
    payload = {
        "sku": "SAR-104",
        "result": "approved",
        "asked_bps": 800,
        "allowed_bps": 1200,
        "failed_rules": [],
        "engine_version": "1.2.0",
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------
# whose log is this
# --------------------------------------------------------------------------

def test_a_report_is_stored_against_the_key_s_merchant(merchant):
    policy_log.record(merchant, report())
    row = db.query_one("SELECT * FROM policy_decisions")

    assert row["merchant_id"] == merchant
    assert row["sku"] == "SAR-104"
    assert row["result"] == "approved"


def test_a_merchant_cannot_write_into_another_merchant_s_log(merchant):
    """merchant_id in the body is ignored. If it were honoured, one leaked
    key would let anyone forge history for every shop on the system."""
    policy_log.record(merchant, report(merchant_id="someone-else"))

    row = db.query_one("SELECT merchant_id FROM policy_decisions")
    assert row["merchant_id"] == merchant


# --------------------------------------------------------------------------
# the version gate
# --------------------------------------------------------------------------

def test_a_current_engine_is_accepted(merchant):
    policy_log.record(merchant, report(), engine_version="1.2.0")
    assert db.query_one("SELECT count(*) AS n FROM policy_decisions")["n"] == 1


def test_an_old_engine_is_refused(monkeypatch, merchant):
    monkeypatch.setattr(policy_log, "MIN_ENGINE_VERSION", "1.2.0")
    with pytest.raises(policy_log.VersionTooOld, match="pip install"):
        policy_log.record(merchant, report(), engine_version="1.0.0")


def test_a_refused_report_is_not_stored(monkeypatch, merchant):
    monkeypatch.setattr(policy_log, "MIN_ENGINE_VERSION", "2.0.0")
    with pytest.raises(policy_log.VersionTooOld):
        policy_log.record(merchant, report(), engine_version="1.2.0")

    assert db.query_one("SELECT count(*) AS n FROM policy_decisions")["n"] == 0


def test_a_missing_version_is_treated_as_ancient_not_as_current(
        monkeypatch, merchant):
    """A version we cannot read must sort low. Sorting it high would make an
    empty header the way past the gate."""
    monkeypatch.setattr(policy_log, "MIN_ENGINE_VERSION", "1.0.0")
    with pytest.raises(policy_log.VersionTooOld):
        policy_log.record(merchant, report(engine_version=None),
                          engine_version=None)


def test_version_comparison_is_numeric_not_alphabetical():
    """"1.10.0" is newer than "1.9.0" and string comparison says otherwise."""
    assert (policy_log.parse_version("1.10.0")
            > policy_log.parse_version("1.9.0"))
    assert (policy_log.parse_version("2.0.0")
            > policy_log.parse_version("1.99.99"))


def test_a_partial_version_still_parses():
    assert policy_log.parse_version("2") == (2, 0, 0)
    assert policy_log.parse_version("1.4") == (1, 4, 0)


def test_junk_versions_sort_lowest():
    assert policy_log.parse_version("banana") == (0, 0, 0)
    assert policy_log.parse_version("") == (0, 0, 0)
    assert policy_log.parse_version(None) == (0, 0, 0)


# --------------------------------------------------------------------------
# bounding what arrives
# --------------------------------------------------------------------------

def test_an_unknown_result_is_refused(merchant):
    with pytest.raises(policy_log.InvalidRecord, match="result"):
        policy_log.record(merchant, report(result="maybe"))


def test_a_refusal_must_name_the_rule_that_blocked_it(merchant):
    """A refusal with no reason is a bug in the reporting engine. Storing it
    would put a row in the log that no one can act on."""
    with pytest.raises(policy_log.InvalidRecord, match="rule"):
        policy_log.record(merchant, report(result="refused",
                                           failed_rules=[]))


def test_basis_points_outside_the_range_are_refused(merchant):
    with pytest.raises(policy_log.InvalidRecord, match="asked_bps"):
        policy_log.record(merchant, report(asked_bps=20000))
    with pytest.raises(policy_log.InvalidRecord, match="allowed_bps"):
        policy_log.record(merchant, report(allowed_bps=-1))


def test_a_missing_sku_is_refused(merchant):
    with pytest.raises(policy_log.InvalidRecord, match="sku"):
        policy_log.record(merchant, report(sku="   "))


def test_a_long_rule_list_is_truncated_rather_than_rejected(merchant):
    """A newer engine may know rules this deployment does not. Dropping the
    report would lose real data; keeping all of it unbounded would let a
    caller decide how much of our storage to use."""
    policy_log.record(merchant, report(
        result="refused",
        failed_rules=[f"rule_{i}" for i in range(50)]))

    row = db.query_one("SELECT failed_rules FROM policy_decisions")
    assert len(row["failed_rules"]) == policy_log.MAX_RULES


def test_an_overlong_rule_name_is_truncated(merchant):
    policy_log.record(merchant, report(result="refused",
                                       failed_rules=["x" * 500]))
    row = db.query_one("SELECT failed_rules FROM policy_decisions")
    assert len(row["failed_rules"][0]) == policy_log.MAX_RULE_NAME


def test_failed_rules_must_be_a_list(merchant):
    with pytest.raises(policy_log.InvalidRecord, match="list"):
        policy_log.record(merchant, report(failed_rules="margin_floor"))


# --------------------------------------------------------------------------
# what a merchant can see
# --------------------------------------------------------------------------

def test_summary_counts_approvals_and_refusals(merchant):
    for _ in range(3):
        policy_log.record(merchant, report())
    policy_log.record(merchant, report(result="refused",
                                       failed_rules=["margin_floor"]))

    result = policy_log.summary(merchant)
    assert result["decisions"] == 4
    assert result["approved"] == 3
    assert result["refused"] == 1
    assert result["refusal_rate"] == 0.25


def test_summary_names_the_rule_costing_the_most_sales(merchant):
    for _ in range(5):
        policy_log.record(merchant, report(result="refused",
                                           failed_rules=["discount_cap"]))
    policy_log.record(merchant, report(result="refused",
                                       failed_rules=["inventory"]))

    assert policy_log.summary(merchant)["top_refusal"] == "discount_cap"


def test_summary_reports_which_engine_versions_are_running(merchant):
    policy_log.record(merchant, report(), engine_version="1.2.0")
    policy_log.record(merchant, report(), engine_version="1.1.0")

    versions = {v["engine_version"]
                for v in policy_log.summary(merchant)["engine_versions"]}
    assert versions == {"1.2.0", "1.1.0"}


def test_summary_of_a_merchant_with_no_reports_is_empty_not_an_error(merchant):
    result = policy_log.summary(merchant)
    assert result["decisions"] == 0
    assert result["refusal_rate"] is None
    assert result["top_refusal"] is None


def test_summary_shows_only_this_merchant(merchant):
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO merchants (id, name, api_key_hash) "
            "VALUES ('other', 'Other Shop', 'otherhash')")
    policy_log.record(merchant, report())
    policy_log.record("other", report())

    assert policy_log.summary(merchant)["decisions"] == 1
    assert policy_log.summary("other")["decisions"] == 1
