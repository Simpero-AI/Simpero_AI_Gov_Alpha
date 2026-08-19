"""Read-side join: annotate a stored screening result's rule verdicts with each
rule's question + kind from the rulebook, for API / UI consumption.

Kept out of the API route layer so a second consumer (a memo composer, an
export) does not have to reach into a private endpoint helper.
"""

from typing import Any

from app.services.screening.rulebook import Rulebook


def enrich_rule_results(
    rule_results: list[dict], rulebook: Rulebook, result_version: str
) -> list[Any]:
    """Join each stored verdict to its rule's `question` and `kind`
    (green_signal | deal_breaker) from `rulebook`, so the frontend can render
    the question text and tell a met green-signal from a tripped deal-breaker
    without duplicating policy -- track_b.yaml stays the single source of truth.

    Only annotates when `rulebook.version` matches the version the result was
    screened under (`result_version`). `screening_result.rulebook_version`
    exists precisely so an old result stays re-readable after the rules change;
    enriching a result with a *newer* rulebook would show new question text (or
    a reclassified kind) next to an old verdict -- quiet provenance corruption
    on a shipped decision. Old rulebook versions are not retained on disk, so a
    version mismatch (like a rule_id absent from the rulebook) yields null
    question/kind rather than a wrong read. The frontend degrades gracefully on
    null (falls back to the rule_id, infers kind from the gs_/db_ prefix).

    Returns plain dict rows (list[Any]) that ScreeningResultResponse validates
    into ScreeningRuleResultResponse -- same as the pre-enrichment code, which
    handed the stored JSONB rows straight to the model.
    """
    usable = rulebook if rulebook.version == result_version else None
    enriched: list[Any] = []
    for rule_result in rule_results:
        rule = usable.by_id.get(rule_result["rule_id"]) if usable is not None else None
        enriched.append(
            {
                **rule_result,
                "question": rule.question if rule is not None else None,
                "kind": rule.kind if rule is not None else None,
            }
        )
    return enriched
