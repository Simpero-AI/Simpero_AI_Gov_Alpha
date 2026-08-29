"""Screening materials -- the compact, cited snapshot that feeds the Initial
Screening tab's three panels (Extracted from Materials, Agent Highlights, Risk
Flags) from the deal's claims spine.

The claims table is the ground truth: one row per extracted fact, each carrying
its canonical attribute, its raw label, its unit-normalized value, its period,
its trust status, and where in which document it came from. This module curates
that into a screening-sized view -- for each headline metric of the deal's lead
business subject, the latest actual figure, formatted and cited. A headline
metric is either a genuine canonical attribute or, for a table-dense CIM whose
statement cells the parser left in a catch-all bucket, a line item recovered
from its raw label (see _headline_key).

The curation mirrors the Pipeline Inspector's own "Financial trends" logic
(app/api/templates/pipeline_inspector.html) so the two surfaces agree on what a
canonical metric is, which subject a fact belongs to, and how a value is
formatted -- this is the JSON, screening-sized cut of the same data the
inspector renders as a diagnostic HTML page.

Accuracy contract: values are copied verbatim from the claims (formatted, never
re-derived), only trust-earned statuses are shown, and a fact is shown only when
it actually resolved to a displayable value. `highlights` and `risk_flags` are
left empty here -- deriving positive-vs-risk from quantitative facts is a
judgment call handled by a separate LLM pass, not invented deterministically.
"""

import math
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.models.claim import Claim

# The two E2 catch-all buckets the parser assigns when a fact does not map to a
# real canonical metric; everything else is a genuine canonical attribute.
_CATCHALL = frozenset({"operating_metric", "core_unmapped"})

# Trust-earned statuses -- the only ones shown on a decision surface (matches
# app/services/screening/claims_lookup.py and the inspector's isTrusted).
_TRUSTED = frozenset({"verified", "partially_verified", "cited"})

# Higher wins when two claims describe the same metric/period (pick the most
# corroborated). Mirrors the inspector's STATUS_RANK.
_STATUS_RANK = {
    "conflicted": 0,
    "inconclusive": 1,
    "rejected": 2,
    "proposed": 3,
    "missing": 4,
    "cited": 5,
    "partially_verified": 6,
    "verified": 7,
}

# Sector-neutral income-statement-then-balance-sheet fallback order, used for any
# metric the deal's own parser-derived metric_order did not rank. Mirrors the
# inspector's CANON_ORDER.
_CANON_ORDER = {
    "revenue": 0,
    "cogs": 1,
    "gross_profit": 2,
    "opex": 3,
    "ebitda": 4,
    "ebitda_margin": 5,
    "ebit": 6,
    "net_income": 7,
    "capex": 8,
    "operating_cash_flow": 9,
    "cash_and_equivalents": 10,
    "total_assets": 11,
    "total_liabilities": 12,
    "total_equity": 13,
    "total_debt": 14,
}

# Headline financial line items the parser leaves in a catch-all bucket
# (operating_metric / core_unmapped) instead of mapping to a canonical metric --
# the common case in table-dense CIMs, where every statement cell is extracted
# verbatim and none resolves to a canonical attribute. Each entry lets such a
# fact still surface on the screening snapshot, matched by a case-insensitive
# substring of its raw label (attribute_raw). Ordered by reading priority;
# keywords are kept mutually non-overlapping so a fact maps to at most one item.
_HEADLINE_LABELS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("headline_net_revenue", "Net Revenue", ("net revenues", "net revenue")),
    ("headline_gross_revenue", "Gross Revenue", ("gross revenues", "gross revenue")),
    ("headline_ebitda", "EBITDA", ("ebitda",)),
    ("headline_operating_income", "Operating Income", ("income from operations",)),
    ("headline_net_income", "Net Income", ("net income",)),
    ("headline_total_costs", "Total Costs & Expenses", ("total costs and expenses",)),
    ("headline_sga", "SG&A", ("selling, general and administrative",)),
    ("headline_dna", "D&A", ("depreciation and amortization",)),
)

