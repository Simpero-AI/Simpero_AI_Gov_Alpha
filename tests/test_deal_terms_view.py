"""Unit tests for build_deal_terms_view -- pure curation over in-memory Claim
objects, no database. Guards which claims surface as Key Deal Terms: deal-structure
scalars recovered by label from the operating_metric/core_unmapped catch-all
buckets, value_type-gated, subject-scoped, best-per-slot, only trust-earned, never
fabricated."""

import uuid

from app.models.claim import Claim
from app.services.deal_terms_view import build_deal_terms_view


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
    data_source_id: uuid.UUID | None = None,
) -> Claim:
    if unit is None and value_type == "currency":
        unit = "USD"
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
            "unit": unit,
            "value_type": value_type,
        },
        kind=kind,
        page=page,
        status=status,
        data_source_id=data_source_id,
    )


def _labels(view) -> list[str]:
    return [t.label for t in view.terms]


def _by_label(view, label: str):
    return next(t for t in view.terms if t.label == label)


def test_empty_deal_yields_empty_terms():
    view = build_deal_terms_view([], filenames={})
    assert view.terms == []


def test_recovers_valuation_investment_ownership_by_label():
    claims = [
        _claim(attribute_raw="Pre-Money Valuation", normalized=40_000_000),
        _claim(attribute_raw="Investment Amount", normalized=10_000_000),
        _claim(attribute_raw="Ownership", normalized=16.7, value_type="percent", unit="%"),
    ]
    view = build_deal_terms_view(claims, filenames={}, company="AcmeCo")
    got = {t.label: t.value for t in view.terms}
    assert got == {
        "Investment Amount": "$10.00M",
        "Pre-Money Valuation": "$40.00M",
        "Ownership Stake": "16.7%",
    }


def test_value_type_gate_excludes_wrong_typed_figures():
    # A percent labelled "valuation" ("valuation grew 20%") must not fill the
    # dollar valuation slot; a dollar labelled "ownership" ("ownership costs
    # $2M") must not fill the percent ownership slot.
    claims = [
        _claim(attribute_raw="Valuation", normalized=20.0, value_type="percent", unit="%"),
        _claim(attribute_raw="Ownership", normalized=2_000_000, value_type="currency"),
    ]
    view = build_deal_terms_view(claims, filenames={}, company="AcmeCo")
    assert view.terms == []


def test_specific_pre_money_beats_generic_valuation_slot():
    # "pre-money valuation" keys Pre-Money (specific slot, checked first), and a
    # bare "Valuation" keys the generic slot -- the two coexist, never collapse.
    claims = [
        _claim(attribute_raw="Pre-Money Valuation", normalized=40_000_000),
        _claim(attribute_raw="Post-Money Valuation", normalized=50_000_000),
        _claim(attribute_raw="Valuation", normalized=45_000_000),
    ]
    view = build_deal_terms_view(claims, filenames={}, company="AcmeCo")
    assert {t.label: t.value for t in view.terms} == {
        "Pre-Money Valuation": "$40.00M",
        "Post-Money Valuation": "$50.00M",
        "Valuation": "$45.00M",
    }


def test_a_named_competitors_valuation_is_dropped():
    # A named non-lead subject's stated valuation must not surface as the deal's.
    claims = [
        _claim(attribute_raw="Pre-Money Valuation", normalized=40_000_000, entity="AcmeCo"),
        _claim(attribute_raw="Pre-Money Valuation", normalized=900_000_000, entity="Rival Corp"),
    ]
    structure = {
        "subjects": [
            {"name": "AcmeCo", "entities": ["AcmeCo"]},
            {"name": "Rival Corp", "entities": ["Rival Corp"]},
        ]
    }
    view = build_deal_terms_view(
        claims, filenames={}, dashboard_structure=structure, company="AcmeCo"
    )
    assert [t.value for t in view.terms] == ["$40.00M"]


def test_an_unmatched_the_company_figure_is_kept():
    # Deal terms are commonly phrased "the Company's pre-money valuation ...",
    # which folds to UNMATCHED (not the deal's registered name). It is about the
    # deal, so it is kept -- distinct from a NAMED rival, which is dropped.
    claims = [
        _claim(attribute_raw="Pre-Money Valuation", normalized=40_000_000, entity="the Company"),
    ]
    structure = {"subjects": [{"name": "AcmeCo", "entities": ["AcmeCo"]}]}
    view = build_deal_terms_view(
        claims, filenames={}, dashboard_structure=structure, company="AcmeCo"
    )
    assert [t.value for t in view.terms] == ["$40.00M"]


