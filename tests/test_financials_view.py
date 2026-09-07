"""Unit tests for build_financials_view -- pure curation over in-memory Claim
objects, no database. Guards how the deal's trusted headline metrics are
partitioned across the Financials tab's five statement sections: canonical lines
and catch-all-recovered dollar figures into the income statement, margins into
profitability, balance-sheet stocks into their section, cash-flow lines into
theirs, and the latest-actual figure winning a single row per metric."""

import uuid

from app.models.claim import Claim
from app.services.financials_view import build_financials_view


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


def test_canonical_revenue_lands_in_income_statement():
    claims = [
        _claim(attribute="revenue", normalized=497_200_000, period_year=2023, period_kind="A"),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    (fact,) = view.income_statement
    assert fact.label == "Revenue"
    assert fact.value == "$497.20M"  # pre-formatted server-side, rendered verbatim
    assert fact.period == "FY2023"  # its OWN field, never folded into the label
    assert fact.citation == "p.1"
    # No other section is populated by a lone income-statement line.
    assert view.profitability == []
    assert view.balance_sheet == []
    assert view.cash_flow == []
    assert view.operating == []


def test_catchall_ebitda_is_recovered_into_income_statement():
    # A table-dense CIM leaves EBITDA in the operating_metric catch-all; it is
    # recovered from its raw label onto the income statement, not dropped.
    claims = [
        _claim(attribute="operating_metric", attribute_raw="EBITDA", normalized=88_000_000),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    (fact,) = view.income_statement
    assert fact.label == "EBITDA"
    assert fact.value == "$88.00M"
    assert fact.period == ""  # undated CIM figure -> empty period, not dropped


def test_percent_margin_lands_in_profitability():
    claims = [
        _claim(attribute="ebitda_margin", normalized=42.0, value_type="percent", period_year=2023),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    assert view.income_statement == []
    (fact,) = view.profitability
    assert fact.label == "Ebitda Margin"
    assert fact.value == "42%"
    assert fact.period == "FY2023"


def test_balance_sheet_metrics_land_in_balance_sheet_in_canonical_order():
    claims = [
        _claim(attribute="total_assets", normalized=1_200_000_000, period_year=2023),
        _claim(attribute="cash_and_equivalents", normalized=150_000_000, period_year=2023),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    assert view.income_statement == []
    # Cash (canonical rank 10) sorts before Total Assets (rank 11).
    assert [(f.label, f.value) for f in view.balance_sheet] == [
        ("Cash And Equivalents", "$150.00M"),
        ("Total Assets", "$1.20B"),
    ]


def test_cash_flow_metrics_land_in_cash_flow():
    claims = [
        _claim(attribute="operating_cash_flow", normalized=60_000_000, period_year=2023),
        _claim(attribute="free_cash_flow", normalized=35_000_000, period_year=2023),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    assert {f.label for f in view.cash_flow} == {"Operating Cash Flow", "Free Cash Flow"}
    assert view.income_statement == []


def test_a_canonical_operating_metric_lands_in_operating():
    # A genuine canonical operating metric surfaces in the operating section.
    claims = [
        _claim(
            attribute="customer_concentration",
            normalized=0.35,
            value_type="ratio",
            period_year=2023,
        ),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    assert [f.label for f in view.operating] == ["Customer Concentration"]
    assert view.income_statement == []


def test_a_noncanonical_raw_label_metric_is_not_surfaced():
    # A raw-label attribute the parser never canonicalized -- fiscal metadata, a
    # Forbes-list year, a customer-count definition -- is NOT a financial metric.
    # It falls through the dollar-line recovery (which rejects non-dollar values)
    # and surfaces nowhere. The old "not a catch-all == canonical" rule dumped
    # exactly these into the operating section.
    claims = [
        _claim(
            attribute="operating_metric",
            attribute_raw="Forbes List Year Used",
            normalized=2022,
            value_type="count",
        ),
        _claim(
            attribute="operating_metric",
            attribute_raw="Fiscal Period Covered",
            normalized=2024,
            value_type="count",
        ),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    assert view.operating == []
    assert view.income_statement == []
    assert view.balance_sheet == []
    assert view.cash_flow == []
    assert view.profitability == []


def test_latest_period_wins_one_row_per_metric():
    # Two revenue figures for different years -> a single Revenue row, the latest.
    claims = [
        _claim(attribute="revenue", normalized=400_000_000, period_year=2022),
        _claim(attribute="revenue", normalized=497_200_000, period_year=2023),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    assert [(f.label, f.value, f.period) for f in view.income_statement] == [
        ("Revenue", "$497.20M", "FY2023"),
    ]


def test_a_competitors_figure_is_dropped_by_the_lead_subject_filter():
    # A named competitor's revenue must not surface as the deal's own figure.
    claims = [
        _claim(attribute="revenue", normalized=100_000_000, entity="AcmeCo", period_year=2023),
        _claim(attribute="revenue", normalized=900_000_000, entity="Rival Corp", period_year=2023),
    ]
    structure = {
        "subjects": [
            {"name": "AcmeCo", "entities": ["AcmeCo"]},
            {"name": "Rival Corp", "entities": ["Rival Corp"]},
        ]
    }

    view = build_financials_view(
        claims, filenames={}, dashboard_structure=structure, company="AcmeCo"
    )

    assert [(f.label, f.value) for f in view.income_statement] == [("Revenue", "$100.00M")]


def test_web_claim_surfaces_source_url_but_a_dated_deck_beats_it_for_the_same_metric():
    # A web-collected figure (include_web=True on the Financials tab) reaches the
    # view carrying its source URL. For a metric where a DATED deck figure also
    # exists, the deck (a later, internal figure) wins the single row; the web
    # claim's URL surfaces on a metric the deck does not report.
    web_rev = uuid.uuid4()
    web_ebitda = uuid.uuid4()
    deck = uuid.uuid4()
    claims = [
        # Web revenue, undated -> beaten by the dated deck revenue below.
        _claim(
            attribute="operating_metric",
            attribute_raw="Net Revenue",
            normalized=90_000_000,
            kind="web",
            status="cited",
            page=None,
            period_year=None,
            data_source_id=web_rev,
        ),
        # Dated deck revenue for the SAME metric -> wins the single Revenue row.
        _claim(
            attribute="revenue",
            normalized=120_000_000,
            period_year=2023,
            data_source_id=deck,
        ),
        # Web EBITDA, uncontested -> surfaces with its source_url.
        _claim(
            attribute="ebitda",
            normalized=30_000_000,
            kind="web",
            status="cited",
            page=None,
            data_source_id=web_ebitda,
        ),
    ]

    view = build_financials_view(
        claims,
        filenames={web_rev: "Web A", web_ebitda: "Web B", deck: "CIM.pdf"},
        source_urls={
            web_rev: "https://example.com/rev",
            web_ebitda: "https://example.com/ebitda",
        },
        company="AcmeCo",
    )

    inc = {f.label: f for f in view.income_statement}
    # Revenue: the dated deck beat the undated web figure -> deck value, no URL.
    assert inc["Revenue"].value == "$120.00M"
    assert inc["Revenue"].period == "FY2023"
    assert inc["Revenue"].source_url is None
    # EBITDA: the uncontested web claim surfaces its source_url.
    assert inc["Ebitda"].value == "$30.00M"
    assert inc["Ebitda"].source_url == "https://example.com/ebitda"


def test_empty_deal_yields_five_empty_sections():
    view = build_financials_view([], filenames={})
    assert view.income_statement == []
    assert view.profitability == []
    assert view.balance_sheet == []
    assert view.cash_flow == []
    assert view.operating == []
