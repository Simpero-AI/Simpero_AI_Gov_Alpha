"""Service-layer tests for AE-A-CORR-1 (SIM-252): external-source conflict
handling. tests/test_corroboration_events.py already covers the table's own
RLS/immutability guarantees at the SQL level; this file covers the reactor
built on top of it (app/services/corroboration.py).
"""

import pytest

from app.models.claim import Claim
from app.repo.CorroborationEventRepo import CorroborationEventRepo
from app.services.corroboration import (
    ClaimNotCorroboratableError,
    CorroborationVerdict,
    record_corroboration_result,
    run_corroboration,
)
from app.services.status_rollup import roll_up_deal


def _claim_kwargs(
    org_pk: int, deal_id: str, *, status: str, verification_method: str | None
) -> dict:
    """Minimal valid claims row satisfying every CHECK constraint, with
    status/verification_method swappable per test. `deal_id` is required:
    claims.deal_id is NOT NULL with an FK to deals.id (f3a8c9d2e1b7), so every
    claim hangs off a real deal even though corroboration is claim-scoped."""
    has_span = status != "missing"
    return {
        "org_id": org_pk,
        "deal_id": deal_id,
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
def deal_pk(owner_conn, org_pk) -> str:
    """A deal for org_pk. Inserted via owner_conn (bypassing RLS, same as
    org_pk) so the claim's deal_id FK resolves regardless of the RLS scope the
    async session runs under. Corroboration is claim-scoped, but claims.deal_id
    is NOT NULL (f3a8c9d2e1b7), so a real deal must exist to hang a claim off."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_pk, "Corroboration Test Deal"),
        )
        return cur.fetchone()[0]


@pytest.fixture
async def cited_claim(db_session, org_pk, deal_pk) -> Claim:
    """A claim that already passed internal Verify — the only kind of claim
    an outside source has a document-sourced value to agree/disagree with."""
    claim = Claim(
        **_claim_kwargs(org_pk, deal_pk, status="cited", verification_method="exact_span")
    )
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
async def test_uncheckable_claim_raises_and_writes_nothing(db_session, org_pk, deal_pk, status):
    """A claim that never reached `cited` has no document-sourced value yet
    to disagree with — must reject before writing anything, not silently
    corroborate an unchecked claim."""
    claim = Claim(**_claim_kwargs(org_pk, deal_pk, status=status, verification_method=None))
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


# --- SIM-415: the corroboration pass (run_corroboration) + roll-up hand-off ----


class _StubSource:
    """A CorroborationSource stub: returns a fixed verdict (or None for
    no-signal), or raises to exercise the durable-failure path."""

    def __init__(self, name, verdict, *, raises=False):
        self.name = name
        self._verdict = verdict
        self._raises = raises

    async def check(self, db, claim):
        if self._raises:
            raise RuntimeError("source boom")
        return self._verdict


async def test_run_corroboration_records_an_agreeing_source(db_session, cited_claim):
    src = _StubSource("edgar", CorroborationVerdict(agrees=True, result={"form": "D"}))
    await run_corroboration(db_session, [cited_claim], [src])
    await db_session.flush()

    events = await CorroborationEventRepo(db_session).list_for_claim(cited_claim.id)
    assert [e.outside_source for e in events] == ["edgar"]
    assert cited_claim.status == "cited"  # agreement alone does not change status here


async def test_run_corroboration_marks_conflicted_on_disagree(db_session, cited_claim):
    src = _StubSource("ised", CorroborationVerdict(agrees=False, result={"status": "NOT_FOUND"}))
    await run_corroboration(db_session, [cited_claim], [src])
    await db_session.flush()

    assert cited_claim.status == "conflicted"
    assert len(await CorroborationEventRepo(db_session).list_for_claim(cited_claim.id)) == 1


async def test_run_corroboration_no_signal_records_nothing(db_session, cited_claim):
    """Absence is never a conflict: a source returning None writes no event and
    leaves the claim untouched."""
    await run_corroboration(db_session, [cited_claim], [_StubSource("ised", None)])
    await db_session.flush()

    assert cited_claim.status == "cited"
    assert await CorroborationEventRepo(db_session).list_for_claim(cited_claim.id) == []


async def test_run_corroboration_a_raising_source_is_treated_as_no_signal(db_session, cited_claim):
    """One flaky source must not sink the pass -- it is logged and skipped, and
    the other sources still record."""
    boom = _StubSource("edgar", None, raises=True)
    ok = _StubSource("ised", CorroborationVerdict(agrees=True, result={"ok": True}))
    await run_corroboration(db_session, [cited_claim], [boom, ok])
    await db_session.flush()

    events = await CorroborationEventRepo(db_session).list_for_claim(cited_claim.id)
    assert [e.outside_source for e in events] == ["ised"]


async def test_run_corroboration_skips_non_corroboratable_claims(db_session, org_pk, deal_pk):
    """A `proposed` claim has no document-verified value yet -- it is skipped,
    not handed to record_corroboration_result (which would raise)."""
    proposed = Claim(**_claim_kwargs(org_pk, deal_pk, status="proposed", verification_method=None))
    db_session.add(proposed)
    await db_session.flush()

    src = _StubSource("edgar", CorroborationVerdict(agrees=False, result={}))
    await run_corroboration(db_session, [proposed], [src])
    await db_session.flush()

    assert proposed.status == "proposed"
    assert await CorroborationEventRepo(db_session).list_for_claim(proposed.id) == []


async def test_agree_then_rollup_reaches_partially_verified(db_session, org_pk, deal_pk):
    """Acceptance: an external agree event moves a weak (reranker) claim to
    `partially_verified` through the roll-up -- the foundation's end-to-end path."""
    claim = Claim(**_claim_kwargs(org_pk, deal_pk, status="cited", verification_method="reranker"))
    db_session.add(claim)
    await db_session.flush()

    src = _StubSource("edgar", CorroborationVerdict(agrees=True, result={"form": "D"}))
    await run_corroboration(db_session, [claim], [src])
    await db_session.flush()
    await roll_up_deal(db_session, [claim])

    assert claim.status == "partially_verified"


async def test_disagree_then_rollup_stays_conflicted(db_session, cited_claim):
    """Acceptance: an external disagree event moves the claim to `conflicted`,
    and the roll-up keeps it there."""
    src = _StubSource("ised", CorroborationVerdict(agrees=False, result={"status": "NOT_FOUND"}))
    await run_corroboration(db_session, [cited_claim], [src])
    await db_session.flush()
    await roll_up_deal(db_session, [cited_claim])

    assert cited_claim.status == "conflicted"
