"""Unit tests for the parser deal_profile -> deal-field mapping (Path B).

Pure: no DB, no model. deal_profile_updates must only ever SET a column it can
resolve, prefer a confident approved match over an out-of-mandate read, and
never manufacture a value out of uncertainty.
"""

from app.services.deal_profile import deal_profile_updates


def _fit(status: str, option: str | None = None) -> dict:
    return {"status": status, "option": option}


def test_match_writes_the_approved_option_verbatim() -> None:
    profiles = [{"sector_fit": _fit("match", "Fintech"), "sector": "fintech lending"}]
    assert deal_profile_updates(profiles) == {"sector": "Fintech"}


def test_outside_writes_the_raw_read() -> None:
    profiles = [{"sector_fit": _fit("outside"), "sector": "cannabis retail"}]
    assert deal_profile_updates(profiles) == {"sector": "cannabis retail"}


def test_unknown_sets_nothing() -> None:
    profiles = [{"sector_fit": _fit("unknown"), "sector": "something"}]
    assert deal_profile_updates(profiles) == {}


def test_no_fit_sets_nothing() -> None:
    profiles = [{"sector_fit": None, "sector": "something"}]
    assert deal_profile_updates(profiles) == {}


def test_none_profiles_are_ignored() -> None:
    assert deal_profile_updates([None, None]) == {}
    assert deal_profile_updates([]) == {}


def test_match_beats_outside_across_documents() -> None:
    profiles = [
        {"sector_fit": _fit("outside"), "sector": "raw-from-doc-1"},
        {"sector_fit": _fit("match", "Healthcare IT"), "sector": "raw-from-doc-2"},
    ]
    assert deal_profile_updates(profiles) == {"sector": "Healthcare IT"}


def test_outside_needs_a_raw_value() -> None:
    profiles = [{"sector_fit": _fit("outside"), "sector": None}]
    assert deal_profile_updates(profiles) == {}


def test_match_with_blank_option_sets_nothing() -> None:
    profiles = [{"sector_fit": _fit("match", "   "), "sector": "x"}]
    assert deal_profile_updates(profiles) == {}


def test_hq_dimension_is_mapped_the_same_way() -> None:
    profiles = [{"hq_fit": _fit("match", "Canada"), "hq_geography": "Toronto, ON"}]
    assert deal_profile_updates(profiles) == {"hq_geography": "Canada"}


def test_both_dimensions_resolve_together() -> None:
    profiles = [
        {
            "sector_fit": _fit("match", "Fintech"),
            "sector": "payments",
            "hq_fit": _fit("outside"),
            "hq_geography": "Berlin, Germany",
        }
    ]
    assert deal_profile_updates(profiles) == {
        "sector": "Fintech",
        "hq_geography": "Berlin, Germany",
    }


def test_malformed_fit_is_ignored() -> None:
    profiles = [{"sector_fit": "not-a-dict", "sector": "x"}]
    assert deal_profile_updates(profiles) == {}
