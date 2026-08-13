"""Screening #4: the decision engine's three judgments and its short-circuit.

The engine is pure code over RuleResults, so most of these drive it through
`screen_deal` with monkeypatched evaluators rather than seeding a database
per rule -- the per-rule DB behavior is already covered by
tests/test_screening_evaluators.py, and re-seeding it here would test the
evaluators a second time instead of testing the decision logic.

The DB-backed cases at the bottom are the ones that genuinely need real
claims: they prove the engine and the evaluators actually compose.
"""

from __future__ import annotations

import uuid

import pytest

from app.repo.DealRepo import DealRepo
from app.services.screening import decision as decision_module
from app.services.screening.decision import ScreeningDecision, ordered_rule_ids, screen_deal
from app.services.screening.rulebook import load_rulebook
from app.services.screening.types import ClaimRef, DealField, RuleResult

RULEBOOK = load_rulebook()

_GREEN_IDS = [r.id for r in RULEBOOK.rules if r.kind == "green_signal"]
_BREAKER_IDS = [r.id for r in RULEBOOK.rules if r.kind == "deal_breaker"]


def _evidence(rule_id: str) -> DealField:
    return DealField(f"{rule_id}_field", "seeded")


def _stub_evaluators(monkeypatch, verdicts: dict[str, str], *, calls: list[str] | None = None):
    """Drive the engine with a fixed verdict per rule id, so a test states
    only the rules it cares about.

    The default is the PASSING verdict for each kind, which is not the same
    string for both: a green signal passes at Y, a deal-breaker passes at N
    ("this breaker does not apply"). Defaulting everything to Y would fire
    the first deal-breaker on every test.

    `calls` records evaluation order, which is what makes the short-circuit
    OBSERVABLE -- asserting only on the returned results would not distinguish
    "stopped early" from "evaluated everything and reported a subset"."""

    async def fake_evaluate_rule(rule_id, session, deal, rulebook):
        if calls is not None:
            calls.append(rule_id)
        passing = "Y" if rulebook.by_id[rule_id].kind == "green_signal" else "N"
        verdict = verdicts.get(rule_id, passing)
        return RuleResult(
            rule_id,
            verdict,
            _evidence(rule_id) if verdict != "unknown" else None,
            "deterministic",
            reason=None if verdict != "unknown" else f"{rule_id} has no figure",
        )

    monkeypatch.setattr(decision_module, "evaluate_rule", fake_evaluate_rule)


# --- Rule ordering ----------------------------------------------------------


def test_deal_breakers_are_ordered_before_green_signals():
    order = ordered_rule_ids(RULEBOOK)
    assert set(order) == {r.id for r in RULEBOOK.rules}
    assert len(order) == 21
    last_breaker = max(order.index(rid) for rid in _BREAKER_IDS)
    first_green = min(order.index(rid) for rid in _GREEN_IDS)
    assert last_breaker < first_green


# --- auto_decline + short-circuit -------------------------------------------


async def test_deal_breaker_auto_declines_and_cites_the_rule(monkeypatch):
    _stub_evaluators(monkeypatch, {"db_04": "Y"})
    result = await screen_deal(None, None, RULEBOOK)

    assert result.recommendation == "auto_decline"
    assert result.triggered_by is not None
    assert result.triggered_by.rule_id == "db_04"
    assert result.triggered_by.evidence == _evidence("db_04")
    assert result.rulebook_version == "track_b.v1"


async def test_auto_decline_stops_evaluating(monkeypatch):
    """The short-circuit is the point: rules after the breaker are never even
    called. #6's external checks are paid API calls, so 'evaluated but
    ignored' is materially different from 'not evaluated'."""
    calls: list[str] = []
    _stub_evaluators(monkeypatch, {"db_01": "Y"}, calls=calls)
    result = await screen_deal(None, None, RULEBOOK)

    assert result.recommendation == "auto_decline"
    assert calls == ["db_01"], "db_01 is the first breaker; nothing after it should run"
    assert len(result.results) == 1
    # No green signal was reached, so none can be reported as blocking.
    assert result.blocking == ()


