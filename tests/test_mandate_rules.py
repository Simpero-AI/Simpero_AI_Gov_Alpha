"""Unit tests for mandate-selection -> rule-id mapping (Path B gating).

Pure: uses the real rulebook (loaded from track_b.yaml) and a hand-built
WorkspaceConfig -- no DB. Verifies that free-text options resolve to the right
rule, stay confined to their kind, and that gs_07/gs_08 turn on with the
geography/sector policy.
"""

from app.services.screening.mandate_rules import map_option_to_rule_id, selected_rule_ids
from app.services.screening.rulebook import load_rulebook
from app.services.screening.workspace_config import WorkspaceConfig

_RB = load_rulebook()


def _config(**kwargs) -> WorkspaceConfig:
    base = {
        "approved_sectors": None,
        "approved_geographies": None,
        "must_have_options": None,
        "deal_breaker_options": None,
    }
    base.update(kwargs)
    return WorkspaceConfig(**base)


def test_option_that_is_a_rule_id_maps() -> None:
    assert map_option_to_rule_id("gs_03", "green_signal", _RB) == "gs_03"


def test_option_that_is_the_canonical_question_maps() -> None:
    assert (
        map_option_to_rule_id("Company has a paying customer (any revenue)", "green_signal", _RB)
        == "gs_03"
    )


def test_matching_is_case_and_whitespace_insensitive() -> None:
    assert (
        map_option_to_rule_id("  company HAS a paying customer (any revenue) ", "green_signal", _RB)
        == "gs_03"
    )


def test_live_taxonomy_options_map_to_the_right_rules() -> None:
    """Pin the mapping against the ACTUAL Must-Have / Deal-Breaker option strings
    configured in the live taxonomy (verified against staging 2026-08-25). They
    are the rulebook questions verbatim, so each maps via the question match with
    no alias -- if an admin later re-words one, this test is where it surfaces."""
    must_have = {
        "HQ in approved geography": "gs_07",
        "Founder(s) full-time on the business": "gs_01",
        "Company has a paying customer (any revenue)": "gs_03",
        "Operates in approved sector": "gs_08",
    }
    deal_breaker = {
        "Founder seeking full exit within 24 months": "db_03",
        "Sanctioned country or individual involved": "db_09",
        "No paying customers and no signed LOIs/pilots": "db_01",
    }
    for option, rule_id in must_have.items():
        assert map_option_to_rule_id(option, "green_signal", _RB) == rule_id
    for option, rule_id in deal_breaker.items():
        assert map_option_to_rule_id(option, "deal_breaker", _RB) == rule_id


def test_deal_breaker_question_maps_to_its_rule() -> None:
    assert map_option_to_rule_id("Single customer >70% of revenue", "deal_breaker", _RB) == "db_07"


def test_kind_is_confined_no_cross_kind_match() -> None:
    # gs_03's question must not resolve when asked for a deal-breaker.
    assert (
        map_option_to_rule_id("Company has a paying customer (any revenue)", "deal_breaker", _RB)
        is None
    )


def test_unmapped_option_returns_none() -> None:
    assert map_option_to_rule_id("something no rule says", "green_signal", _RB) is None
    assert map_option_to_rule_id("", "green_signal", _RB) is None


def test_selected_from_must_haves_and_deal_breakers() -> None:
    config = _config(
        must_have_options=["Company has a paying customer (any revenue)"],
        deal_breaker_options=["Single customer >70% of revenue"],
        approved_sectors=["Fintech"],
        approved_geographies=None,
    )
    # gs_03 (must-have), db_07 (deal-breaker), gs_08 (sector policy set).
    # gs_07 absent -- no geography policy.
    assert selected_rule_ids(_RB, config) == {"gs_03", "db_07", "gs_08"}


def test_geography_policy_turns_on_gs_07() -> None:
    config = _config(approved_geographies=["Canada"])
    assert selected_rule_ids(_RB, config) == {"gs_07"}


def test_nothing_configured_selects_nothing() -> None:
    assert selected_rule_ids(_RB, _config()) == set()


def test_unmappable_options_are_dropped_not_errored() -> None:
    config = _config(must_have_options=["free text nobody mapped"])
    assert selected_rule_ids(_RB, config) == set()
