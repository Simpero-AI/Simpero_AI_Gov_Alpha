"""Financials view -- the Financials tab's claims-driven surface.

Same claims-first principle as market_view/company_view/screening_materials: the
claims spine is the ground truth, nothing is invented, and a section with no
backing claims comes back empty so the tab renders "information not available".

This is a re-partition of the SAME curation the screening extracted panel runs.
It reuses screening_materials._headline_claims (the one eligibility gate) with
`include_web=True`, applies the same best-per-key dedup, pinned per-key labels
and _rank_for ordering, then routes each winning metric to one of five statement
sections via _SECTION_BY_METRIC:

- income_statement: the dollar income-statement lines (revenue/COGS/gross
  profit/opex/EBITDA/EBIT/net income/D&A/interest/tax), including every headline
  line item screening recovers from a catch-all bucket (all dollar figures).
- profitability: the margin percentages (gross/EBITDA/net).
- balance_sheet: the balance-sheet stocks (assets, liabilities, equity, cash,
  debt, working-capital components).
- cash_flow: operating cash flow, free cash flow, capex.
- operating: customer concentration, monthly burn, and any metric no section
  claims (the catch-all default).

Accuracy contract is inherited from screening_materials: values are copied
verbatim (formatted by _fmt_value, never re-derived), only trust-earned statuses
show, and a fact appears only when it resolved to a displayable value. The period
is its OWN field (never folded into the label), so the FE renders the metric name
and the period separately.
"""

import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.models.claim import Claim
from app.services.financial_sanity import flag_implausible, individually_implausible
from app.services.screening_materials import (
    _HEADLINE_LABELS,
    _citation,
    _fmt_period,
    _fmt_value,
    _headline_claims,
    _labels_by_key,
    _prefer,
    _rank_for,
    _source_url,
)


@dataclass(frozen=True)
class FinancialFact:
    label: str
    value: str
    period: str
    citation: str | None
    status: str
    entity: str | None
    source_url: str | None = None
    # True when the consistency pass (SIM-372) flagged this figure's arithmetic
    # as inconsistent with its operands (e.g. revenue - cogs != gross_profit).
    # The figure still shows -- an operand, not this line, may be the wrong one,
    # and dropping it would lose data -- but the FE badges it so a
    # non-reconciling statement is visible instead of a confident clean number.
    reconciliation_mismatch: bool = False


def _reconciliation_mismatch(claim: Claim) -> bool:
    """Whether the SIM-372 consistency pass flagged this claim's arithmetic as
    inconsistent. `formula_mismatch` is the flag that pass sets on the derived
    claim when an evaluable accounting identity fails; it is reserved for exactly
    this in the claims contract."""
    return bool(claim.flags and "formula_mismatch" in claim.flags)


@dataclass(frozen=True)
class FinancialsView:
    income_statement: list[FinancialFact]
    profitability: list[FinancialFact]
    balance_sheet: list[FinancialFact]
    cash_flow: list[FinancialFact]
    operating: list[FinancialFact]


@dataclass(frozen=True)
class TrendPoint:
    period: str  # "FY2023" (or "FY2024E") -- _fmt_period carries the actual/est marker
    value: str  # formatted verbatim by _fmt_value, never re-derived
    year: int  # the raw period_year, for x-axis ordering on the FE
    # Per-point provenance, mirroring FinancialFact so the FE can badge a trend
    # figure with the SAME signal the statement rows show: `status` is the claim's
    # rolled-up trust (verified/partially_verified/cited/conflicted/inconclusive),
    # which already reflects the per-year EDGAR corroboration verdict; `citation`
    # is the human "file · p.N" (or web URL). A prior-year revenue that EDGAR
    # confirmed thus reads as `verified` on the trend, not just an un-badged number.
    status: str = "cited"
    citation: str | None = None
    source_url: str | None = None
    reconciliation_mismatch: bool = False


@dataclass(frozen=True)
class FinancialTrendMetric:
    label: str
    points: list[TrendPoint]  # ascending by year; only metrics with >= 2 years appear


@dataclass(frozen=True)
class ProjectionColumn:
    year: int
    kind: str  # "A" (actual) | "E" (management estimate) | "P" (projected)


