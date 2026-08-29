"""Unit tests for build_company_view -- pure curation over in-memory Claim
objects, no database. Guards what surfaces on the Business Overview tab:
identity facts (deal-profile sector/HQ + headcount/founded by label) and the
qualitative assertions grouped by assertion_class, only trust-earned."""

from app.models.claim import Claim
from app.services.company_view import build_company_view


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


def test_facts_include_deal_profile_and_identity_claims():
    claims = [
        _claim(attribute_raw="Total Employees", normalized=1_450, value_type="count"),
        _claim(attribute_raw="Year Founded", raw="1998", normalized=None, value_type="text"),
    ]

    view = build_company_view(
        claims,
        filenames={},
        sector="Gaming & Leisure",
        hq_geography="Las Vegas, NV",
        company="AcmeCo",
    )

    by_label = {f.label: f for f in view.facts}
    assert by_label["Sector"].value == "Gaming & Leisure"
    assert by_label["Sector"].status == "derived"
    assert by_label["Sector"].citation is None
    assert by_label["Headquarters"].value == "Las Vegas, NV"
    assert by_label["Headcount"].value == "1,450"
    assert by_label["Headcount"].citation == "p.1"
    assert by_label["Founded"].value == "1998"


def test_qualitative_assertions_group_by_class():
    claims = [
        _qual("Revenue is 70% recurring subscription.", "operating_model"),
        _qual("Heavily dependent on a single supplier.", "risk_or_dependency"),
        _qual("Three-year contracts with 5% annual uplift.", "commercial_terms"),
        _qual("The founder's brother owns the leased HQ.", "related_party"),
        _qual("Plans to enter the German market in 2027.", "plan_or_commitment"),
        # Market classes belong to the Market tab, not here.
        _qual("The market is fragmented.", "market_definition"),
        _qual("We lead the mid-market segment.", "competitive_position"),
    ]

    view = build_company_view(claims, filenames={})

    assert [f.value for f in view.overview] == ["Revenue is 70% recurring subscription."]
    assert [f.value for f in view.risks] == ["Heavily dependent on a single supplier."]
    assert [f.value for f in view.commercial] == ["Three-year contracts with 5% annual uplift."]
    assert [f.value for f in view.related_parties] == ["The founder's brother owns the leased HQ."]
    assert [f.value for f in view.plans] == ["Plans to enter the German market in 2027."]
    # Market-class assertions did not leak into any Company section.
    all_company_text = [
        f.value
        for section in (
            view.overview,
            view.risks,
            view.commercial,
            view.related_parties,
            view.plans,
        )
        for f in section
    ]
    assert "The market is fragmented." not in all_company_text
    assert "We lead the mid-market segment." not in all_company_text


def test_untrusted_and_unlabelled_claims_are_excluded():
    claims = [
        _qual("Draft note.", "operating_model", status="proposed"),  # untrusted
        _claim(attribute="revenue", normalized=100_000_000),  # a financial, not identity
        _qual("Real ops note.", "operating_model"),  # trusted, shown
    ]

    view = build_company_view(claims, filenames={})

    assert view.facts == []  # no sector/hq passed, no identity claim
    assert [f.value for f in view.overview] == ["Real ops note."]


def test_empty_deal_yields_empty_view():
    view = build_company_view([], filenames={})
    assert view.facts == []
    assert view.overview == []
    assert view.risks == []
    assert view.commercial == []
    assert view.related_parties == []
    assert view.plans == []
