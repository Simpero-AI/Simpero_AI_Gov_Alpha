"""Unit tests for build_market_view -- pure curation over in-memory Claim
objects, no database. Guards which claims surface on the Market tab: numeric
sizing recovered by label, qualitative market/competition assertions by
assertion_class, only trust-earned, never fabricated."""

import uuid

from app.models.claim import Claim
from app.services.market_view import build_market_view


def _claim(
    *,
    attribute: str = "operating_metric",
    attribute_raw: str | None = None,
    normalized: float | None = None,
    raw: str | None = None,
    value_type: str = "currency",
    entity: str = "AcmeCo",
    status: str = "verified",
    claim_kind: str | None = None,
    assertion_class: str | None = None,
    period_year: int | None = None,
    period_kind: str | None = None,
    kind: str = "pdf",
    page: int | None = 1,
    data_source_id: uuid.UUID | None = None,
) -> Claim:
    return Claim(
        entity=entity,
        attribute=attribute,
        attribute_raw=attribute_raw,
        claim_kind=claim_kind,
        assertion_class=assertion_class,
        period_year=period_year,
        period_kind=period_kind,
        value={
            "raw": raw if raw is not None else str(normalized),
            "normalized": normalized,
            "unit": "USD" if value_type == "currency" else None,
            "value_type": value_type,
        },
        kind=kind,
        page=page,
        status=status,
        data_source_id=data_source_id,
    )


def _qual(text: str, assertion_class: str, **kw) -> Claim:
    return _claim(
        raw=text,
        value_type="text",
        normalized=None,
        claim_kind="qualitative",
        assertion_class=assertion_class,
        **kw,
    )


def test_recovers_market_sizing_by_label():
    claims = [
        _claim(attribute_raw="Total Addressable Market", normalized=5_000_000_000),
        _claim(attribute_raw="Serviceable Obtainable Market (SOM)", normalized=400_000_000),
        _claim(attribute_raw="Estimated Market Size", normalized=1_200_000_000),
    ]

    view = build_market_view(claims, filenames={})

    labels = [f.label for f in view.sizing]
    assert labels == ["TAM", "SOM", "Market Size"]  # in funnel order
    assert view.sizing[0].value == "$5.00B"
    assert view.sizing[0].citation == "p.1"


def test_som_acronym_does_not_match_an_unrelated_word():
    # "some" contains the letters of SOM, but the acronym only matches as a
    # standalone token -- a stray "awesome"/"wholesome" label is not sizing.
    claims = [_claim(attribute_raw="Awesome Retention Metric", normalized=42)]

    view = build_market_view(claims, filenames={})

    assert view.sizing == []


def test_qualitative_market_and_competition_assertions_surface_with_status():
    claims = [
        _qual(
            "The UK student housing market is highly fragmented with no national operator.",
            "market_definition",
            entity="UK student housing market",
        ),
        _qual(
            "The company holds the leading position in three of its four regions.",
            "competitive_position",
            entity="AcmeCo",
            status="cited",
        ),
    ]

    view = build_market_view(claims, filenames={})

    assert [f.value for f in view.market_definition] == [
        "The UK student housing market is highly fragmented with no national operator."
    ]
    assert view.market_definition[0].status == "verified"
    assert view.market_definition[0].entity == "UK student housing market"
    assert view.market_definition[0].citation == "p.1"

    assert [f.value for f in view.competitive_position] == [
        "The company holds the leading position in three of its four regions."
    ]
    assert view.competitive_position[0].status == "cited"


def test_untrusted_and_unrelated_claims_are_excluded():
    claims = [
        _qual("Draft market note.", "market_definition", status="proposed"),  # untrusted
        _claim(attribute="revenue", normalized=100_000_000),  # a financial, not market
        _qual("Real competitive note.", "competitive_position"),  # trusted, shown
    ]

    view = build_market_view(claims, filenames={})

    assert view.sizing == []
    assert view.market_definition == []
    assert [f.value for f in view.competitive_position] == ["Real competitive note."]


def test_empty_deal_yields_empty_view():
    view = build_market_view([], filenames={})
    assert view.sizing == []
    assert view.market_definition == []
    assert view.competitive_position == []


def test_a_competitors_sizing_figure_does_not_win_the_slot():
    # A named competitor subject's TAM must not displace the target's; the
    # unmapped/lead figure is kept, the competitor's dropped.
    claims = [
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=100_000_000,
            entity="AcmeCo",
            period_year=2024,
        ),
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=900_000_000,
            entity="Rival Corp",
            period_year=2024,
        ),
    ]
    structure = {
        "subjects": [
            {"name": "AcmeCo", "entities": ["AcmeCo"]},
            {"name": "Rival Corp", "entities": ["Rival Corp"]},
        ]
    }

    view = build_market_view(claims, filenames={}, dashboard_structure=structure, company="AcmeCo")

    assert [f.value for f in view.sizing] == ["$100.00M"]


def test_a_qualitative_market_fact_with_no_text_does_not_show_an_empty_row():
    claims = [
        _qual("", "market_definition"),
        _qual("The market is highly fragmented.", "market_definition"),
    ]

    view = build_market_view(claims, filenames={}, company="AcmeCo")

    assert [f.value for f in view.market_definition] == ["The market is highly fragmented."]


def test_a_competitive_position_fact_about_a_competitor_is_still_shown():
    # competitive_position is ABOUT competitors, so the subject filter must not
    # drop it even though its entity is not the target.
    claims = [
        _qual("Rival Corp leads with a 40% share.", "competitive_position", entity="Rival Corp"),
    ]
    structure = {"subjects": [{"name": "AcmeCo", "entities": ["AcmeCo"]}]}

    view = build_market_view(claims, filenames={}, dashboard_structure=structure, company="AcmeCo")

    assert [f.value for f in view.competitive_position] == ["Rival Corp leads with a 40% share."]


def test_a_bare_revenue_cagr_is_not_classified_as_market_growth():
    # "Revenue CAGR" is a company growth rate, not a market one; only a
    # market-qualified label lands in the Market Growth (CAGR) row.
    claims = [
        _claim(attribute_raw="Revenue CAGR", normalized=18.0, value_type="percent"),
    ]

    view = build_market_view(claims, filenames={}, company="AcmeCo")

    assert view.sizing == []


def test_market_cagr_is_classified_and_latest_period_wins():
    claims = [
        _claim(
            attribute_raw="Market CAGR",
            normalized=12.0,
            value_type="percent",
            period_year=2021,
        ),
        _claim(
            attribute_raw="Market CAGR",
            normalized=9.0,
            value_type="percent",
            period_year=2024,
        ),
    ]

    view = build_market_view(claims, filenames={}, company="AcmeCo")

    # Later year wins the single CAGR slot, not the larger stale figure.
    assert [(f.label, f.value) for f in view.sizing] == [("Market Growth (CAGR)", "9%")]