# Headline items sort after every canonical metric (which rank well under 100 --
# see _metric_rank), preserving their own reading order among themselves.
_HEADLINE_RANK = {key: 1000 + i for i, (key, _label, _keywords) in enumerate(_HEADLINE_LABELS)}

_UNIT_SYMBOL = {"USD": "$", "%": "%"}
_PERIOD_KIND = {"A": "Actual", "E": "Estimate", "P": "Projected"}


@dataclass(frozen=True)
class CitedField:
    label: str
    value: str
    citation: str | None


@dataclass(frozen=True)
class ScreeningMaterials:
    extracted_fields: list[CitedField]
    highlights: list[str]
    risk_flags: list[str]


def _is_canonical(attribute: str | None) -> bool:
    return bool(attribute) and attribute not in _CATCHALL


def _fmt_num(n: float) -> str:
    """Whole numbers without a decimal tail, otherwise up to two decimals, always
    comma-grouped -- e.g. 1200 -> "1,200", 4.25 -> "4.25", 4.0 -> "4"."""
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.2f}".rstrip("0").rstrip(".")


def _fmt_value(value: Any) -> str:
    """Format a claim's value dict for display, mirroring the inspector's
    fmtValue: currency scales to K/M/B with its unit symbol, percent/ratio read
    naturally, everything else falls back to the verbatim `raw`."""
    if not isinstance(value, dict):
        return "—"
    raw = str(value["raw"]) if value.get("raw") is not None else ""
    n = value.get("normalized")
    unit = value.get("unit") or ""
    value_type = value.get("value_type")
    if isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n):
        if value_type == "currency":
            a = abs(n)
            if a >= 1e9:
                scaled = f"{n / 1e9:.2f}B"
            elif a >= 1e6:
                scaled = f"{n / 1e6:.2f}M"
            elif a >= 1e3:
                scaled = f"{n / 1e3:.1f}K"
            else:
                scaled = _fmt_num(n)
            return _UNIT_SYMBOL.get(unit, "") + scaled
        if value_type == "percent":
            return f"{_fmt_num(n)}%"
        if value_type == "ratio":
            return _fmt_num(n) + (f" {unit}" if unit and unit != "ratio" else "×")
        return _fmt_num(n) + (f" {unit}" if unit and unit != "count" else "")
    return raw or "—"


def _human_attr(attribute: str | None) -> str:
    if not attribute:
        return "Value"
    return attribute.replace("_", " ").title()


def _fmt_period(period_year: int | None, period_kind: str | None) -> str:
    if period_year is None:
        return ""
    kind = _PERIOD_KIND.get(period_kind or "")
    return f"FY{period_year}" + (f" {kind}" if kind and kind != "Actual" else "")


def _field_label(claim: Claim, label: str | None = None) -> str:
    """ "<Metric> · FY<year>" -- the period is dropped when the claim carries no
    year (a period-less fact reads as just its metric name). `label` overrides
    the metric name for a headline line item recovered from a catch-all bucket,
    whose canonical attribute would otherwise read as "Operating Metric"."""
    metric = label or _human_attr(claim.attribute)
    period = _fmt_period(claim.period_year, claim.period_kind)
    return f"{metric} · {period}" if period else metric


def _where_text(claim: Claim) -> str | None:
    if claim.kind == "xlsx":
        return f"Sheet {claim.sheet or '?'} · cell {claim.cell_ref or '?'}"
    if claim.kind == "docx":
        return f"Paragraph {claim.paragraph if claim.paragraph is not None else '?'}"
    if claim.page is not None:
        return f"p.{claim.page}"
    return None


def _citation(claim: Claim, filenames: Mapping[uuid.UUID, str]) -> str | None:
    parts: list[str] = []
    if claim.data_source_id is not None:
        name = filenames.get(claim.data_source_id)
        if name:
            parts.append(name)
    where = _where_text(claim)
    if where:
        parts.append(where)
    return " · ".join(parts) or None


