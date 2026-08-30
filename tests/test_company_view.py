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
    unit: str | None = None,
    entity: str = "AcmeCo",
    status: str = "verified",
    claim_kind: str | None = None,
    assertion_class: str | None = None,
    period_year: int | None = None,
    period_kind: str | None = None,
    kind: str = "pdf",
    page: int | None = 1,
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
            "unit": unit if unit is not None else ("USD" if value_type == "currency" else None),
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


def test_a_competitors_identity_fact_is_not_shown_as_the_targets():
    # Subject filter: a headcount claim about a mentioned competitor must not be
    # picked as the deal company's own.
    claims = [
        _claim(
            attribute_raw="Total Employees", normalized=1450, value_type="count", entity="AcmeCo"
        ),
        _claim(
            attribute_raw="Total Employees",
            normalized=50000,
            value_type="count",
            entity="BigCorp Competitor",
        ),
    ]
    structure = {"subjects": [{"name": "AcmeCo", "entities": ["AcmeCo"]}]}

    view = build_company_view(claims, filenames={}, dashboard_structure=structure)

    assert [f.value for f in view.facts if f.label == "Headcount"] == ["1,450"]


def test_latest_headcount_wins_over_a_stale_larger_one():
    # Recency, not magnitude: a company that shrank shows its latest headcount.
    claims = [
        _claim(
            attribute_raw="Total Employees", normalized=2000, value_type="count", period_year=2021
        ),
        _claim(
            attribute_raw="Total Employees", normalized=1200, value_type="count", period_year=2024
        ),
    ]

    view = build_company_view(claims, filenames={})

    assert [f.value for f in view.facts if f.label == "Headcount"] == ["1,200"]


def test_a_percent_metric_is_not_mislabeled_as_headcount():
    # "Total Employees Turnover %" carries the headcount words but is a percent --
    # the value_type guard keeps it out; the real count keeps the slot.
    claims = [
        _claim(
            attribute_raw="Total Employees Turnover",
            normalized=12.5,
            value_type="percent",
            unit="%",
        ),
        _claim(attribute_raw="Total Employees", normalized=1450, value_type="count"),
    ]

    view = build_company_view(claims, filenames={})

    assert [f.value for f in view.facts if f.label == "Headcount"] == ["1,450"]


def test_a_qualitative_claim_with_no_text_does_not_show_an_empty_row():
    claims = [
        _claim(
            raw="",
            value_type="text",
            normalized=None,
            claim_kind="qualitative",
            assertion_class="operating_model",
        ),
        _claim(
            raw="Real ops note.",
            value_type="text",
            normalized=None,
            claim_kind="qualitative",
            assertion_class="operating_model",
        ),
    ]

    view = build_company_view(claims, filenames={})

    assert [f.value for f in view.overview] == ["Real ops note."]


def test_founded_date_renders_the_year_without_grouping():
    # A founding date typed "date" with a numeric year falls through the numeric
    # formatter and would read "1,998"; the year must read "1998".
    claims = [
        _claim(attribute_raw="Founded", normalized=1998, value_type="date", raw="1998"),
    ]

    view = build_company_view(claims, filenames={}, company="AcmeCo")

    by_label = {f.label: f.value for f in view.facts}
    assert by_label["Founded"] == "1998"


def test_employees_terminated_is_not_mislabeled_as_headcount():
    # "Employees Terminated" is a count too, so the value-type guard can't reject
    # it -- the exclude token ("terminated") must. It is a flow, not a headcount.
    claims = [
        _claim(attribute_raw="Employees Terminated", normalized=120, value_type="count"),
    ]

    view = build_company_view(claims, filenames={}, company="AcmeCo")

    assert all(f.label != "Headcount" for f in view.facts)


def test_freq_fallback_anchors_the_lead_on_the_company_excluding_competitors():
    # No dashboard structure and no entity crosses the frequency threshold (each
    # appears once). Without an anchor the subject filter is a no-op and the
    # competitor's later, larger headcount would win; anchoring the lead on the
    # deal's company keeps the target's figure and drops the competitor's.
    claims = [
        _claim(
            attribute_raw="Total Employees",
            normalized=1_450,
            value_type="count",
            entity="AcmeCo",
            period_year=2023,
        ),
        _claim(
            attribute_raw="Total Employees",
            normalized=50_000,
            value_type="count",
            entity="Rival Corp",
            period_year=2024,
        ),
    ]

    view = build_company_view(claims, filenames={}, company="AcmeCo")

    assert [f.value for f in view.facts if f.label == "Headcount"] == ["1,450"]


def test_related_party_assertion_about_a_third_party_is_kept():
    # A related-party assertion's entity is the party it names -- a director or
    # affiliate (a third party), which folds to "Other". It must still surface in
    # Related Parties, not be dropped by the lead-subject filter.
    claims = [
        _qual(
            "Mr Smith, a director, also serves on the board of Acme Ltd.",
            "related_party",
            entity="Mr Smith",
        ),
    ]

    view = build_company_view(claims, filenames={}, company="TargetCo")

    assert [f.value for f in view.related_parties] == [
        "Mr Smith, a director, also serves on the board of Acme Ltd."
    ]


def test_related_party_assertion_about_a_named_competitor_is_dropped():
    # A related-party assertion whose entity resolves to a NAMED competitor subject
    # belongs to that rival, not the target -- still filtered out.
    claims = [
        _qual(
            "Rival Corp is under common control with the group.",
            "related_party",
            entity="Rival Corp",
        ),
    ]
    structure = {
        "subjects": [
            {"name": "TargetCo", "entities": ["TargetCo"]},
            {"name": "Rival Corp", "entities": ["Rival Corp"]},
        ]
    }

    view = build_company_view(
        claims, filenames={}, dashboard_structure=structure, company="TargetCo"
    )

    assert view.related_parties == []


def test_a_non_related_party_assertion_about_a_third_party_is_still_dropped():
    # The related-party exemption is scoped: a non-related-party qualitative claim
    # about a third party is still filtered out (unchanged behavior).
    claims = [
        _qual("Competitor context about a rival.", "operating_model", entity="Rival Corp"),
    ]

    view = build_company_view(claims, filenames={}, company="TargetCo")

    assert view.overview == []
