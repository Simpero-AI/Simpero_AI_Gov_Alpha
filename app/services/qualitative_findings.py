"""Merge the parser's per-document qualitative findings into one per-deal map
(Path B "search just in case").

The parser searches each document independently and returns, per selected
qualitative rule, a grounded {"verdict": "Y"|"N"|"unknown", "evidence": ...}.
A deal usually has one CIM, but may have several documents; this reduces them to
the single map written to deals.qualitative_findings and read by the document
evaluators.

Conservative merge: a rule is settled only when the documents that speak to it
AGREE. If two documents ground opposite verdicts (one Y, one N) that is a genuine
cross-document conflict, so the rule is dropped -> the evaluator reads it as
unknown -> human review, never an arbitrary pick. Only decisive, agreed verdicts
are kept; unknowns contribute nothing.
"""

from typing import Any


def merge_qualitative_findings(per_document: list[Any]) -> dict[str, dict]:
    """Reduce a list of per-document finding maps to one {rule_id: {verdict,
    evidence}} for the deal. Only rules whose decisive (Y/N) verdicts agree
    across documents are kept; a conflict or an all-unknown rule is omitted."""
    by_rule: dict[str, list[dict]] = {}
    for document in per_document:
        if not isinstance(document, dict):
            continue
        for rule_id, finding in document.items():
            if isinstance(finding, dict) and finding.get("verdict") in ("Y", "N"):
                by_rule.setdefault(rule_id, []).append(finding)

    merged: dict[str, dict] = {}
    for rule_id, findings in by_rule.items():
        verdicts = {f["verdict"] for f in findings}
        if len(verdicts) != 1:
            continue  # cross-document conflict -> leave unsettled (unknown)
        chosen = findings[0]
        merged[rule_id] = {
            "verdict": chosen["verdict"],
            "evidence": chosen.get("evidence") or "",
        }
    return merged
