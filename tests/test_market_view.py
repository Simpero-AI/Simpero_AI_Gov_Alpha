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


def test_an_unlisted_competitors_sizing_figure_does_not_win_the_slot():
    # A competitor NOT named in dashboard_structure folds to "Other", so the
    # named-subject filter can't drop it; its larger, later figure would outrank
    # the target's if the slot were decided on recency/magnitude alone. The
    # lead-subject priority must keep the target's own figure in the slot.
    claims = [
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=100_000_000,
            entity="AcmeCo",
            period_year=2023,
        ),
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=900_000_000,
            entity="BigRival",  # unlisted -> folds to "Other", not dropped by the filter
            period_year=2024,
        ),
    ]
    structure = {"subjects": [{"name": "AcmeCo", "entities": ["AcmeCo"]}]}

    view = build_market_view(claims, filenames={}, dashboard_structure=structure, company="AcmeCo")

    assert [f.value for f in view.sizing] == ["$100.00M"]


def test_an_unmapped_market_figure_fills_a_slot_the_target_lacks():
    # The lead-subject priority must not suppress a legitimate "Other" figure
    # (e.g. a market-descriptor entity) for a slot the target itself never reports.
    claims = [
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=5_000_000_000,
            entity="the UK student housing market",  # unmapped -> "Other"
            period_year=2024,
        ),
    ]
    structure = {"subjects": [{"name": "AcmeCo", "entities": ["AcmeCo"]}]}

    view = build_market_view(claims, filenames={}, dashboard_structure=structure, company="AcmeCo")

    assert [(f.label, f.value) for f in view.sizing] == [("TAM", "$5.00B")]


def test_an_unlisted_competitors_sizing_figure_does_not_fill_an_empty_slot():
    # The target reports NO TAM. An unlisted competitor's TAM folds to _UNMATCHED
    # and would fill the empty TAM slot, surfacing as the deal's own market size.
    # A bare company name is not a market descriptor, so the figure must be dropped
    # -- distinct from the market-descriptor case above, which legitimately fills it.
    claims = [
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=900_000_000,
            entity="BigRival",
            period_year=2024,
        ),
    ]
    structure = {"subjects": [{"name": "AcmeCo", "entities": ["AcmeCo"]}]}

    view = build_market_view(claims, filenames={}, dashboard_structure=structure, company="AcmeCo")

    assert view.sizing == []


def test_a_bare_competitor_is_dropped_even_when_the_deal_has_no_lead():
    # No dashboard subject, no company name (deal.name==""), and a single sizing
    # claim that never crosses the frequency threshold -> lead_subject is _UNMATCHED.
    # A named competitor also folds to _UNMATCHED, so without the `lead != _UNMATCHED`
    # guard it would pass the filter as if it were the lead. It is not a market
    # descriptor, so it must still be dropped.
    claims = [
        _claim(attribute_raw="Total Addressable Market", normalized=900_000_000, entity="BigRival"),
    ]

    view = build_market_view(claims, filenames={}, company="")

    assert view.sizing == []


def test_entity_with_whitespace_noise_still_folds_to_its_subject():
    # A claim whose entity carries trailing whitespace (plausible extraction noise)
    # must still fold to the registered subject: normalize_name absorbs it where a
    # bare casefold would not, so the target's own figure is kept rather than dropped.
    claims = [
        _claim(attribute_raw="Total Addressable Market", normalized=100_000_000, entity="AcmeCo "),
    ]
    structure = {"subjects": [{"name": "AcmeCo", "entities": ["AcmeCo"]}]}

    view = build_market_view(claims, filenames={}, dashboard_structure=structure, company="AcmeCo")

    assert [(f.label, f.value) for f in view.sizing] == [("TAM", "$100.00M")]


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


def test_market_cagr_ranks_by_signed_value_not_absolute_magnitude():
    # Same period + status, differing only in sign. An abs() magnitude tiebreak
    # would wrongly prefer the larger-magnitude shrinking figure (-20%) over real
    # growth (+5%); the CAGR (percent) slot ranks by the signed value, so +5% wins.
    claims = [
        _claim(
            attribute_raw="Market CAGR", normalized=-20.0, value_type="percent", period_year=2024
        ),
        _claim(attribute_raw="Market CAGR", normalized=5.0, value_type="percent", period_year=2024),
    ]

    view = build_market_view(claims, filenames={}, company="AcmeCo")

    assert [(f.label, f.value) for f in view.sizing] == [("Market Growth (CAGR)", "5%")]