def _subject_map(
    dashboard_structure: dict[str, Any] | None, claims: Sequence[Claim]
) -> tuple[dict[str, str], list[str]]:
    """Fold entity strings into business subjects. The parser's grounded
    organizing pass leads when present; otherwise the most-mentioned entities
    become their own subjects (frequency fallback). Mirrors the inspector."""
    entity_subject: dict[str, str] = {}
    order: list[str] = []
    subjects = (dashboard_structure or {}).get("subjects")
    if isinstance(subjects, list) and subjects:
        for subject in subjects:
            if not isinstance(subject, dict) or not subject.get("name"):
                continue
            name = str(subject["name"])
            order.append(name)
            for entity in subject.get("entities") or []:
                if entity and entity not in entity_subject:
                    entity_subject[entity] = name
    else:
        freq = Counter(c.entity for c in claims if c.entity)
        for entity, _count in sorted(
            ((e, f) for e, f in freq.items() if f >= 2),
            key=lambda item: (-item[1], item[0]),
        ):
            entity_subject[entity] = entity
            order.append(entity)
    order.append("Other")
    return entity_subject, order


def _metric_rank(dashboard_structure: dict[str, Any] | None) -> dict[str, int]:
    """The deal's own parser-derived metric_order leads (which metrics matter
    most for THIS company); the canonical order fills in the rest."""
    rank: dict[str, int] = {}
    index = 0
    metric_order = (dashboard_structure or {}).get("metric_order")
    if isinstance(metric_order, list):
        for metric in metric_order:
            if metric and metric not in rank:
                rank[metric] = index
                index += 1
    for metric in sorted(_CANON_ORDER, key=lambda m: _CANON_ORDER[m]):
        if metric not in rank:
            rank[metric] = index
            index += 1
    return rank


def _headline_key(claim: Claim) -> tuple[str, str] | None:
    """The metric a claim contributes to the screening snapshot, as
    (grouping key, display label) -- or None when the claim is not a headline
    fact and should be skipped.

    A genuine canonical attribute keys on itself. A fact the parser left in a
    catch-all bucket keys on the first headline line item its raw label matches,
    so a table-dense CIM's verbatim statement cells still surface; everything
    else in those buckets is skipped, keeping the snapshot a curated headline
    set rather than the raw extract.
    """
    attribute = claim.attribute
    if _is_canonical(attribute):
        return attribute, _human_attr(attribute)
    raw = (claim.attribute_raw or "").lower()
    if not raw:
        return None
    for key, label, keywords in _HEADLINE_LABELS:
        if any(keyword in raw for keyword in keywords):
            return key, label
    return None


def _rank_for(metric_key: str, canonical_rank: dict[str, int]) -> int:
    """Sort rank for a metric key: canonical metrics by the deal's own order,
    headline line items after them in reading order."""
    if metric_key in _HEADLINE_RANK:
        return _HEADLINE_RANK[metric_key]
    return canonical_rank.get(metric_key, 99)


