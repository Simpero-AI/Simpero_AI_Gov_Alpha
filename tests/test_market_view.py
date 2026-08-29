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