@dataclass(frozen=True)
class ProjectionCell:
    """Per-cell provenance for one (metric, period) figure in the projections grid,
    aligned by index to ProjectionRow.values. `status` is None for an ABSENT cell
    (its value is None too); otherwise the claim's rolled-up trust status, so the
    grid can badge a corroborated ACTUAL column distinctly from a forward E/P
    projection that no historical registry can confirm."""

    status: str | None
    citation: str | None = None
    source_url: str | None = None
    reconciliation_mismatch: bool = False


@dataclass(frozen=True)
class ProjectionRow:
    label: str
    values: list[str | None]  # aligned to the columns; _fmt_value verbatim, None when absent
    cells: list[ProjectionCell]  # aligned to `values`; ProjectionCell(status=None) where absent


@dataclass(frozen=True)
class FinancialProjections:
    columns: list[ProjectionColumn]
    rows: list[ProjectionRow]


# period_kind severity for choosing a column's overall marker: a year is shown as
# Projected/Estimate if ANY of its figures carry that kind, so a forward column is
# never quietly labelled Actual.
_KIND_SEVERITY = {"A": 0, "E": 1, "P": 2}


# The headline lines a multi-year trend is worth drawing, in display order. The
# same canonical metric keys build_financials_view uses; a metric appears only
# when the deal actually reports it across two or more periods.
_TREND_METRICS: tuple[str, ...] = (
    "revenue",
    "gross_profit",
    "ebitda",
    "ebit",
    "net_income",
    "gross_margin",
    "ebitda_margin",
    "net_margin",
)
_TREND_METRIC_SET = frozenset(_TREND_METRICS)
_TREND_MIN_POINTS = 2  # a single period is a figure, not a trend
_TREND_MAX_YEARS = 5  # most-recent N years, so an old outlier can't stretch the axis


# Section names, in tab reading order -- the five FinancialsView lists.
_SECTIONS: tuple[str, ...] = (
    "income_statement",
    "profitability",
    "balance_sheet",
    "cash_flow",
    "operating",
)

# Which section each metric key feeds. The income-statement set carries the
# canonical dollar lines PLUS every _HEADLINE_LABELS recovered key (read straight
# from screening_materials so the two can't drift): those recovered keys -- gross
# revenue, total costs & expenses, SG&A, D&A -- are all income-statement dollar
# figures the parser left in a catch-all bucket. A metric in NO set defaults to
# `operating` (see build_financials_view), so a newly-canonicalized attribute is
# surfaced somewhere rather than silently dropped.
_SECTION_MEMBERS: dict[str, frozenset[str]] = {
    "income_statement": frozenset(
        {
            "revenue",
            "cogs",
            "gross_profit",
            "opex",
            "ebitda",
            "ebit",
            "net_income",
            "depreciation_and_amortization",
            "interest_expense",
            "tax_expense",
        }
        | {label.key for label in _HEADLINE_LABELS}
    ),
    "profitability": frozenset({"gross_margin", "ebitda_margin", "net_margin"}),
    "balance_sheet": frozenset(
        {
            "total_assets",
            "total_liabilities",
            "total_equity",
            "cash_and_equivalents",
            "total_debt",
            "net_debt",
            "current_assets",
            "current_liabilities",
            "working_capital",
            "accounts_receivable",
            "accounts_payable",
            "inventory",
        }
    ),
    "cash_flow": frozenset({"operating_cash_flow", "free_cash_flow", "capex"}),
    "operating": frozenset({"customer_concentration", "monthly_burn"}),
}

_SECTION_BY_METRIC: dict[str, str] = {
    key: section for section, keys in _SECTION_MEMBERS.items() for key in keys
}

# Intra-section reading order, used ONLY to break ties among metrics that
# _rank_for leaves at the same rank. _rank_for (the deal's own metric_order, then
# _CANON_ORDER, then headline order) still leads; but the many balance-sheet,
# margin, D&A, interest and tax metrics absent from _CANON_ORDER all land at its
# rank-99 fallback, where without a second key they would sort in arbitrary dict
# order. This pins a statement-shaped order for that residual (income statement
# top-to-bottom, then the usual balance-sheet / cash-flow reading order).
_SECTION_ORDER: dict[str, int] = {
    key: i
    for i, key in enumerate(
        (
            # income statement, top to bottom
            "revenue",
            "headline_gross_revenue",
            "cogs",
            "gross_profit",
            "headline_sga",
            "opex",
            "headline_total_costs",
            "depreciation_and_amortization",
            "headline_dna",
            "ebitda",
            "ebit",
            "interest_expense",
            "tax_expense",
            "net_income",
            # profitability
            "gross_margin",
            "ebitda_margin",
            "net_margin",
            # balance sheet
            "cash_and_equivalents",
            "accounts_receivable",
            "inventory",
            "current_assets",
            "total_assets",
            "accounts_payable",
            "current_liabilities",
            "total_debt",
            "net_debt",
            "total_liabilities",
            "working_capital",
            "total_equity",
            # cash flow
            "operating_cash_flow",
            "capex",
            "free_cash_flow",
            # operating
            "customer_concentration",
            "monthly_burn",
        )
    )
}