def build_screening_materials(
    claims: Sequence[Claim],
    *,
    dashboard_structure: dict[str, Any] | None,
    filenames: Mapping[uuid.UUID, str],
    limit: int = 12,
) -> ScreeningMaterials:
    """Curate the deal's claims into the screening snapshot.

    Shows, for the lead business subject, each headline metric's latest actual
    figure (preferring an Actual period over an Estimate/Projection, then the
    most recent year, then the most corroborated status), ranked by the deal's
    own metric order, capped at `limit`. A metric is either a genuine canonical
    attribute or a headline line item recovered from a catch-all bucket by its
    raw label (see _headline_key) -- so a table-dense CIM whose statement cells
    never resolve to a canonical attribute still populates the panel.
    `filenames` maps data_source_id -> the document's filename for the citation.
    """
    entity_subject, subject_order = _subject_map(dashboard_structure, claims)
    lead_subject = subject_order[0] if subject_order else "Other"

    def subject_of(claim: Claim) -> str:
        return entity_subject.get(claim.entity or "", "Other")

    # Best claim per headline metric within the lead subject. Keyed on the
    # metric key from _headline_key, not claim.attribute -- catch-all facts all
    # share one attribute ("operating_metric"), so keying on it would collapse
    # every recovered line item onto a single row.
    best: dict[str, tuple[Claim, str]] = {}
    for claim in claims:
        if claim.status not in _TRUSTED:
            continue
        if subject_of(claim) != lead_subject:
            continue
        if _fmt_value(claim.value) == "—":
            continue
        keyed = _headline_key(claim)
        if keyed is None:
            continue
        metric_key, label = keyed
        current = best.get(metric_key)
        if current is None or _prefer(claim, current[0]):
            best[metric_key] = (claim, label)

    canonical_rank = _metric_rank(dashboard_structure)
    chosen = sorted(best.items(), key=lambda item: _rank_for(item[0], canonical_rank))[:limit]

    extracted_fields = [
        CitedField(
            label=_field_label(claim, label),
            value=_fmt_value(claim.value),
            citation=_citation(claim, filenames),
        )
        for _key, (claim, label) in chosen
    ]

    # highlights / risk_flags are intentionally left empty here -- see the module
    # docstring; a separate LLM pass derives them from the same claims.
    return ScreeningMaterials(extracted_fields=extracted_fields, highlights=[], risk_flags=[])


def render_claim_facts(
    claims: Sequence[Claim],
    *,
    dashboard_structure: dict[str, Any] | None,
    limit: int = 100,
) -> list[str]:
    """Human-readable fact lines for the deal's trusted headline claims -- the
    grounding handed to the LLM insights pass (app/services/screening_insights.py).

    Lead business subject only, every headline metric's figures ACROSS its
    periods (so the model can see trends, not just the latest point), ranked by
    the deal's own metric order then latest-year-first, capped at `limit`. Same
    selection, trust filter and value formatting as the extracted panel -- a
    metric is either a canonical attribute or a headline line item recovered
    from a catch-all bucket (see _headline_key) -- so the model reads exactly
    the figures the user sees, and no fact that is not one of them.
    """
    entity_subject, subject_order = _subject_map(dashboard_structure, claims)
    lead_subject = subject_order[0] if subject_order else "Other"
    canonical_rank = _metric_rank(dashboard_structure)

    rows: list[tuple[Claim, str, str]] = []  # (claim, metric_key, display label)
    for claim in claims:
        if claim.status not in _TRUSTED:
            continue
        if entity_subject.get(claim.entity or "", "Other") != lead_subject:
            continue
        if _fmt_value(claim.value) == "—":
            continue
        keyed = _headline_key(claim)
        if keyed is None:
            continue
        rows.append((claim, keyed[0], keyed[1]))
    rows.sort(key=lambda r: (_rank_for(r[1], canonical_rank), -(r[0].period_year or 0)))

    lines: list[str] = []
    seen: set[str] = set()
    for claim, _key, label in rows[:limit]:
        period = _fmt_period(claim.period_year, claim.period_kind)
        value = _fmt_value(claim.value)
        line = f"{label} ({period}): {value}" if period else f"{label}: {value}"
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return lines


def _prefer(candidate: Claim, current: Claim) -> bool:
    """True when `candidate` is the better figure to show for its metric: a
    historical period beats a forecast, then a later year, then a more
    corroborated status."""
    return _rank_key(candidate) > _rank_key(current)


def _rank_key(claim: Claim) -> tuple[int, int, int]:
    # A forecast (Estimate/Projection) ranks below any historical figure; an
    # unmarked period counts as historical, not a forecast -- so a latest actual
    # is never passed over for a later-year estimate even when the actuals carry
    # no explicit "A" kind.
    is_historical = 0 if claim.period_kind in ("E", "P") else 1
    year = claim.period_year if claim.period_year is not None else -1
    return (is_historical, year, _STATUS_RANK.get(claim.status, 0))
