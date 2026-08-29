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

Subject folding and value formatting mirror the Pipeline Inspector
(app/api/templates/pipeline_inspector.html) -- this is a JSON, screening-sized
cut of the same claims the inspector renders as a diagnostic page. It is NOT
identical in scope, though: the inspector's "Financial trends" panel is
canonical-only, while this view additionally recovers headline line items from
the catch-all buckets (see _headline_key), so on a table-dense CIM it shows
metrics the inspector's trends panel does not. Bringing the inspector to the same
recovery is a separate follow-up.

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
from app.services.entity_resolution.resolved import normalize_name

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
# verbatim and none resolves to a canonical attribute. Each entry recovers such a
# fact onto the screening snapshot from its raw label. Matching is over
# normalize_name(attribute_raw) (reused from entity resolution), so case,
# punctuation and spacing variants fold together.
#
# `key` is the grouping key: a real canonical attribute where the metric has one,
# so a recovered fact and its canonical twin collapse onto ONE row instead of
# showing as duplicates; otherwise a distinct headline_* key. A fact matches the
# first label whose `exclude` phrases are all absent and one `include` phrase is
# present -- `exclude` is what makes overlapping names deterministic ("net income
# from operations" is Operating Income, not Net Income), so the set is
# non-overlapping by construction, not by luck. Every label here is a dollar
# figure; a percent/ratio fact carrying the same words (an "EBITDA margin") is
# rejected up front by value_type (see _headline_key), so a margin can never win
# a dollar slot.
@dataclass(frozen=True)
class _HeadlineLabel:
    key: str
    display: str
    include: tuple[str, ...]
    exclude: tuple[str, ...] = ()


_HEADLINE_LABELS: tuple[_HeadlineLabel, ...] = (
    _HeadlineLabel("revenue", "Net Revenue", ("net revenue",), ("growth", "per share")),
    _HeadlineLabel(
        "headline_gross_revenue", "Gross Revenue", ("gross revenue",), ("growth", "per share")
    ),
    _HeadlineLabel("ebitda", "EBITDA", ("ebitda",), ("margin", "per share", "growth")),
    _HeadlineLabel(
        "ebit",
        "Operating Income",
        ("income from operations", "operating income"),
        ("margin", "per share"),
    ),
    _HeadlineLabel(
        "net_income",
        "Net Income",
        ("net income",),
        ("from operations", "per share", "margin", "growth"),
    ),
    _HeadlineLabel(
        "headline_total_costs",
        "Total Costs & Expenses",
        ("total costs and expenses",),
        ("per share",),
    ),
    _HeadlineLabel("headline_sga", "SG&A", ("selling general and administrative",), ("per share",)),
    _HeadlineLabel("headline_dna", "D&A", ("depreciation and amortization",), ("per share",)),
)

# A dollar headline metric never matches a fact of these value types: a percent
# margin or a ratio can carry the same label words but is not the figure shown.
_NON_DOLLAR_TYPES = frozenset({"percent", "ratio"})