def build_financials_view(
    claims: Sequence[Claim],
    *,
    filenames: Mapping[uuid.UUID, str],
    source_urls: Mapping[uuid.UUID, str] | None = None,
    dashboard_structure: dict[str, Any] | None = None,
    company: str | None = None,
) -> FinancialsView:
    """Curate the deal's claims into the Financials tab's five statement sections.

    Reuses the screening extracted-panel curation end to end -- the shared
    eligibility gate (with web claims included here), the best-claim-per-metric
    dedup, the pinned per-key labels and the _rank_for order -- then partitions
    the per-metric winners into income statement / profitability / balance sheet /
    cash flow / operating. Only trust-earned claims of the deal's lead business
    subject are shown; a section with none comes back empty.
    """
    rows, canonical_rank = _headline_claims(
        claims,
        dashboard_structure=dashboard_structure,
        company=company,
        include_web=True,
    )

    # Best claim per metric key (same rule as build_screening_materials): keying
    # on the metric, not claim.attribute, both spreads recovered catch-all line
    # items across their own rows and folds a recovered fact onto its canonical
    # twin, so a metric present both ways shows once.
    best: dict[str, Claim] = {}
    for claim, metric_key, _label in rows:
        current = best.get(metric_key)
        if current is None or _prefer(claim, current):
            best[metric_key] = claim

    # Label pinned per key (canonical name when a canonical claim exists, else the
    # recovered headline display), not taken from whichever claim wins the value.
    labels = _labels_by_key(rows)

    # Read-time internal-consistency check: flag any displayed figure that cannot
    # reconcile with its statement-mates (a failed accounting identity, an
    # impossible income-statement ordering, or a magnitude orders of magnitude off
    # -- a scale mis-detection). Grouped by (entity, period_year) so only
    # like-period figures are compared. Deterministic and LLM-free, so it lights up
    # already-stored deals with NO re-analysis; ORed with the verify-time
    # formula_mismatch flag below, never overriding it.
    sanity_groups: dict[tuple[str | None, int | None], dict[str, tuple[float, str]]] = defaultdict(
        dict
    )
    for metric_key, claim in best.items():
        value = claim.value or {}
        normalized = value.get("normalized")
        if isinstance(normalized, (int, float)) and not isinstance(normalized, bool):
            sanity_groups[(claim.entity, claim.period_year)][metric_key] = (
                float(normalized),
                value.get("value_type") or "",
            )
    # `implausible` (all flags) badges a shaky figure; `individually_wrong` (the
    # specific-figure subset -- wrong sign, margin-vs-ratio, magnitude, mislabelled
    # net income) is DROPPED entirely, since the wrong line is known. An accounting
    # IDENTITY/ordering failure is only in `implausible` (ambiguous which operand),
    # so it is badged, not dropped -- dropping all its operands would lose the data.
    implausible: set[tuple[str | None, int | None, str]] = set()
    individually_wrong: set[tuple[str | None, int | None, str]] = set()
    for (entity, period_year), figures in sanity_groups.items():
        for flagged_key in flag_implausible(figures):
            implausible.add((entity, period_year, flagged_key))
        for wrong_key in individually_implausible(figures):
            individually_wrong.add((entity, period_year, wrong_key))

    partitioned: dict[str, list[tuple[str, Claim]]] = {name: [] for name in _SECTIONS}
    for metric_key, claim in best.items():
        section = _SECTION_BY_METRIC.get(metric_key, "operating")
        partitioned[section].append((metric_key, claim))

    def _sort_key(item: tuple[str, Claim]) -> tuple[int, int]:
        metric_key = item[0]
        return (
            _rank_for(metric_key, canonical_rank),
            _SECTION_ORDER.get(metric_key, len(_SECTION_ORDER)),
        )

    built: dict[str, list[FinancialFact]] = {}
    for name, items in partitioned.items():
        built[name] = [
            FinancialFact(
                label=labels[metric_key],
                value=_fmt_value(claim.value),
                period=_fmt_period(claim.period_year, claim.period_kind),
                citation=_citation(claim, filenames),
                status=claim.status,
                entity=claim.entity,
                source_url=_source_url(claim, source_urls),
                reconciliation_mismatch=(
                    _reconciliation_mismatch(claim)
                    or (claim.entity, claim.period_year, metric_key) in implausible
                ),
            )
            for metric_key, claim in sorted(items, key=_sort_key)
            if (claim.entity, claim.period_year, metric_key) not in individually_wrong
        ]

    # Derive gross margin from gross_profit / revenue when the deal reports no valid
    # extracted one -- none was extracted, or the extracted figure was dropped as
    # individually-wrong (the QA "2.3% gross margin" case). Gross margin IS
    # gross_profit / revenue by definition, so this is a labelled derivation, never a
    # guess; it feeds both the Financials tab AND the Summary KPI tile, which read the
    # SAME profitability section -- so a single, consistent margin replaces the old
    # 71.1% / 2.3% / "Not available" split.
    gm_label = labels.get("gross_margin", "Gross Margin")
    if not any(f.label == gm_label for f in built["profitability"]):
        gp, rev = best.get("gross_profit"), best.get("revenue")
        if (
            gp is not None
            and rev is not None
            and gp.period_year == rev.period_year
            and (gp.entity, gp.period_year, "gross_profit") not in individually_wrong
            and (rev.entity, rev.period_year, "revenue") not in individually_wrong
        ):
            gp_val = (gp.value or {}).get("normalized")
            rev_val = (rev.value or {}).get("normalized")
            if isinstance(gp_val, (int, float)) and isinstance(rev_val, (int, float)) and rev_val:
                both_verified = gp.status == "verified" and rev.status == "verified"
                built["profitability"].append(
                    FinancialFact(
                        label="Gross Margin",
                        value=f"{gp_val / rev_val * 100:.1f}%",
                        period=_fmt_period(rev.period_year, rev.period_kind),
                        citation="Derived from Revenue and Gross Profit",
                        status="verified" if both_verified else "cited",
                        entity=rev.entity,
                        source_url=None,
                    )
                )

    return FinancialsView(
        income_statement=built["income_statement"],
        profitability=built["profitability"],
        balance_sheet=built["balance_sheet"],
        cash_flow=built["cash_flow"],
        operating=built["operating"],
    )


