"""enrich_rule_results joins a stored screening result's rule verdicts to each
rule's question + kind from the rulebook (app/services/screening/rule_view.py).

Pure over the rulebook -- no DB. The rulebook is the single source of truth for
a rule's question text and kind; the stored screening_result row carries only
the verdict, so the read joins the two -- but only when the on-disk rulebook is
the same version the result was screened under.
"""

from app.services.screening.rule_view import enrich_rule_results
from app.services.screening.rulebook import load_rulebook


def _verdict(rule_id: str) -> dict:
    """The stored RuleResult.to_json() shape -- no question/kind on the row."""
    return {
        "rule_id": rule_id,
        "verdict": "unknown",
        "evaluator": "deterministic",
        "evidence_ref": None,
        "confidence": 0.0,
        "reason": None,
    }


def test_enrich_adds_question_and_kind_from_the_rulebook():
    rulebook = load_rulebook()
    enriched = enrich_rule_results(
        [_verdict("gs_07"), _verdict("db_04")], rulebook, rulebook.version
    )

    assert enriched[0]["rule_id"] == "gs_07"
    assert enriched[0]["question"] == rulebook.by_id["gs_07"].question
    assert enriched[0]["kind"] == "green_signal"

    assert enriched[1]["rule_id"] == "db_04"
    assert enriched[1]["kind"] == "deal_breaker"
    # The stored fields survive untouched alongside the two joined ones.
    assert enriched[1]["verdict"] == "unknown"
    assert enriched[1]["evidence_ref"] is None


def test_enrich_leaves_question_and_kind_null_for_an_unknown_rule_id():
    """A rule_id absent from the rulebook must not fail the read -- it just
    carries null question/kind."""
    rulebook = load_rulebook()
    [enriched] = enrich_rule_results([_verdict("zz_not_a_rule")], rulebook, rulebook.version)

    assert enriched["question"] is None
    assert enriched["kind"] is None
    assert enriched["rule_id"] == "zz_not_a_rule"


def test_enrich_does_not_annotate_when_the_result_was_screened_under_another_version():
    """rulebook_version keeps an old result re-readable after the rules change:
    a result screened under a different version is NOT annotated with the
    current rulebook (which could show new question text / a reclassified kind
    next to an old verdict). Null question/kind rather than a wrong read."""
    rulebook = load_rulebook()
    assert rulebook.version != "track_b.v0"  # guard the fixture's premise
    enriched = enrich_rule_results([_verdict("gs_07"), _verdict("db_04")], rulebook, "track_b.v0")

    assert all(r["question"] is None and r["kind"] is None for r in enriched)
    # The verdict rows themselves still pass through intact.
    assert [r["rule_id"] for r in enriched] == ["gs_07", "db_04"]


def test_enrich_does_not_mutate_the_stored_rows():
    """The join builds new dicts so the persisted rule_results (a JSONB row read
    straight from the DB) is never edited in place."""
    rulebook = load_rulebook()
    row = _verdict("gs_07")
    enrich_rule_results([row], rulebook, rulebook.version)
    assert "question" not in row
    assert "kind" not in row