def test_a_negative_market_cagr_is_kept_when_it_is_the_only_figure():
    # A shrinking market is a legitimate CAGR, not an extraction error to discard
    # by sign -- with one figure the slot shows it as-is.
    view = build_market_view(
        [
            _claim(
                attribute_raw="Market CAGR", normalized=-3.0, value_type="percent", period_year=2024
            )
        ],
        filenames={},
        company="AcmeCo",
    )
    assert [(f.label, f.value) for f in view.sizing] == [("Market Growth (CAGR)", "-3%")]


def test_qual_fact_falls_back_to_a_class_label_when_entity_is_blank():
    """F5: a market_definition / competitive_position assertion with no entity
    must not render a blank row header -- it falls back to a class-appropriate
    label, never an empty string (MarketFactResponse.label is required)."""
    claims = [
        _qual("The market is $5B and growing.", "market_definition", entity=""),
        _qual("Holds ~15% share.", "competitive_position", entity="  "),
    ]

    view = build_market_view(claims, filenames={})

    assert view.market_definition[0].label == "The market"
    assert view.competitive_position[0].label == "Competitor"
    # The raw (blank) entity is still preserved on the fact for downstream use.
    assert view.market_definition[0].entity == ""


def test_qualitative_lists_are_capped():
    """F7: a claim-dense CIM can surface dozens of competitor/market assertions;
    each qualitative list is capped so an unbounded list can't swamp the tab."""
    claims = [
        _qual(f"Competitor {i} note.", "competitive_position", entity=f"Rival {i:02d}")
        for i in range(20)
    ]

    view = build_market_view(claims, filenames={})

    assert len(view.competitive_position) == 12


def test_a_percent_tam_cagr_does_not_key_or_displace_the_dollar_tam():
    """A label pairing a sizing acronym with a growth qualifier ("TAM CAGR",
    value_type=percent) must not key the dollar TAM slot -- the value_type gate
    keeps the percent out, so it can never overwrite the real dollar TAM (which
    _sizing_rank, blind to value_type, would otherwise let it do)."""
    claims = [
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=5_000_000_000,
            value_type="currency",
            period_year=2024,
        ),
        _claim(
            attribute_raw="TAM CAGR ('23-'28E)",
            normalized=12.0,
            value_type="percent",
            period_year=2028,
            period_kind="E",
        ),
    ]

    view = build_market_view(claims, filenames={})

    # The one sizing row is the dollar TAM, not the percent -- the percent never
    # lands in the TAM slot.
    assert [f.label for f in view.sizing] == ["TAM"]
    assert "%" not in view.sizing[0].value


def test_all_caps_possessive_acronym_is_not_a_sizing_metric():
    """An all-caps possessive like "SAM'S CLUB REVENUE" (a retail comp's revenue
    line) must not false-match the SAM sizing row -- the apostrophe-s marks a
    possessive noun, not the Serviceable Addressable Market acronym, even all-caps."""
    claims = [
        _claim(attribute_raw="SAM'S CLUB REVENUE", normalized=1_000_000, value_type="currency")
    ]

    view = build_market_view(claims, filenames={})

    assert view.sizing == []


def test_a_non_currency_typed_claim_does_not_displace_a_dollar_size():
    """A claim typed anything other than the slot's expected type (e.g. a "count"
    whose label carries a sizing acronym, "TAM Customer Count") must not key the
    dollar TAM slot -- the value_type gate excludes ANY typed mismatch, not just
    currency/percent, so the count can't overwrite the real dollar TAM."""
    claims = [
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=5_000_000_000,
            value_type="currency",
            period_year=2023,
        ),
        _claim(
            attribute_raw="TAM Customer Count",
            normalized=9_000_000_000,
            value_type="count",
            period_year=2024,
        ),
    ]

    view = build_market_view(claims, filenames={})

    assert [(f.label, f.value) for f in view.sizing] == [("TAM", "$5.00B")]


