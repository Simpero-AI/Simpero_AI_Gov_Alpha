"""Service-layer tests for AE-A-CORR-1 (SIM-252): external-source conflict
handling. tests/test_corroboration_events.py already covers the table's own
RLS/immutability guarantees at the SQL level; this file covers the reactor
built on top of it (app/services/corroboration.py).
"""

import pytest

from app.models.claim import Claim
from app.repo.CorroborationEventRepo import CorroborationEventRepo
from app.services.corroboration import ClaimNotCorroboratableError, record_corroboration_result


def _claim_kwargs(org_pk: int, *, status: str, verification_method: str | None) -> dict:
    """Minimal valid claims row satisfying every CHECK constraint, with
    status/verification_method swappable per test."""
    has_span = status != "missing"
    return {
        "org_id": org_pk,
        "entity": "Acme Corp",
        "attribute": "revenueLatestUsd",
        "value": {
            "raw": "$15,295",
            "normalized": 15295000,
            "unit": "USD",
            "value_type": "currency",
        },
        "kind": "pdf",
        "page": 3,
        "char_start": 100 if has_span else None,
        "char_end": 120 if has_span else None,
        "status": status,
        "verification_method": verification_method,
    }


@pytest.fixture
def org_pk(owner_conn, test_org_id) -> int:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (clerk_org_id) DO NOTHING",
            (test_org_id, "Org A"),
        )
        cur.execute("SELECT id FROM organisation WHERE clerk_org_id = %s", (test_org_id,))
        return cur.fetchone()[0]


@pytest.fixture
async def cited_claim(db_session, org_pk) -> Claim:
    """A claim that already passed internal Verify — the only kind of claim
    an outside source has a document-sourced value to agree/disagree with."""
    claim = Claim(**_claim_kwargs(org_pk, status="cited", verification_method="exact_span"))
    db_session.add(claim)
    await db_session.flush()
    return claim


async def test_disagreeing_result_marks_claim_conflicted(db_session, cited_claim):
    event = await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="entity_lookup",
        result={"status": "NOT_FOUND"},
        agrees=False,
    )
    await db_session.flush()

    assert cited_claim.status == "conflicted"
    assert event.outside_source == "entity_lookup"
    assert event.result == {"status": "NOT_FOUND"}
    assert event.claim_id == cited_claim.id


async def test_agreeing_result_does_not_change_status(db_session, cited_claim):
    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="entity_lookup",
        result={"status": "MATCHED", "matched_entity": "Acme Corp"},
        agrees=True,
    )
    await db_session.flush()

    assert cited_claim.status == "cited"


async def test_disagreement_never_overwrites_claims_document_sourced_value(db_session, cited_claim):
    """The whole point of `conflicted` is that both sides stay visible — the
    claim's own value must never be silently replaced by the external
    finding."""
    original_value = dict(cited_claim.value)

    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="ofac_screen",
        result={"match": True, "matched_name": "A. Corp"},
        agrees=False,
    )
    await db_session.flush()

    assert cited_claim.value == original_value


async def test_verification_method_survives_conflict(db_session, cited_claim):
    """ck_claims_checked_requires_method: `conflicted` is a checked status,
    so verification_method must stay non-null through the transition."""
    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="ofac_screen",
        result={"match": True},
        agrees=False,
    )
    await db_session.flush()

    assert cited_claim.verification_method == "exact_span"


@pytest.mark.parametrize("status", ["proposed", "rejected", "missing"])
async def test_uncheckable_claim_raises_and_writes_nothing(db_session, org_pk, status):
    """A claim that never reached `cited` has no document-sourced value yet
    to disagree with — must reject before writing anything, not silently
    corroborate an unchecked claim."""
    claim = Claim(**_claim_kwargs(org_pk, status=status, verification_method=None))
    db_session.add(claim)
    await db_session.flush()

    with pytest.raises(ClaimNotCorroboratableError):
        await record_corroboration_result(
            db_session,
            claim=claim,
            outside_source="entity_lookup",
            result={"status": "NOT_FOUND"},
            agrees=False,
        )

    assert claim.status == status
    events = await CorroborationEventRepo(db_session).list_for_claim(claim.id)
    assert events == []


async def test_multiple_disagreeing_sources_all_recorded(db_session, cited_claim):
    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="entity_lookup",
        result={"status": "NOT_FOUND"},
        agrees=False,
    )
    await db_session.flush()
    assert cited_claim.status == "conflicted"

    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="ofac_screen",
        result={"match": True},
        agrees=False,
    )
    await db_session.flush()

    events = await CorroborationEventRepo(db_session).list_for_claim(cited_claim.id)
    assert [e.outside_source for e in events] == ["entity_lookup", "ofac_screen"]
    assert cited_claim.status == "conflicted"


async def test_agreeing_event_does_not_clear_existing_conflict(db_session, cited_claim):
    """A later agreeing check must not silently resolve a conflict — the
    composite recompute across signals is AE-A-CORR-2's job, not this
    ticket's."""
    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="entity_lookup",
        result={"status": "NOT_FOUND"},
        agrees=False,
    )
    await db_session.flush()
    assert cited_claim.status == "conflicted"

    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="uspto_lookup",
        result={"status": "CONFIRMED"},
        agrees=True,
    )
    await db_session.flush()

    assert cited_claim.status == "conflicted"


async def test_event_org_id_derives_from_claim_not_a_caller_supplied_value(db_session, cited_claim):
    """No org_id parameter exists to pass mismatched — it always comes from
    the already RLS-loaded claim."""
    event = await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="entity_lookup",
        result={"status": "NOT_FOUND"},
        agrees=False,
    )

    assert event.org_id == cited_claim.org_id
