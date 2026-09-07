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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.models.claim import Claim
from app.services.screening_materials import (
    _HEADLINE_LABELS,
    _citation,
    _fmt_period,
    _fmt_value,
    _headline_claims,
    _labels_by_key,
    _prefer,
    _rank_for,
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


@dataclass(frozen=True)
class FinancialsView:
    income_statement: list[FinancialFact]
    profitability: list[FinancialFact]
    balance_sheet: list[FinancialFact]
    cash_flow: list[FinancialFact]
    operating: list[FinancialFact]


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
                source_url=(source_urls or {}).get(claim.data_source_id),
            )
            for metric_key, claim in sorted(items, key=_sort_key)
        ]

    return FinancialsView(
        income_statement=built["income_statement"],
        profitability=built["profitability"],
        balance_sheet=built["balance_sheet"],
        cash_flow=built["cash_flow"],
        operating=built["operating"],
    )