def test_dashboard_subject_with_no_entities_does_not_disable_scoping():
    """A dashboard subject with a name but an empty entities list must not become
    an unusable lead (one no claim maps to), which would silently drop lead-subject
    scoping and let a competitor's larger figure win a slot. It falls through to
    the frequency election instead, so the target's own figure still wins."""
    dashboard = {"subjects": [{"name": "TargetCo", "entities": []}]}
    claims = [
        # TargetCo mentioned twice -> elected lead by frequency.
        _claim(
            attribute_raw="Total Addressable Market", normalized=5_000_000_000, entity="TargetCo"
        ),
        _claim(attribute="revenue", normalized=1_000_000, entity="TargetCo"),
        # A competitor whose larger TAM must NOT win the slot over the lead's.
        _claim(
            attribute_raw="Total Addressable Market", normalized=9_000_000_000, entity="RivalCo"
        ),
    ]

    view = build_market_view(
        claims, filenames={}, dashboard_structure=dashboard, company="TargetCo"
    )

    assert [(f.label, f.value) for f in view.sizing] == [("TAM", "$5.00B")]
    assert view.sizing[0].entity == "TargetCo"


def test_untrusted_claims_do_not_inflate_the_lead_election():
    """Only trusted, quantitative claims vote for the lead subject. Two untrusted
    (proposed) mentions of a bogus entity must not crown it lead over the real
    target and let its sizing figure win the slot."""
    claims = [
        # Bogus entity: two untrusted mentions (which would cross the freq>=2 vote
        # threshold if they counted) plus a trusted TAM that must not win.
        _claim(attribute="revenue", normalized=1, entity="BogusCo", status="proposed"),
        _claim(attribute="cogs", normalized=1, entity="BogusCo", status="proposed"),
        _claim(
            attribute_raw="Total Addressable Market", normalized=9_000_000_000, entity="BogusCo"
        ),
        # Real target: two trusted quantitative claims -> wins the election.
        _claim(attribute="revenue", normalized=1, entity="TargetCo"),
        _claim(
            attribute_raw="Total Addressable Market", normalized=5_000_000_000, entity="TargetCo"
        ),
    ]

    view = build_market_view(claims, filenames={})

    assert [(f.label, f.value) for f in view.sizing] == [("TAM", "$5.00B")]
    assert view.sizing[0].entity == "TargetCo"


def test_a_dashboard_subject_named_other_does_not_collide_with_unmatched():
    """A dashboard subject literally named "Other" must not share the lead's sizing
    priority with unmatched entities: an unlisted competitor folds to the internal
    _UNMATCHED sentinel, not to the real "Other" subject, so it can't win the slot."""
    dashboard = {"subjects": [{"name": "Other", "entities": ["OtherCo"]}]}
    claims = [
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=5_000_000_000,
            entity="OtherCo",  # belongs to the real lead subject "Other"
            period_year=2024,
        ),
        _claim(
            attribute_raw="Total Addressable Market",
            normalized=9_000_000_000,
            entity="RivalCo",  # unlisted -> _UNMATCHED, must not inherit lead priority
            period_year=2024,
        ),
    ]

    view = build_market_view(claims, filenames={}, dashboard_structure=dashboard, company="OtherCo")

    assert [(f.label, f.value) for f in view.sizing] == [("TAM", "$5.00B")]
    assert view.sizing[0].entity == "OtherCo"


def test_company_leads_even_when_a_competitor_has_more_claims():
    """The deal's own company is the target: when it appears among the trusted
    quantitative claims it leads outright, so a competitor with MORE claims can't
    win the election and completely replace the target's own sizing figure (sizing
    keeps a single winner per slot)."""
    claims = [
        _claim(attribute_raw="Total Addressable Market", normalized=100_000_000, entity="TargetCo"),
        # Competitor with more trusted quantitative claims (crosses f>=2 first).
        _claim(attribute_raw="Total Addressable Market", normalized=900_000_000, entity="RivalCo"),
        _claim(attribute="revenue", normalized=1, entity="RivalCo"),
    ]

    view = build_market_view(claims, filenames={}, company="TargetCo")

    assert [(f.label, f.value) for f in view.sizing] == [("TAM", "$100.00M")]
    assert view.sizing[0].entity == "TargetCo"
