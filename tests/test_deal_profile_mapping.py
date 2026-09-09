"""Unit tests for the parser deal_profile -> deal-field mapping (Path B).

Pure: no DB, no model. deal_profile_updates must only ever SET a screening
column (sector/hq_geography) it can resolve, prefer a confident approved match
over an out-of-mandate read, and never manufacture a value out of uncertainty.
When a dimension resolves to no screening value, it instead keeps the stated
(grounded raw) value in the display-only sector_raw/hq_geography_raw columns.
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


def test_unknown_fit_keeps_the_stated_sector_for_display() -> None:
    # No mandate mapping -> no screening value, but the deck stated a sector, so
    # it is kept in the display-only column for the Company Facts box.
    profiles = [{"sector_fit": _fit("unknown"), "sector": "something"}]
    assert deal_profile_updates(profiles) == {"sector_raw": "something"}


def test_no_mandate_options_keeps_the_stated_sector_for_display() -> None:
    # fit is None when the org supplied no sector options to map against. The
    # stated sector still surfaces for display, never for screening.
    profiles = [{"sector_fit": None, "sector": "something"}]
    assert deal_profile_updates(profiles) == {"sector_raw": "something"}


def test_unknown_fit_with_no_stated_value_sets_nothing() -> None:
    # Nothing stated and nothing mapped -> we never write an empty display value.
    profiles = [{"sector_fit": _fit("unknown"), "sector": None}]
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


def test_match_with_blank_option_keeps_the_stated_raw_but_no_screening_value() -> None:
    # A blank option can't be trusted as a screening value, but the stated sector
    # is still captured for display.
    profiles = [{"sector_fit": _fit("match", "   "), "sector": "x"}]
    assert deal_profile_updates(profiles) == {"sector_raw": "x"}


def test_hq_dimension_is_mapped_the_same_way() -> None:
    profiles = [{"hq_fit": _fit("match", "Canada"), "hq_geography": "Toronto, ON"}]
    assert deal_profile_updates(profiles) == {"hq_geography": "Canada"}


def test_stated_hq_is_kept_for_display_when_unmapped() -> None:
    profiles = [{"hq_fit": _fit("unknown"), "hq_geography": "Bozeman, MT"}]
    assert deal_profile_updates(profiles) == {"hq_geography_raw": "Bozeman, MT"}


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


def test_both_dimensions_fall_back_to_the_stated_values_together() -> None:
    profiles = [
        {
            "sector_fit": _fit("unknown"),
            "sector": "Cloud data platform",
            "hq_fit": None,
            "hq_geography": "Bozeman, MT",
        }
    ]
    assert deal_profile_updates(profiles) == {
        "sector_raw": "Cloud data platform",
        "hq_geography_raw": "Bozeman, MT",
    }


def test_a_mapped_dimension_does_not_also_emit_a_stated_raw() -> None:
    # The screening value and the display fallback are mutually exclusive per run:
    # a resolved sector never also writes sector_raw (the view prefers the mapped
    # value anyway, and this keeps the two columns from disagreeing on one run).
    profiles = [{"sector_fit": _fit("match", "Fintech"), "sector": "fintech lending"}]
    assert "sector_raw" not in deal_profile_updates(profiles)


def test_first_stated_value_wins_across_unmapped_documents() -> None:
    profiles = [
        {"sector_fit": _fit("unknown"), "sector": "  "},  # blank -> skipped
        {"sector_fit": _fit("unknown"), "sector": "Cloud data platform"},
        {"sector_fit": _fit("unknown"), "sector": "Data warehousing"},
    ]
    assert deal_profile_updates(profiles) == {"sector_raw": "Cloud data platform"}


def test_malformed_fit_sets_no_screening_value_but_keeps_the_stated_raw() -> None:
    # A malformed fit is ignored for screening; the stated sector is still kept.
    profiles = [{"sector_fit": "not-a-dict", "sector": "x"}]
    assert deal_profile_updates(profiles) == {"sector_raw": "x"}