def build_financials_trend(
    claims: Sequence[Claim],
    *,
    filenames: Mapping[uuid.UUID, str] | None = None,
    source_urls: Mapping[uuid.UUID, str] | None = None,
    dashboard_structure: dict[str, Any] | None = None,
    company: str | None = None,
) -> list[FinancialTrendMetric]:
    """A multi-year series per headline P&L metric, from the SAME claims spine the
    statement sections use -- so the "3-Year Financial Trend" is real, not the
    unwritten memo_json it read before.

    Reuses the shared eligibility gate (_headline_claims: trust-earned, lead-
    subject-scoped, web included), but aggregates the BEST claim per (metric,
    period_year) instead of best-per-metric, so each metric becomes a per-year
    series. Values are copied verbatim (_fmt_value) and never re-derived. A metric
    appears only with >= 2 distinct years; the most-recent _TREND_MAX_YEARS are
    kept. An estimate/projection year is included and labelled by _fmt_period
    (FY2024E), never silently mixed in as an actual."""
    rows, _canonical_rank = _headline_claims(
        claims,
        dashboard_structure=dashboard_structure,
        company=company,
        include_web=True,
    )
    labels = _labels_by_key(rows)

    # Best claim per (metric, year) -- same _prefer rule as the statement sections,
    # applied within each year rather than across all of a metric's periods.
    best_by_year: dict[tuple[str, int], Claim] = {}
    for claim, metric_key, _label in rows:
        if metric_key not in _TREND_METRIC_SET or claim.period_year is None:
            continue
        key = (metric_key, claim.period_year)
        current = best_by_year.get(key)
        if current is None or _prefer(claim, current):
            best_by_year[key] = claim

    series: dict[str, list[tuple[int, Claim]]] = {}
    for (metric_key, year), claim in best_by_year.items():
        series.setdefault(metric_key, []).append((year, claim))

    trend: list[FinancialTrendMetric] = []
    for metric_key in _TREND_METRICS:  # pinned display order
        points = sorted(series.get(metric_key, []), key=lambda yc: yc[0])
        if len(points) < _TREND_MIN_POINTS:
            continue
        points = points[-_TREND_MAX_YEARS:]
        trend.append(
            FinancialTrendMetric(
                label=labels.get(metric_key, metric_key),
                points=[
                    TrendPoint(
                        period=_fmt_period(claim.period_year, claim.period_kind),
                        value=_fmt_value(claim.value),
                        year=year,
                        status=claim.status,
                        citation=_citation(claim, filenames or {}),
                        source_url=_source_url(claim, source_urls),
                        reconciliation_mismatch=_reconciliation_mismatch(claim),
                    )
                    for year, claim in points
                ],
            )
        )
    return trend


