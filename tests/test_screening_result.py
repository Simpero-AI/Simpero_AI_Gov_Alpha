"""Screening #4: persistence of a screening pass.

Covers what the decision-engine tests deliberately don't: that a decision
survives the round-trip into `screening_result` with its evidence intact,
that the row is genuinely tenant-isolated, and that the write-once posture
holds at the database layer rather than only by convention.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from app.models.screening_result import RECOMMENDATIONS
from app.repo.DealRepo import DealRepo
from app.repo.ScreeningResultRepo import ScreeningResultRepo
from app.services.screening.decision import screen_deal
from app.services.screening.rulebook import load_rulebook
from app.services.screening.types import ClaimRef, DealField, RuleResult

RULEBOOK = load_rulebook()


async def _deal(db_session, org_a_id, **fields):
    deal = await DealRepo(db_session).create({"org_id": org_a_id, "name": "Screened Co", **fields})
    await db_session.flush()
    return deal


# --- round-trip -------------------------------------------------------------


async def test_recording_a_decision_persists_version_and_verdicts(db_session, org_a_id):
    deal = await _deal(db_session, org_a_id, sector="cannabis")
    decision = await screen_deal(db_session, deal, RULEBOOK)

    row = await ScreeningResultRepo(db_session).record(decision, org_id=org_a_id, deal_id=deal.id)
    await db_session.flush()

    assert row.recommendation == "auto_decline"
    assert row.rulebook_version == "track_b.v1"
    assert row.deal_id == deal.id

    # Short-circuit: breakers run in rulebook order, so the stored list is
    # db_01..db_04 and ENDS at the one that fired -- it is not just the
    # matched rule. The earlier entries are the breakers genuinely checked
    # and cleared (or left unknown) on the way there, and dropping them would
    # misrepresent what the run actually evaluated.
    stored = [r["rule_id"] for r in row.rule_results]
    assert stored == ["db_01", "db_02", "db_03", "db_04"]
    assert row.rule_results[-1]["verdict"] == "Y"
    assert all(r["verdict"] != "Y" for r in row.rule_results[:-1])
    # Nothing past the breaker ran -- no green signal was reached.
    assert not any(rid.startswith("gs_") for rid in stored)


async def test_stored_rule_results_carry_the_four_required_fields(db_session, org_a_id):
    """#4's acceptance names verdict, evaluator, evidence_ref and confidence
    explicitly -- all four present on every rule, evidence_ref explicitly
    null rather than omitted when there is nothing to cite."""
    deal = await _deal(db_session, org_a_id)
    decision = await screen_deal(db_session, deal, RULEBOOK)

    row = await ScreeningResultRepo(db_session).record(decision, org_id=org_a_id, deal_id=deal.id)
    await db_session.flush()

    assert len(row.rule_results) == 21
    for entry in row.rule_results:
        assert set(entry) == {
            "rule_id",
            "verdict",
            "evaluator",
            "evidence_ref",
            "confidence",
            "reason",
        }


async def test_evidence_survives_the_json_round_trip(db_session, org_a_id):
    """A DealField evidence ref must come back out identifiable -- the whole
    point of storing it is that a human can go look at what decided this."""
    deal = await _deal(db_session, org_a_id, sector="gambling")
    decision = await screen_deal(db_session, deal, RULEBOOK)

    row = await ScreeningResultRepo(db_session).record(decision, org_id=org_a_id, deal_id=deal.id)
    await db_session.flush()
    await db_session.refresh(row)

    # [-1], not [0]: the breaker that fired is the LAST entry (see
    # test_recording_a_decision_persists_version_and_verdicts).
    evidence = row.rule_results[-1]["evidence_ref"]
    assert evidence == {"kind": "deal_field", "field": "sector", "value": "gambling"}


def test_claim_evidence_serializes_its_uuid_as_a_string():
    """JSONB has no UUID type -- an un-stringified uuid.UUID raises on
    insert, so this is the guard for the serializer, not for JSON itself."""
    claim_id = uuid.uuid4()
    result = RuleResult("gs_03", "Y", ClaimRef(claim_id, "revenue", 2024), "deterministic")
    evidence = result.to_json()["evidence_ref"]

    assert evidence == {
        "kind": "claim",
        "claim_id": str(claim_id),
        "attribute": "revenue",
        "period_year": 2024,
    }
    assert isinstance(evidence["claim_id"], str)


def test_the_two_evidence_kinds_are_distinguishable_after_serialization():
    """Both arms carry a `kind` discriminator; without it a reader would have
    to guess the union arm from which keys happen to be present."""
    claim = RuleResult(
        "gs_03", "Y", ClaimRef(uuid.uuid4(), "revenue", None), "deterministic"
    ).to_json()
    field = RuleResult("gs_08", "Y", DealField("sector", "saas"), "deterministic").to_json()

    assert claim["evidence_ref"]["kind"] == "claim"
    assert field["evidence_ref"]["kind"] == "deal_field"


# --- latest_for_deal --------------------------------------------------------


async def test_latest_for_deal_returns_the_most_recent_pass(db_session, org_a_id):
    """Rows are append-only, so a re-screen adds a row -- the reader must get
    the newest, not the first."""
    deal = await _deal(db_session, org_a_id)
    repo = ScreeningResultRepo(db_session)

    first = await screen_deal(db_session, deal, RULEBOOK)
    await repo.record(first, org_id=org_a_id, deal_id=deal.id)
    await db_session.flush()

    deal.sector = "cannabis"
    await db_session.flush()
    second = await screen_deal(db_session, deal, RULEBOOK)
    newer = await repo.record(second, org_id=org_a_id, deal_id=deal.id)
    await db_session.flush()

    latest = await repo.latest_for_deal(deal.id)
    assert latest is not None
    assert latest.id == newer.id
    assert latest.recommendation == "auto_decline"


async def test_latest_for_deal_is_none_before_any_screening(db_session, org_a_id):
    deal = await _deal(db_session, org_a_id)
    assert await ScreeningResultRepo(db_session).latest_for_deal(deal.id) is None


# --- tenant isolation -------------------------------------------------------


async def test_screening_results_are_rls_scoped(db_session, org_a_id, owner_conn, test_org_id):
    """Another org's screening verdicts must be invisible. Seeded through the
    owner connection because a dd_app session clamped to org A could never
    create org B's row in the first place -- same idiom as
    tests/test_deals_rls.py."""
    deal = await _deal(db_session, org_a_id)
    await ScreeningResultRepo(db_session).record(
        await screen_deal(db_session, deal, RULEBOOK), org_id=org_a_id, deal_id=deal.id
    )
    await db_session.flush()

    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) "
            "VALUES (%s, %s, now()) ON CONFLICT (clerk_org_id) DO NOTHING",
            ("other-tenant-11111111", "Org B"),
        )
        cur.execute(
            "SELECT id FROM organisation WHERE clerk_org_id = %s", ("other-tenant-11111111",)
        )
        org_b_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_b_id, "Org B Deal"),
        )
        deal_b_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO screening_result "
            "(org_id, deal_id, rulebook_version, recommendation, rule_results) "
            "VALUES (%s, %s, %s, %s, %s)",
            (org_b_id, deal_b_id, "track_b.v1", "green", "[]"),
        )

    try:
        visible = (
            (await db_session.execute(text("SELECT org_id FROM screening_result"))).scalars().all()
        )
        assert org_b_id not in visible
        assert set(visible) == {org_a_id}
        # And the other org's deal is unreachable by direct lookup too.
        assert await ScreeningResultRepo(db_session).latest_for_deal(deal_b_id) is None
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM screening_result WHERE org_id = %s", (org_b_id,))
            cur.execute("DELETE FROM deals WHERE org_id = %s", (org_b_id,))


# --- write-once at the database layer ---------------------------------------


@pytest.mark.parametrize("statement", ["UPDATE", "DELETE"])
async def test_screening_results_cannot_be_mutated_by_the_app_role(db_session, org_a_id, statement):
    """UPDATE/DELETE are REVOKEd from dd_app in this table's migration. This
    asserts the DATABASE refuses them -- an application-level guard would be
    bypassable and give false assurance (same reasoning as human_audit_log).
    """
    deal = await _deal(db_session, org_a_id)
    row = await ScreeningResultRepo(db_session).record(
        await screen_deal(db_session, deal, RULEBOOK), org_id=org_a_id, deal_id=deal.id
    )
    await db_session.flush()

    sql = (
        f"UPDATE screening_result SET recommendation = 'green' WHERE id = '{row.id}'"
        if statement == "UPDATE"
        else f"DELETE FROM screening_result WHERE id = '{row.id}'"
    )
    with pytest.raises(ProgrammingError):
        await db_session.execute(text(sql))


async def test_recommendation_is_check_constrained(db_session, org_a_id):
    """The three recommendations are a closed set at the schema level, so a
    typo'd verdict cannot be persisted as if it were meaningful."""
    deal = await _deal(db_session, org_a_id)
    with pytest.raises(IntegrityError, match="ck_screening_result_recommendation"):
        await db_session.execute(
            text(
                "INSERT INTO screening_result "
                "(org_id, deal_id, rulebook_version, recommendation, rule_results) "
                "VALUES (:org, :deal, 'track_b.v1', 'definitely_yes', '[]'::jsonb)"
            ),
            {"org": org_a_id, "deal": str(deal.id)},
        )


def test_model_and_migration_agree_on_the_recommendation_set():
    """RECOMMENDATIONS is what the engine's Recommendation Literal produces;
    the migration's CHECK is the enforcement. Drift between them would only
    show up as a runtime insert failure on whichever value was forgotten."""
    assert set(RECOMMENDATIONS) == {"auto_decline", "green", "human_review"}