async def test_first_breaker_in_order_wins(monkeypatch):
    calls: list[str] = []
    _stub_evaluators(monkeypatch, {"db_04": "Y", "db_07": "Y"}, calls=calls)
    result = await screen_deal(None, None, RULEBOOK)

    assert result.triggered_by is not None
    assert result.triggered_by.rule_id == "db_04"
    assert "db_07" not in calls


async def test_unknown_deal_breaker_does_not_auto_decline(monkeypatch):
    """The core safety property: `unknown` never auto-declines. A breaker we
    could not evaluate must reach a human, not decline the deal."""
    _stub_evaluators(monkeypatch, {"db_09": "unknown"})
    result = await screen_deal(None, None, RULEBOOK)

    assert result.recommendation == "human_review"
    assert result.triggered_by is None


async def test_unknown_deal_breaker_also_blocks_green(monkeypatch):
    """The other half, and the dangerous one. db_09 is the OFAC sanctions
    screen: if it times out it returns `unknown`, and every green signal can
    still be Y. Treating an unresolved breaker as "no breaker found" would
    recommend GREEN on a deal whose sanctions check never ran."""
    _stub_evaluators(monkeypatch, {"db_09": "unknown"})
    result = await screen_deal(None, None, RULEBOOK)

    assert result.recommendation == "human_review"
    assert [r.rule_id for r in result.blocking] == ["db_09"]


async def test_a_breaker_only_clears_green_on_a_definite_n(monkeypatch):
    """N clears it, `unknown` does not -- the distinction the fail-closed
    posture rests on."""
    _stub_evaluators(monkeypatch, {"db_05": "N"})
    assert (await screen_deal(None, None, RULEBOOK)).recommendation == "green"

    _stub_evaluators(monkeypatch, {"db_05": "unknown"})
    assert (await screen_deal(None, None, RULEBOOK)).recommendation == "human_review"


# --- green / human_review ---------------------------------------------------


async def test_all_green_signals_y_and_no_breaker_is_green(monkeypatch):
    _stub_evaluators(monkeypatch, {})  # everything Y
    result = await screen_deal(None, None, RULEBOOK)

    assert result.recommendation == "green"
    assert result.blocking == ()
    assert len(result.results) == 21


@pytest.mark.parametrize("blocking_verdict", ["N", "unknown"])
async def test_a_single_missing_must_have_blocks_green(monkeypatch, blocking_verdict):
    """Both an explicit N and an `unknown` block green -- neither satisfies a
    must-have -- and the result must say WHICH rule and why."""
    _stub_evaluators(monkeypatch, {"gs_05": blocking_verdict})
    result = await screen_deal(None, None, RULEBOOK)

    assert result.recommendation == "human_review"
    assert [r.rule_id for r in result.blocking] == ["gs_05"]
    assert result.blocking[0].verdict == blocking_verdict


async def test_blocking_lists_every_unsatisfied_must_have(monkeypatch):
    _stub_evaluators(monkeypatch, {"gs_01": "N", "gs_09": "unknown", "gs_11": "unknown"})
    result = await screen_deal(None, None, RULEBOOK)

    assert result.recommendation == "human_review"
    assert [r.rule_id for r in result.blocking] == ["gs_01", "gs_09", "gs_11"]
    # Every blocking entry carries an explanation a human can act on.
    assert all(r.verdict == "N" or r.reason for r in result.blocking)


async def test_a_failing_deal_breaker_n_does_not_block_green(monkeypatch):
    """N on a deal-breaker is the GOOD outcome ("this deal-breaker does not
    apply") -- it must not be counted as a missing must-have."""
    _stub_evaluators(monkeypatch, {rid: "N" for rid in _BREAKER_IDS})
    result = await screen_deal(None, None, RULEBOOK)

    assert result.recommendation == "green"
    assert result.blocking == ()


# --- provenance -------------------------------------------------------------


async def test_every_non_unknown_result_carries_evidence(monkeypatch):
    _stub_evaluators(monkeypatch, {"gs_02": "unknown"})
    result = await screen_deal(None, None, RULEBOOK)

    for rule_result in result.results:
        if rule_result.verdict == "unknown":
            assert rule_result.evidence is None
            assert rule_result.reason
        else:
            assert rule_result.evidence is not None