# Headline items with no canonical twin sort after every canonical metric (which
# rank well under 100 -- see _metric_rank), keeping their own reading order. The
# ones keyed on a canonical attribute rank through _metric_rank like any other.
_HEADLINE_RANK = {
    label.key: 1000 + i for i, label in enumerate(_HEADLINE_LABELS) if label.key not in _CANON_ORDER
}

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
    become their own subjects (frequency fallback). Mirrors the inspector, but
    the map is keyed case-insensitively (casefolded entity -> subject): a deck
    that writes "American Casino" in the dashboard structure and "american
    casino" on the claims must still fold together, or the whole subject's facts
    fall to "Other" and vanish from the panel. Look claims up via _subject_of."""
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
                if entity and entity.casefold() not in entity_subject:
                    entity_subject[entity.casefold()] = name
    else:
        freq = Counter(c.entity for c in claims if c.entity)
        for entity, _count in sorted(
            ((e, f) for e, f in freq.items() if f >= 2),
            key=lambda item: (-item[1], item[0]),
        ):
            entity_subject[entity.casefold()] = entity
            order.append(entity)
    order.append("Other")
    return entity_subject, order


def _subject_of(entity_subject: dict[str, str], entity: str | None) -> str:
    """The business subject a claim's entity folds to, matched case-insensitively
    (see _subject_map); "Other" when it matches no subject."""
    return entity_subject.get((entity or "").casefold(), "Other")


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


def _value_type(claim: Claim) -> str | None:
    return claim.value.get("value_type") if isinstance(claim.value, dict) else None


def _headline_key(claim: Claim) -> tuple[str, str] | None:
    """The metric a claim contributes to the screening snapshot, as
    (grouping key, display label) -- or None when the claim is not a headline
    fact and should be skipped.

    A genuine canonical attribute keys on itself. A fact the parser left in a
    catch-all bucket is recovered from its raw label: it keys on the first
    _HEADLINE_LABEL whose value type fits (a percent/ratio never matches a dollar
    metric) and whose include/exclude phrases resolve to it. A recovered fact
    whose metric has a canonical form keys on that canonical attribute, so it
    dedupes against a canonical twin rather than showing twice. Everything else
    in the catch-all buckets is skipped, keeping the snapshot a curated headline
    set rather than the raw extract.
    """
    attribute = claim.attribute
    if _is_canonical(attribute):
        return attribute, _human_attr(attribute)
    raw = normalize_name(claim.attribute_raw or "")
    if not raw:
        return None
    # Every headline label is a dollar figure; a margin/ratio carrying the same
    # words is not the figure shown, so reject it before any phrase match.
    if _value_type(claim) in _NON_DOLLAR_TYPES:
        return None
    for label in _HEADLINE_LABELS:
        if any(bad in raw for bad in label.exclude):
            continue
        if any(inc in raw for inc in label.include):
            return label.key, label.display
    return None


def _rank_for(metric_key: str, canonical_rank: dict[str, int]) -> int:
    """Sort rank for a metric key: canonical metrics (including recovered facts
    keyed on a canonical attribute) by the deal's own order, headline line items
    with no canonical twin after them in reading order."""
    if metric_key in _HEADLINE_RANK:
        return _HEADLINE_RANK[metric_key]
    return canonical_rank.get(metric_key, 99)


def _headline_claims(
    claims: Sequence[Claim], *, dashboard_structure: dict[str, Any] | None
) -> tuple[list[tuple[Claim, str, str]], dict[str, int]]:
    """(claim, metric_key, display) for every trusted, displayable headline
    claim of the deal's lead business subject, plus the metric-rank map. The one
    eligibility gate shared by the extracted panel and the LLM grounding, so the
    two never drift on which facts count.

    A missing period_year is NOT a filter: many CIMs carry statement figures with
    no machine-readable year, and dropping them empties the panel on exactly the
    deals it exists for. When a metric has several undated values, _prefer picks
    one deterministically (see _rank_key); when years ARE present it still prefers
    the latest actual."""
    entity_subject, subject_order = _subject_map(dashboard_structure, claims)
    lead_subject = subject_order[0] if subject_order else "Other"

    rows: list[tuple[Claim, str, str]] = []
    for claim in claims:
        if claim.status not in _TRUSTED:
            continue
        if _subject_of(entity_subject, claim.entity) != lead_subject:
            continue
        if _fmt_value(claim.value) == "—":
            continue
        keyed = _headline_key(claim)
        if keyed is None:
            continue
        rows.append((claim, keyed[0], keyed[1]))
    return rows, _metric_rank(dashboard_structure)


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
    rows, canonical_rank = _headline_claims(claims, dashboard_structure=dashboard_structure)

    # Best claim per metric key. Keying on the metric (not claim.attribute -- the
    # catch-all facts all share "operating_metric") both spreads recovered line
    # items across their own rows and folds a recovered fact onto its canonical
    # twin, so a metric present both ways shows once, not twice.
    best: dict[str, tuple[Claim, str]] = {}
    for claim, metric_key, label in rows:
        current = best.get(metric_key)
        if current is None or _prefer(claim, current[0]):
            best[metric_key] = (claim, label)

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
    eligibility (via _headline_claims), trust filter and value formatting as the
    extracted panel -- a metric is either a canonical attribute or a headline
    line item recovered from a catch-all bucket -- so the model reads exactly the
    figures the user sees, and no fact that is not one of them.
    """
    rows, canonical_rank = _headline_claims(claims, dashboard_structure=dashboard_structure)
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
    corroborated status, then the larger magnitude."""
    return _rank_key(candidate) > _rank_key(current)


def _rank_key(claim: Claim) -> tuple[int, int, int, float]:
    # A forecast (Estimate/Projection) ranks below any historical figure; an
    # unmarked period counts as historical, not a forecast -- so a latest actual
    # is never passed over for a later-year estimate even when the actuals carry
    # no explicit "A" kind. The magnitude is the final tiebreak: when a metric's
    # figures carry no year (period_year None -> -1 for all), preferring the
    # larger value is a stable, deterministic choice instead of insertion order.
    is_historical = 0 if claim.period_kind in ("E", "P") else 1
    year = claim.period_year if claim.period_year is not None else -1
    normalized = claim.value.get("normalized") if isinstance(claim.value, dict) else None
    magnitude = (
        normalized
        if isinstance(normalized, (int, float)) and not isinstance(normalized, bool)
        else float("-inf")
    )
    return (is_historical, year, _STATUS_RANK.get(claim.status, 0), magnitude)
