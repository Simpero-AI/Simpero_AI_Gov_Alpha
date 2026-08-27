"""Merge the parser's per-document dashboard structures into one per-deal
structure (Pipeline Inspector).

The parser organizes each document independently -- grounded, arranging only
that document's own extracted entities and canonical metrics, never inventing a
value. A deal usually has one CIM, but may have several documents; this reduces
their structures to the single one written to deals.dashboard_structure and read
by the Inspector.

Union merge: every document's consolidated subject folds into one deal-level
"Consolidated" (they all name the same whole company); segments merge by name;
each subject's entity list is unioned (first-seen order, deduped). metric_order
is the first-seen union across documents. The result keeps the parser's grounding
invariants -- exactly one consolidated subject, and only entity/metric strings the
parser already validated per document -- so nothing here can introduce a value or
an entity the extraction did not produce.
"""

from typing import Any


def _norm(name: str) -> str:
    return " ".join(name.split()).lower()


def merge_dashboard_structures(per_document: list[Any]) -> dict[str, Any] | None:
    """Reduce a list of per-document dashboard structures to one for the deal.
    Returns None when nothing organizable survives (the Inspector then falls back
    to deterministic frequency grouping)."""
    # One consolidated bucket for the whole company; segments keyed by normalized
    # name so the same segment across documents merges instead of duplicating.
    consolidated_entities: list[str] = []
    consolidated_name = "Consolidated"
    segments: dict[str, dict[str, Any]] = {}  # norm(name) -> {"name", "entities": [...]}
    metric_order: list[str] = []
    seen_metric: set[str] = set()

    def _extend(dest: list[str], entities: Any) -> None:
        if not isinstance(entities, list):
            return
        for e in entities:
            if isinstance(e, str) and e and e not in dest:
                dest.append(e)

    for document in per_document:
        if not isinstance(document, dict):
            continue
        for subject in document.get("subjects") or []:
            if not isinstance(subject, dict):
                continue
            name = subject.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            if subject.get("kind") == "consolidated":
                consolidated_name = name.strip()
                _extend(consolidated_entities, subject.get("entities"))
            else:
                bucket = segments.setdefault(_norm(name), {"name": name.strip(), "entities": []})
                _extend(bucket["entities"], subject.get("entities"))
        for metric in document.get("metric_order") or []:
            if isinstance(metric, str) and metric and metric not in seen_metric:
                seen_metric.add(metric)
                metric_order.append(metric)

    subjects: list[dict[str, Any]] = []
    if consolidated_entities or segments:
        # A consolidated anchor always leads so the Inspector has a whole-company
        # subject even when only segment entities were assigned.
        subjects.append(
            {"name": consolidated_name, "kind": "consolidated", "entities": consolidated_entities}
        )
    for bucket in segments.values():
        if bucket["entities"]:
            subjects.append(
                {"name": bucket["name"], "kind": "segment", "entities": bucket["entities"]}
            )

    if not subjects and not metric_order:
        return None
    return {"subjects": subjects, "metric_order": metric_order}