async def test_unknown_results_carry_zero_confidence(monkeypatch):
    """An `unknown` is the absence of a verdict. Letting it inherit the 1.0
    default would tell the audit trail we were certain."""
    _stub_evaluators(monkeypatch, {"gs_02": "unknown"})
    result = await screen_deal(None, None, RULEBOOK)

    by_id = {r.rule_id: r for r in result.results}
    assert by_id["gs_02"].confidence == 0.0
    assert by_id["gs_01"].confidence == 1.0


def test_rule_result_forces_zero_confidence_on_unknown():
    assert RuleResult("gs_01", "unknown", None, "llm", confidence=0.9).confidence == 0.0
    assert RuleResult("gs_01", "Y", DealField("x", "y"), "deterministic").confidence == 1.0


# --- unimplemented evaluators (#5 / #6) -------------------------------------


async def test_rules_without_an_evaluator_are_unknown_under_their_own_kind(db_session, org_a_id):
    """Not a stub: 12 rules belong to tickets #5/#6. Until those land the
    honest verdict is `unknown` -> human review, reported under the rule's
    real evaluator kind so the audit trail doesn't call an LLM rule
    deterministic."""
    deal = await DealRepo(db_session).create({"org_id": org_a_id, "name": "Unevaluated"})
    await db_session.flush()

    result = await screen_deal(db_session, deal, RULEBOOK)
    by_id = {r.rule_id: r for r in result.results}

    assert by_id["gs_01"].verdict == "unknown"
    assert by_id["gs_01"].evaluator == "llm"
    assert "SIM-405" in (by_id["gs_01"].reason or "")
    assert by_id["db_09"].evaluator == "external"
    assert "SIM-406" in (by_id["db_09"].reason or "")


async def test_structurally_unverifiable_rules_report_the_policy_not_a_ticket(db_session, org_a_id):
    """gs_11/db_08 are unknown BY POLICY -- no ticket will ever change that
    answer, so the reason must be the rulebook's own note, not "not
    implemented yet"."""
    deal = await DealRepo(db_session).create({"org_id": org_a_id, "name": "Unverifiable"})
    await db_session.flush()

    result = await screen_deal(db_session, deal, RULEBOOK)
    by_id = {r.rule_id: r for r in result.results}

    for rule_id in ("gs_11", "db_08"):
        assert by_id[rule_id].verdict == "unknown"
        assert "SIM-405" not in (by_id[rule_id].reason or "")
        assert by_id[rule_id].reason == RULEBOOK.by_id[rule_id].unknown


# --- composition with the real evaluators -----------------------------------


async def test_prohibited_sector_auto_declines_end_to_end(db_session, org_a_id):
    """The engine and the real db_04 evaluator composing over a real deal
    row -- no stubs."""
    deal = await DealRepo(db_session).create(
        {"org_id": org_a_id, "name": "Cannabis Co", "sector": "cannabis"}
    )
    await db_session.flush()

    result = await screen_deal(db_session, deal, RULEBOOK)

    assert result.recommendation == "auto_decline"
    assert result.triggered_by is not None
    assert result.triggered_by.rule_id == "db_04"
    assert isinstance(result.triggered_by.evidence, DealField)
    assert result.triggered_by.evidence.value == "cannabis"


async def test_an_unscreenable_deal_reaches_human_review(db_session, org_a_id):
    """A deal with no claims and no metadata: every rule is `unknown`, which
    is neither green nor an auto-decline."""
    deal = await DealRepo(db_session).create({"org_id": org_a_id, "name": "Empty"})
    await db_session.flush()

    result = await screen_deal(db_session, deal, RULEBOOK)

    assert result.recommendation == "human_review"
    assert all(r.verdict == "unknown" for r in result.results)
    # All 21 block: every must-have is unproven AND every breaker is unruled-out.
    assert len(result.blocking) == 21
    assert set(_GREEN_IDS) | set(_BREAKER_IDS) == {r.rule_id for r in result.blocking}
    assert isinstance(result, ScreeningDecision)


def test_claim_ref_and_deal_field_are_both_valid_evidence():
    """Evidence is a typed union; both arms must survive construction."""
    claim = RuleResult("gs_03", "Y", ClaimRef(uuid.uuid4(), "revenue", 2024), "deterministic")
    field = RuleResult("gs_08", "Y", DealField("sector", "saas"), "deterministic")
    assert isinstance(claim.evidence, ClaimRef)
    assert isinstance(field.evidence, DealField)