def test_lead_subject_priority_beats_unmatched_for_same_slot():
    # When both the target and an UNMATCHED entity report the same slot, the
    # target's own figure wins regardless of the other's larger magnitude.
    claims = [
        _claim(attribute_raw="Pre-Money Valuation", normalized=40_000_000, entity="AcmeCo"),
        _claim(attribute_raw="Pre-Money Valuation", normalized=99_000_000, entity="the Offering"),
    ]
    structure = {"subjects": [{"name": "AcmeCo", "entities": ["AcmeCo"]}]}
    view = build_deal_terms_view(
        claims, filenames={}, dashboard_structure=structure, company="AcmeCo"
    )
    assert [t.value for t in view.terms] == ["$40.00M"]


def test_latest_actual_beats_forecast_then_status():
    # Recency-first: an unmarked historical figure beats a projected one, even a
    # later-dated projection.
    claims = [
        _claim(
            attribute_raw="Post-Money Valuation",
            normalized=60_000_000,
            period_year=2026,
            period_kind="P",
        ),
        _claim(attribute_raw="Post-Money Valuation", normalized=50_000_000, period_year=2024),
    ]
    view = build_deal_terms_view(claims, filenames={}, company="AcmeCo")
    assert [t.value for t in view.terms] == ["$50.00M"]


def test_untrusted_dropped_conflicted_surfaced():
    # A proposed (untrusted) claim is dropped; a conflicted one is surfaced WITH
    # its true status (the analysis pages show conflicted/inconclusive).
    claims = [
        _claim(attribute_raw="Investment Amount", normalized=10_000_000, status="proposed"),
        _claim(
            attribute_raw="Ownership",
            normalized=16.7,
            value_type="percent",
            unit="%",
            status="conflicted",
        ),
    ]
    view = build_deal_terms_view(claims, filenames={}, company="AcmeCo")
    assert _labels(view) == ["Ownership Stake"]
    assert _by_label(view, "Ownership Stake").status == "conflicted"


def test_terms_output_in_reading_order():
    # The output follows the deal-summary reading order, not input order.
    claims = [
        _claim(attribute_raw="Ownership", normalized=16.7, value_type="percent", unit="%"),
        _claim(attribute_raw="Investment Amount", normalized=10_000_000),
        _claim(attribute_raw="Post-Money Valuation", normalized=50_000_000),
        _claim(attribute_raw="Pre-Money Valuation", normalized=40_000_000),
    ]
    view = build_deal_terms_view(claims, filenames={}, company="AcmeCo")
    assert _labels(view) == [
        "Investment Amount",
        "Pre-Money Valuation",
        "Post-Money Valuation",
        "Ownership Stake",
    ]


def test_qualitative_claims_are_ignored():
    # Deal terms are numeric scalars; a qualitative assertion never surfaces here.
    claims = [
        _claim(
            attribute_raw="board seat",
            raw="The investor is entitled to one board seat.",
            value_type="text",
            normalized=None,
            claim_kind="qualitative",
            assertion_class="commercial_terms",
        ),
    ]
    view = build_deal_terms_view(claims, filenames={}, company="AcmeCo")
    assert view.terms == []


def test_share_count_option_pool_and_liquidation_preference_formatting():
    claims = [
        _claim(attribute_raw="Fully Diluted Shares", normalized=8_000_000, value_type="count"),
        _claim(attribute_raw="Price per Share", normalized=2.5),
        _claim(attribute_raw="Option Pool", normalized=10.0, value_type="percent", unit="%"),
        _claim(attribute_raw="Liquidation Preference", normalized=1.0, value_type="ratio"),
    ]
    view = build_deal_terms_view(claims, filenames={}, company="AcmeCo")
    got = {t.label: t.value for t in view.terms}
    assert got == {
        "Price per Share": "$2.5",
        "Fully Diluted Shares": "8,000,000",
        "Option Pool": "10%",
        "Liquidation Preference": "1×",
    }


def test_citation_string_and_web_source_url():
    ds = uuid.uuid4()
    web = uuid.uuid4()
    claims = [
        _claim(
            attribute_raw="Pre-Money Valuation", normalized=40_000_000, page=7, data_source_id=ds
        ),
        _claim(
            attribute_raw="Investment Amount",
            normalized=10_000_000,
            entity="AcmeCo",
            kind="web",
            page=None,
            status="cited",
            data_source_id=web,
        ),
    ]
    view = build_deal_terms_view(
        claims,
        filenames={ds: "termsheet.pdf"},
        source_urls={web: "https://example.com/round"},
        company="AcmeCo",
    )
    pre = _by_label(view, "Pre-Money Valuation")
    assert pre.citation == "termsheet.pdf · p.7"
    assert pre.source_url is None
    inv = _by_label(view, "Investment Amount")
    assert inv.source_url == "https://example.com/round"