def build_financials_projections(
    claims: Sequence[Claim],
    *,
    filenames: Mapping[uuid.UUID, str] | None = None,
    source_urls: Mapping[uuid.UUID, str] | None = None,
    dashboard_structure: dict[str, Any] | None = None,
    company: str | None = None,
) -> FinancialProjections | None:
    """The Financial Projections grid -- year-by-year actuals, management estimates
    and projections -- from the SAME claims spine the statement sections and the
    3-Year Trend use, replacing the unwritten memo_json the FE card read before.

    Metrics are rows (income statement -> profitability -> balance sheet -> cash flow
    -> operating, via _SECTION_ORDER); periods are columns, each marked Actual /
    Estimate / Projected from the claims' period_kind (a column is marked forward if
    ANY of its figures are). Values are copied verbatim (_fmt_value), never
    re-derived or modelled -- an absent (metric, period) cell is None, not an
    interpolation. Returns None when the deal reports fewer than two periods: a single
    period is the headline figure set (already shown above), not a projection grid."""
    rows, _canonical_rank = _headline_claims(
        claims,
        dashboard_structure=dashboard_structure,
        company=company,
        include_web=True,
    )
    labels = _labels_by_key(rows)

    # Best claim per (metric, year) -- the same _prefer rule the trend uses, so the
    # grid and the trend never disagree on which figure a cell shows.
    best: dict[tuple[str, int], Claim] = {}
    for claim, metric_key, _label in rows:
        if claim.period_year is None or metric_key not in _SECTION_ORDER:
            continue
        key = (metric_key, claim.period_year)
        current = best.get(key)
        if current is None or _prefer(claim, current):
            best[key] = claim
    if not best:
        return None

    years = sorted({year for _metric, year in best})
    if len(years) < 2:
        return None

    # A column's kind is the most-forward period_kind of any figure in that year, so a
    # year carrying a projected figure is never mislabelled Actual.
    col_kind: dict[int, str] = {}
    for (_metric, year), claim in best.items():
        kind = claim.period_kind if claim.period_kind in _KIND_SEVERITY else "A"
        if year not in col_kind or _KIND_SEVERITY[kind] > _KIND_SEVERITY[col_kind[year]]:
            col_kind[year] = kind
    columns = [ProjectionColumn(year=year, kind=col_kind[year]) for year in years]

    by_metric: dict[str, dict[int, Claim]] = defaultdict(dict)
    for (metric_key, year), claim in best.items():
        by_metric[metric_key][year] = claim

    projection_rows = [
        ProjectionRow(
            label=labels.get(metric_key, metric_key),
            values=[
                _fmt_value(by_metric[metric_key][year].value)
                if year in by_metric[metric_key]
                else None
                for year in years
            ],
            # Per-cell provenance aligned to `values`, so the FE badges each figure
            # with its trust status (a corroborated actual vs an un-confirmable
            # forward projection). An absent (metric, year) cell carries status=None.
            cells=[
                ProjectionCell(
                    status=by_metric[metric_key][year].status,
                    citation=_citation(by_metric[metric_key][year], filenames or {}),
                    source_url=_source_url(by_metric[metric_key][year], source_urls),
                    reconciliation_mismatch=_reconciliation_mismatch(by_metric[metric_key][year]),
                )
                if year in by_metric[metric_key]
                else ProjectionCell(status=None)
                for year in years
            ],
        )
        for metric_key in sorted(
            by_metric, key=lambda m: _SECTION_ORDER.get(m, len(_SECTION_ORDER))
        )
    ]
    return FinancialProjections(columns=columns, rows=projection_rows)
