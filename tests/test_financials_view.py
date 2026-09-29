"""Unit tests for build_financials_view -- pure curation over in-memory Claim
objects, no database. Guards how the deal's trusted headline metrics are
partitioned across the Financials tab's five statement sections: canonical lines
and catch-all-recovered dollar figures into the income statement, margins into
profitability, balance-sheet stocks into their section, cash-flow lines into
theirs, and the latest-actual figure winning a single row per metric."""

import uuid

from app.models.claim import Claim
from app.services.financials_view import (
    FinancialFact,
    build_financials_projections,
    build_financials_trend,
    build_financials_view,
)


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
    flags: list[str] | None = None,
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
        flags=flags,
    )


def test_formula_mismatch_flag_surfaces_as_reconciliation_mismatch():
    """A figure the consistency pass flagged (formula_mismatch) still shows -- an
    operand may be the wrong one, not this line -- but is marked so the FE can
    badge a non-reconciling statement instead of a confident clean number."""
    # Internally-consistent figures (gross_profit < revenue, same scale) so the
    # read-time check stays silent and this isolates the stored-flag plumbing.
    claims = [
        _claim(
            attribute="gross_profit",
            normalized=200_000_000,
            period_year=2023,
            period_kind="A",
            flags=["formula_mismatch"],
        ),
        _claim(attribute="revenue", normalized=497_200_000, period_year=2023, period_kind="A"),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    facts = {f.label: f for f in view.income_statement}
    assert facts["Gross Profit"].reconciliation_mismatch is True
    # An unflagged, consistent line is not marked.
    assert facts["Revenue"].reconciliation_mismatch is False


def _all_facts(view) -> dict[str, FinancialFact]:
    facts: dict[str, FinancialFact] = {}
    for section in (
        view.income_statement,
        view.profitability,
        view.balance_sheet,
        view.cash_flow,
        view.operating,
    ):
        facts.update({f.label: f for f in section})
    return facts


def test_financials_view_drops_an_individually_wrong_figure():
    # A negative inventory is a cash-flow delta mis-canonicalised onto the stock --
    # the wrong line is KNOWN, so the tab DROPS it rather than badge it; the valid
    # figure alongside it stays.
    claims = [
        _claim(
            attribute="inventory", normalized=-11_324_000_000, period_year=2026, period_kind="A"
        ),
        _claim(
            attribute="total_assets", normalized=206_800_000_000, period_year=2026, period_kind="A"
        ),
    ]

    facts = _all_facts(build_financials_view(claims, filenames={}, company="AcmeCo"))

    assert "Inventory" not in facts  # individually-wrong figure dropped
    assert "Total Assets" in facts


def test_financials_view_badges_but_keeps_an_ambiguous_identity_failure():
    # Assets != Liabilities + Equity: which operand is wrong is ambiguous, so all
    # three are BADGED and still shown -- dropping them would lose the statement.
    claims = [
        _claim(
            attribute="total_assets", normalized=206_800_000_000, period_year=2026, period_kind="A"
        ),
        _claim(
            attribute="total_liabilities",
            normalized=49_510_000_000,
            period_year=2026,
            period_kind="A",
        ),
        _claim(
            attribute="total_equity", normalized=206_800_000_000, period_year=2026, period_kind="A"
        ),
    ]

    facts = _all_facts(build_financials_view(claims, filenames={}, company="AcmeCo"))

    assert {"Total Assets", "Total Liabilities", "Total Equity"} <= set(facts)
    assert facts["Total Assets"].reconciliation_mismatch is True
    assert facts["Total Equity"].reconciliation_mismatch is True


def test_gross_margin_is_derived_when_no_valid_extracted_one_exists():
    # No gross_margin claim, but GP + revenue present -> a derived Gross Margin
    # (GP / revenue) fills the profitability section so the Financials tab and the
    # Summary KPI tile both show a consistent figure instead of "Not available".
    claims = [
        _claim(
            attribute="gross_profit",
            normalized=153_000_000_000,
            period_year=2026,
            period_kind="A",
        ),
        _claim(attribute="revenue", normalized=216_000_000_000, period_year=2026, period_kind="A"),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    gm = next(f for f in view.profitability if f.label == "Gross Margin")
    assert gm.value == "70.8%"
    assert gm.citation == "Derived from Revenue and Gross Profit"


def test_a_valid_extracted_gross_margin_is_not_replaced_by_a_derived_one():
    claims = [
        _claim(
            attribute="gross_margin",
            normalized=0.71,
            value_type="percent",
            period_year=2026,
            period_kind="A",
        ),
        _claim(
            attribute="gross_profit",
            normalized=153_000_000_000,
            period_year=2026,
            period_kind="A",
        ),
        _claim(attribute="revenue", normalized=216_000_000_000, period_year=2026, period_kind="A"),
    ]

    view = build_financials_view(claims, filenames={}, company="AcmeCo")

    gms = [f for f in view.profitability if f.label == "Gross Margin"]
    assert len(gms) == 1
    assert gms[0].citation != "Derived from Revenue and Gross Profit"


def test_read_time_sanity_flags_a_mis_scaled_figure_without_a_stored_flag():
    """The deterministic read-time check lights up reconciliation_mismatch on an
    already-stored deal with NO formula_mismatch flag: a balance-sheet total
    mis-scaled to thousands next to a billions revenue is caught at display time
    (no re-analysis)."""
    claims = [
        _claim(attribute="revenue", normalized=416_000_000_000, period_year=2025, period_kind="A"),
        _claim(
            attribute="net_income", normalized=112_000_000_000, period_year=2025, period_kind="A"
        ),
        # Mis-scaled: Apple's total assets are ~$365B, here extracted as 365K.
        _claim(attribute="total_assets", normalized=365_000, period_year=2025, period_kind="A"),
    ]
    facts = _all_facts(build_financials_view(claims, filenames={}, company="AcmeCo"))
    assert facts["Total Assets"].reconciliation_mismatch is True
    assert facts["Revenue"].reconciliation_mismatch is False


def test_read_time_sanity_flags_ebit_above_gross_profit():
    claims = [
        _claim(attribute="revenue", normalized=200_000_000_000, period_year=2025, period_kind="A"),
        _claim(
            attribute="gross_profit", normalized=97_860_000_000, period_year=2025, period_kind="A"
        ),
        _claim(attribute="ebit", normalized=141_450_000_000, period_year=2025, period_kind="A"),
    ]
    facts = _all_facts(build_financials_view(claims, filenames={}, company="AcmeCo"))
    # EBIT (141.45B) cannot exceed Gross Profit (97.86B) -> both flagged.
    ebit = next(f for label, f in facts.items() if label.lower() == "ebit")
    gp = next(f for label, f in facts.items() if label.lower() == "gross profit")
    assert ebit.reconciliation_mismatch is True
    assert gp.reconciliation_mismatch is True


def test_reconciliation_mismatch_defaults_false_without_flags():
    claims = [
        _claim(attribute="revenue", normalized=497_200_000, period_year=2023, period_kind="A"),
    ]
    (fact,) = build_financials_view(claims, filenames={}, company="AcmeCo").income_statement
    assert fact.reconciliation_mismatch is False


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


# --- 3-Year Financial Trend (build_financials_trend) --------------------------


def test_trend_builds_a_multi_year_series_per_metric():
    claims = [
        _claim(attribute="revenue", normalized=400_000_000, period_year=2021, period_kind="A"),
        _claim(attribute="revenue", normalized=450_000_000, period_year=2022, period_kind="A"),
        _claim(attribute="revenue", normalized=497_200_000, period_year=2023, period_kind="A"),
    ]

    trend = build_financials_trend(claims, company="AcmeCo")

    (rev,) = trend
    assert rev.label == "Revenue"
    assert [(p.year, p.value, p.period) for p in rev.points] == [
        (2021, "$400.00M", "FY2021"),
        (2022, "$450.00M", "FY2022"),
        (2023, "$497.20M", "FY2023"),
    ]


def test_trend_excludes_a_metric_with_a_single_year():
    # One period is a figure, not a trend -- it belongs in the statement sections,
    # not the multi-year trend.
    claims = [_claim(attribute="revenue", normalized=497_200_000, period_year=2023)]
    assert build_financials_trend(claims, company="AcmeCo") == []


def test_trend_keeps_only_the_most_recent_years():
    claims = [
        _claim(attribute="revenue", normalized=1_000_000 * y, period_year=y)
        for y in range(2018, 2025)  # 7 years
    ]
    (rev,) = build_financials_trend(claims, company="AcmeCo")
    years = [p.year for p in rev.points]
    assert years == [2020, 2021, 2022, 2023, 2024]  # most-recent 5


def test_trend_picks_the_best_claim_per_year():
    # Two revenue claims for the same year -> the more-trusted one wins that point.
    claims = [
        _claim(attribute="revenue", normalized=400_000_000, period_year=2022, status="cited"),
        _claim(attribute="revenue", normalized=450_000_000, period_year=2022, status="verified"),
        _claim(attribute="revenue", normalized=497_200_000, period_year=2023, status="verified"),
    ]
    (rev,) = build_financials_trend(claims, company="AcmeCo")
    by_year = {p.year: p.value for p in rev.points}
    assert by_year[2022] == "$450.00M"  # verified beat cited for 2022


def test_trend_drops_a_competitors_series():
    # A named competitor's multi-year revenue must not surface as the deal's trend.
    claims = [
        _claim(attribute="revenue", normalized=100_000_000, entity="AcmeCo", period_year=2022),
        _claim(attribute="revenue", normalized=120_000_000, entity="AcmeCo", period_year=2023),
        _claim(attribute="revenue", normalized=900_000_000, entity="Rival Corp", period_year=2022),
        _claim(attribute="revenue", normalized=950_000_000, entity="Rival Corp", period_year=2023),
    ]
    structure = {
        "subjects": [
            {"name": "AcmeCo", "entities": ["AcmeCo"]},
            {"name": "Rival Corp", "entities": ["Rival Corp"]},
        ]
    }

    (rev,) = build_financials_trend(claims, dashboard_structure=structure, company="AcmeCo")

    assert [p.value for p in rev.points] == ["$100.00M", "$120.00M"]


def test_trend_is_empty_for_a_claimless_deal():
    assert build_financials_trend([], company="AcmeCo") == []


def test_trend_points_carry_per_year_trust_status_and_citation():
    # Each trend point mirrors the statement rows' provenance so the FE can badge a
    # corroborated year: a prior-year figure that reached `verified` (its EDGAR
    # corroboration agreed) reads as verified on the trend, not an un-badged number.
    ds = uuid.uuid4()
    claims = [
        _claim(
            attribute="revenue",
            normalized=400_000_000,
            period_year=2022,
            period_kind="A",
            status="cited",
            data_source_id=ds,
        ),
        _claim(
            attribute="revenue",
            normalized=497_200_000,
            period_year=2023,
            period_kind="A",
            status="verified",
            data_source_id=ds,
        ),
    ]

    (rev,) = build_financials_trend(claims, filenames={ds: "CIM.pdf"}, company="AcmeCo")

    by_year = {p.year: p for p in rev.points}
    assert by_year[2022].status == "cited"
    assert by_year[2023].status == "verified"
    assert by_year[2023].citation is not None and "CIM.pdf" in by_year[2023].citation


def test_projections_builds_a_year_by_year_grid_with_kind_markers():
    claims = [
        _claim(attribute="revenue", normalized=400_000_000, period_year=2023, period_kind="A"),
        _claim(attribute="revenue", normalized=465_600_000, period_year=2024, period_kind="E"),
        _claim(attribute="net_income", normalized=50_000_000, period_year=2023, period_kind="A"),
    ]

    proj = build_financials_projections(claims, company="AcmeCo")

    assert proj is not None
    # Columns ascending by year, each marked Actual / Estimate from period_kind.
    assert [(c.year, c.kind) for c in proj.columns] == [(2023, "A"), (2024, "E")]
    rev = next(r for r in proj.rows if r.label == "Revenue")
    assert rev.values == ["$400.00M", "$465.60M"]  # verbatim, never re-derived
    # A metric absent for a period is a None cell -- never interpolated.
    gap_row = next(r for r in proj.rows if None in r.values)
    assert gap_row.values.count(None) == 1


def test_projection_cells_carry_status_aligned_to_values():
    # Each grid cell carries its figure's trust status, aligned by index to `values`,
    # so the FE badges a corroborated actual distinctly from a forward projection; an
    # absent (metric, period) cell carries status=None (its value is None too).
    claims = [
        _claim(
            attribute="revenue",
            normalized=400_000_000,
            period_year=2023,
            period_kind="A",
            status="verified",
        ),
        _claim(
            attribute="revenue",
            normalized=520_000_000,
            period_year=2024,
            period_kind="P",
            status="cited",
        ),
        # net_income only in 2023 -> its 2024 cell is an absent (None) cell.
        _claim(
            attribute="net_income",
            normalized=50_000_000,
            period_year=2023,
            period_kind="A",
            status="verified",
        ),
    ]

    proj = build_financials_projections(claims, company="AcmeCo")

    assert proj is not None
    years = [c.year for c in proj.columns]
    rev = next(r for r in proj.rows if r.label == "Revenue")
    assert len(rev.cells) == len(rev.values) == len(years)
    by_year = dict(zip(years, rev.cells, strict=True))
    assert by_year[2023].status == "verified"
    assert by_year[2024].status == "cited"

    ni = next(r for r in proj.rows if r.label == "Net Income")
    ni_by_year = dict(zip(years, ni.cells, strict=True))
    # The absent 2024 net-income cell is status=None, aligned to a None value.
    assert ni_by_year[2024].status is None
    assert ni.values[years.index(2024)] is None


def test_forward_projection_figure_never_shows_an_external_verdict():
    # Enforced in code, not left to EDGAR's data horizon: an estimate/projected cell
    # can never read `verified`/`conflicted` (no registry corroborates a forecast),
    # even if the figure's claim carries that status (e.g. an FY-est whose year is
    # already filed, or a mis-resolved period_year). The actual column is untouched.
    claims = [
        _claim(
            attribute="revenue",
            normalized=400_000_000,
            period_year=2023,
            period_kind="A",
            status="verified",
        ),
        _claim(
            attribute="revenue",
            normalized=620_000_000,
            period_year=2025,
            period_kind="P",
            status="verified",
        ),
    ]

    proj = build_financials_projections(claims, company="AcmeCo")

    assert proj is not None
    years = [c.year for c in proj.columns]
    rev = next(r for r in proj.rows if r.label == "Revenue")
    by_year = dict(zip(years, rev.cells, strict=True))
    assert by_year[2023].status == "verified"  # actual, untouched
    assert by_year[2025].status == "partially_verified"  # forecast, external verdict stripped


def test_forward_trend_point_never_shows_an_external_verdict():
    claims = [
        _claim(
            attribute="revenue",
            normalized=400_000_000,
            period_year=2024,
            period_kind="A",
            status="verified",
        ),
        _claim(
            attribute="revenue",
            normalized=520_000_000,
            period_year=2026,
            period_kind="E",
            status="verified",
        ),
    ]

    (rev,) = build_financials_trend(claims, company="AcmeCo")

    by_year = {p.year: p.status for p in rev.points}
    assert by_year[2024] == "verified"  # actual, untouched
    assert by_year[2026] == "partially_verified"  # estimate, external verdict stripped


def test_display_status_strips_external_verdicts_only_for_forward_periods():
    # Directly cover _display_status: an external verdict (verified/conflicted) on an
    # E/P period is downgraded to partially_verified; actuals and non-external
    # statuses pass through unchanged.
    from app.services.financials_view import _display_status

    def _s(status: str, period_kind: str | None) -> str:
        return _display_status(_claim(attribute="revenue", period_kind=period_kind, status=status))

    assert _s("verified", "P") == "partially_verified"
    assert _s("conflicted", "E") == "partially_verified"
    assert _s("verified", "A") == "verified"  # actual untouched
    assert _s("verified", None) == "verified"  # unmarked treated as actual
    assert _s("cited", "P") == "cited"  # not an external verdict, unchanged
    assert _s("partially_verified", "E") == "partially_verified"


def test_projections_none_for_a_single_period():
    # One period is the headline figure set (shown above), not a projection grid.
    claims = [
        _claim(attribute="revenue", normalized=497_200_000, period_year=2023, period_kind="A")
    ]
    assert build_financials_projections(claims, company="AcmeCo") is None


def test_projections_none_for_a_claimless_deal():
    assert build_financials_projections([], company="AcmeCo") is None


def test_projections_marks_a_year_forward_if_any_figure_is():
    # A year carrying a projected figure is marked Projected for the whole column,
    # so a forward period is never mislabelled Actual.
    claims = [
        _claim(attribute="revenue", normalized=400_000_000, period_year=2023, period_kind="A"),
        _claim(attribute="revenue", normalized=500_000_000, period_year=2025, period_kind="A"),
        _claim(attribute="ebit", normalized=90_000_000, period_year=2025, period_kind="P"),
    ]

    proj = build_financials_projections(claims, company="AcmeCo")

    assert proj is not None
    kinds = {c.year: c.kind for c in proj.columns}
    assert kinds == {2023: "A", 2025: "P"}
