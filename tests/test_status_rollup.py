"""Tests for AE-A-CORR-2 (SIM-254): status-lifecycle roll-up.

Two halves, same split as tests/test_screening_decision.py: the pure rule
(`resolve_status`) is driven directly over its whole input matrix, then the
DB-wired `roll_up_status` is driven off real claims / corroboration events /
edges to prove the two compose.
"""

import pytest

from app.models.claim import Claim
from app.models.edge import Edge
from app.services.corroboration import ClaimNotCorroboratableError, record_corroboration_result
from app.services.screening.claims_lookup import claims_for_attribute
from app.services.status_rollup import (
    RESOLVED_STATUSES,
    STRONG_VERIFICATION_METHODS,
    has_internal_disagreement,
    resolve_status,
    roll_up_deal,
    roll_up_status,
)

WEAK_METHOD = "reranker"


def _claim_kwargs(
    org_pk: int, deal_id: str, *, status: str, verification_method: str | None
) -> dict:
    """Minimal valid claims row satisfying every CHECK constraint, with
    status/verification_method swappable per test. `deal_id` is required:
    claims.deal_id is NOT NULL with an FK to deals.id (f3a8c9d2e1b7)."""
    has_span = status != "missing"
    return {
        "org_id": org_pk,
        "deal_id": deal_id,
        "entity": "Acme Corp",
        "attribute": "revenue",
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
    """A deal for org_pk, inserted via owner_conn (bypassing RLS) so the
    claim's deal_id FK resolves regardless of the RLS scope the async session
    runs under."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_pk, "Status Rollup Test Deal"),
        )
        return cur.fetchone()[0]


# --------------------------------------------------------------------------
# The pure rule.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "verification_method,internal_disagreement,has_disagreement,has_agreement,expected",
    [
        # Strong internal check, nothing internally inconsistent.
        ("exact_span", False, True, False, "conflicted"),
        ("exact_span", False, False, True, "verified"),
        ("exact_span", False, False, False, "verified"),
        # Weak internal check (reranker).
        (WEAK_METHOD, False, True, False, "conflicted"),
        (WEAK_METHOD, False, False, True, "partially_verified"),
        (WEAK_METHOD, False, False, False, "inconclusive"),
        # Strong method, but the claim is internally inconsistent -- demoted
        # to exactly the weak rows above.
        ("exact_span", True, True, False, "conflicted"),
        ("exact_span", True, False, True, "partially_verified"),
        ("exact_span", True, False, False, "inconclusive"),
    ],
)
def test_resolve_status_matrix(
    verification_method, internal_disagreement, has_disagreement, has_agreement, expected
):
    assert (
        resolve_status(
            verification_method=verification_method,
            internal_disagreement=internal_disagreement,
            has_disagreement=has_disagreement,
            has_agreement=has_agreement,
        )
        == expected
    )


def test_resolve_status_is_total_over_its_input_space():
    """Every combination resolves, and only ever to one of the four statuses
    the ticket names -- no silent fall-through to `cited` or None."""
    methods = sorted(STRONG_VERIFICATION_METHODS | {WEAK_METHOD})
    for method in methods:
        for internal in (False, True):
            for external_dis in (False, True):
                for external_agr in (False, True):
                    assert (
                        resolve_status(
                            verification_method=method,
                            internal_disagreement=internal,
                            has_disagreement=external_dis,
                            has_agreement=external_agr,
                        )
                        in RESOLVED_STATUSES
                    )


def test_resolved_statuses_are_all_writable_to_the_claims_table():
    """RESOLVED_STATUSES must stay a subset of the model's own CHECK list, or
    the roll-up writes a status the database rejects."""
    from app.models.claim import _STATUSES

    assert set(_STATUSES) >= RESOLVED_STATUSES


def test_strong_methods_match_the_verification_method_check_constraint():
    """STRONG_VERIFICATION_METHODS is meant to be exhaustive over the enum
    minus `reranker`. If a new method is added to the model and not triaged
    here, it silently falls into the weak bucket."""
    from app.models.claim import _VERIFICATION_METHODS

    assert STRONG_VERIFICATION_METHODS | {WEAK_METHOD} == set(_VERIFICATION_METHODS)


def test_external_disagreement_beats_every_other_signal():
    """A disagreement must never be smoothed over by a strong internal check
    plus an agreeing second source -- conflicted always wins."""
    assert (
        resolve_status(
            verification_method="human_review",
            internal_disagreement=False,
            has_disagreement=True,
            has_agreement=True,
        )
        == "conflicted"
    )


@pytest.mark.parametrize("method", sorted(STRONG_VERIFICATION_METHODS))
def test_all_strong_methods_resolve_verified_without_external_check(method):
    assert (
        resolve_status(
            verification_method=method,
            internal_disagreement=False,
            has_disagreement=False,
            has_agreement=False,
        )
        == "verified"
    )


@pytest.mark.parametrize("method", sorted(STRONG_VERIFICATION_METHODS))
def test_internal_disagreement_blocks_verified_for_every_strong_method(method):
    assert (
        resolve_status(
            verification_method=method,
            internal_disagreement=True,
            has_disagreement=False,
            has_agreement=False,
        )
        == "inconclusive"
    )


def test_unknown_verification_method_is_treated_as_weak():
    """Fail-closed: a method this module has never heard of must not earn
    `verified` by default."""
    assert (
        resolve_status(
            verification_method="some_future_method",
            internal_disagreement=False,
            has_disagreement=False,
            has_agreement=False,
        )
        == "inconclusive"
    )


# --------------------------------------------------------------------------
# roll_up_status: the DB-wired version, driven off real rows.
# --------------------------------------------------------------------------


@pytest.fixture
async def cited_claim(db_session, org_pk, deal_pk) -> Claim:
    """A weakly-verified claim -- the interesting case, since a strong one
    reaches `verified` without any external signal at all."""
    claim = Claim(**_claim_kwargs(org_pk, deal_pk, status="cited", verification_method=WEAK_METHOD))
    db_session.add(claim)
    await db_session.flush()
    return claim


@pytest.fixture
async def strong_claim(db_session, org_pk, deal_pk) -> Claim:
    claim = Claim(
        **_claim_kwargs(org_pk, deal_pk, status="cited", verification_method="exact_span")
    )
    db_session.add(claim)
    await db_session.flush()
    return claim


async def _other_claim(db_session, org_pk, deal_pk) -> Claim:
    """A second claim, so a contradicts edge has somewhere to point
    (ck_edges_no_self_edge forbids an edge from a claim to itself)."""
    claim = Claim(
        **_claim_kwargs(org_pk, deal_pk, status="cited", verification_method="exact_span")
    )
    claim.attribute = "gross_profit"
    db_session.add(claim)
    await db_session.flush()
    return claim


async def _add_contradicts_edge(db_session, org_pk, *, source: Claim, target: Claim) -> None:
    db_session.add(
        Edge(
            org_id=org_pk,
            from_claim_id=source.id,
            to_claim_id=target.id,
            type="contradicts",
            basis="planted by test",
            created_by="consistency",
            run_id="test-rollup",
        )
    )
    await db_session.flush()


async def test_rollup_strong_method_no_events_is_verified(db_session, strong_claim):
    assert await roll_up_status(db_session, strong_claim) == "verified"
    assert strong_claim.status == "verified"


async def test_rollup_weak_method_no_events_is_inconclusive(db_session, cited_claim):
    assert await roll_up_status(db_session, cited_claim) == "inconclusive"
    assert cited_claim.status == "inconclusive"


async def test_rollup_weak_method_with_agreeing_event_is_partially_verified(
    db_session, cited_claim
):
    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="entity_lookup",
        result={"status": "MATCHED"},
        agrees=True,
    )
    await db_session.flush()

    assert await roll_up_status(db_session, cited_claim) == "partially_verified"


async def test_rollup_after_disagreement_is_conflicted_regardless_of_method(
    db_session, strong_claim
):
    await record_corroboration_result(
        db_session,
        claim=strong_claim,
        outside_source="ofac_screen",
        result={"match": True},
        agrees=False,
    )
    await db_session.flush()
    assert strong_claim.status == "conflicted"

    assert await roll_up_status(db_session, strong_claim) == "conflicted"


async def test_rollup_is_idempotent(db_session, cited_claim):
    first = await roll_up_status(db_session, cited_claim)
    second = await roll_up_status(db_session, cited_claim)

    assert first == second == "inconclusive"


async def test_roll_up_deal_matches_per_claim_in_one_batch(
    db_session, org_pk, deal_pk, strong_claim, cited_claim
):
    """roll_up_deal resolves the same statuses as roll_up_status would, batched:
    strong -> verified, weak -> inconclusive, a strong claim demoted by a
    contradicts edge -> inconclusive, a weak claim with an agreeing external
    event -> partially_verified. Guards the batch path against diverging from
    the per-claim decision it reimplements inline."""
    demoted = Claim(
        **_claim_kwargs(org_pk, deal_pk, status="cited", verification_method="exact_span")
    )
    demoted.attribute = "ebitda"
    corroborated = Claim(
        **_claim_kwargs(org_pk, deal_pk, status="cited", verification_method=WEAK_METHOD)
    )
    corroborated.attribute = "gross_profit"
    db_session.add_all([demoted, corroborated])
    await db_session.flush()

    other = await _other_claim(db_session, org_pk, deal_pk)
    await _add_contradicts_edge(db_session, org_pk, source=demoted, target=other)
    await record_corroboration_result(
        db_session,
        claim=corroborated,
        outside_source="entity_lookup",
        result={"status": "MATCHED"},
        agrees=True,
    )
    await db_session.flush()

    await roll_up_deal(db_session, [strong_claim, cited_claim, demoted, corroborated])

    assert strong_claim.status == "verified"
    assert cited_claim.status == "inconclusive"
    assert demoted.status == "inconclusive"
    assert corroborated.status == "partially_verified"


async def test_roll_up_deal_over_no_claims_is_a_noop(db_session):
    await roll_up_deal(db_session, [])  # no query, no error


async def test_rollup_is_stable_re_run_over_an_already_resolved_status(db_session, strong_claim):
    """Idempotency where the SECOND pass's INPUT status is already a resolved
    value, not `cited`. test_rollup_is_idempotent only re-runs from
    `cited` -> `inconclusive`; here the first pass moves `cited` -> `verified`,
    so the re-run exercises a resolved status as input. It must not raise
    (every resolved status is in CORROBORATABLE_STATUSES) and must not drift."""
    assert await roll_up_status(db_session, strong_claim) == "verified"
    assert strong_claim.status == "verified"

    assert await roll_up_status(db_session, strong_claim) == "verified"
    assert strong_claim.status == "verified"


async def test_rollup_upgrades_as_new_agreeing_evidence_arrives(db_session, cited_claim):
    """The roll-up is meant to be re-run as more corroboration lands, not
    computed once and frozen."""
    assert await roll_up_status(db_session, cited_claim) == "inconclusive"

    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="entity_lookup",
        result={"status": "MATCHED"},
        agrees=True,
    )
    await db_session.flush()

    assert await roll_up_status(db_session, cited_claim) == "partially_verified"


async def test_rollup_stays_conflicted_after_a_later_agreeing_event(db_session, cited_claim):
    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="entity_lookup",
        result={"status": "NOT_FOUND"},
        agrees=False,
    )
    await db_session.flush()
    assert await roll_up_status(db_session, cited_claim) == "conflicted"

    await record_corroboration_result(
        db_session,
        claim=cited_claim,
        outside_source="ofac_screen",
        result={"match": False},
        agrees=True,
    )
    await db_session.flush()

    assert await roll_up_status(db_session, cited_claim) == "conflicted"


@pytest.mark.parametrize("status", ["proposed", "rejected", "missing"])
async def test_rollup_rejects_uncheckable_claim(db_session, org_pk, deal_pk, status):
    claim = Claim(**_claim_kwargs(org_pk, deal_pk, status=status, verification_method=None))
    db_session.add(claim)
    await db_session.flush()

    with pytest.raises(ClaimNotCorroboratableError):
        await roll_up_status(db_session, claim)

    assert claim.status == status


async def test_rollup_does_not_mutate_the_claim_value(db_session, cited_claim):
    """The roll-up resolves trust, never the figure itself."""
    before = dict(cited_claim.value)

    await roll_up_status(db_session, cited_claim)

    assert cited_claim.value == before
    assert cited_claim.verification_method == WEAK_METHOD


# --------------------------------------------------------------------------
# The internal-disagreement signal.
# --------------------------------------------------------------------------


async def test_formula_mismatch_flag_demotes_a_strong_claim(db_session, strong_claim):
    """3b re-executed this claim's arithmetic and got a different answer. An
    exact-span citation must not launder that into `verified`."""
    strong_claim.flags = ["formula_mismatch"]
    await db_session.flush()

    assert await has_internal_disagreement(db_session, strong_claim) is True
    assert await roll_up_status(db_session, strong_claim) == "inconclusive"


async def test_formula_mismatch_plus_external_agreement_is_partially_verified(
    db_session, strong_claim
):
    strong_claim.flags = ["formula_mismatch"]
    await record_corroboration_result(
        db_session,
        claim=strong_claim,
        outside_source="entity_lookup",
        result={"status": "MATCHED"},
        agrees=True,
    )
    await db_session.flush()

    assert await roll_up_status(db_session, strong_claim) == "partially_verified"


async def test_outgoing_contradicts_edge_demotes(db_session, org_pk, deal_pk, strong_claim):
    other = await _other_claim(db_session, org_pk, deal_pk)
    await _add_contradicts_edge(db_session, org_pk, source=strong_claim, target=other)

    assert await has_internal_disagreement(db_session, strong_claim) is True
    assert await roll_up_status(db_session, strong_claim) == "inconclusive"


async def test_incoming_contradicts_edge_demotes(db_session, org_pk, deal_pk, strong_claim):
    """reconciliation canonicalizes `contradicts` as from < to, so whether a
    claim is the `from` or the `to` side is an accident of uuid ordering --
    both must count."""
    other = await _other_claim(db_session, org_pk, deal_pk)
    await _add_contradicts_edge(db_session, org_pk, source=other, target=strong_claim)

    assert await has_internal_disagreement(db_session, strong_claim) is True
    assert await roll_up_status(db_session, strong_claim) == "inconclusive"


async def test_non_contradicts_edges_do_not_demote(db_session, org_pk, deal_pk, strong_claim):
    """same_fact and derived_from are agreement signals, not disagreement."""
    other = await _other_claim(db_session, org_pk, deal_pk)
    for edge_type in ("same_fact", "derived_from"):
        db_session.add(
            Edge(
                org_id=org_pk,
                from_claim_id=strong_claim.id,
                to_claim_id=other.id,
                type=edge_type,
                basis="planted by test",
                created_by="reconciliation",
                run_id="test-rollup",
            )
        )
    await db_session.flush()

    assert await has_internal_disagreement(db_session, strong_claim) is False
    assert await roll_up_status(db_session, strong_claim) == "verified"


async def test_superseded_by_same_fact_flag_does_not_demote(db_session, strong_claim):
    """That flag's contract meaning is "this claim corroborates a canonical
    claim elsewhere" -- an agreement signal. Demoting on it would penalise a
    claim for being corroborated."""
    strong_claim.flags = ["superseded_by_same_fact"]
    await db_session.flush()

    assert await has_internal_disagreement(db_session, strong_claim) is False
    assert await roll_up_status(db_session, strong_claim) == "verified"


async def test_unrelated_flags_do_not_demote(db_session, strong_claim):
    strong_claim.flags = ["scale_assumed", "ragged_table_rows"]
    await db_session.flush()

    assert await has_internal_disagreement(db_session, strong_claim) is False
    assert await roll_up_status(db_session, strong_claim) == "verified"


async def test_no_flags_is_not_a_disagreement(db_session, strong_claim):
    """claims.flags is nullable -- `None` must not blow up the membership
    test."""
    assert strong_claim.flags is None
    assert await has_internal_disagreement(db_session, strong_claim) is False


async def test_another_claims_contradicts_edge_does_not_demote(
    db_session, org_pk, deal_pk, strong_claim
):
    """The edge lookup must be anchored to this claim, not to the deal."""
    other = await _other_claim(db_session, org_pk, deal_pk)
    third = Claim(
        **_claim_kwargs(org_pk, deal_pk, status="cited", verification_method="exact_span")
    )
    third.attribute = "ebitda"
    db_session.add(third)
    await db_session.flush()

    await _add_contradicts_edge(db_session, org_pk, source=other, target=third)

    assert await has_internal_disagreement(db_session, strong_claim) is False
    assert await roll_up_status(db_session, strong_claim) == "verified"


async def test_external_disagreement_still_wins_over_internal_demotion(db_session, strong_claim):
    """Both signals firing resolves to `conflicted`, not `inconclusive` --
    an outside source disagreeing is the more specific, more actionable
    finding."""
    strong_claim.flags = ["formula_mismatch"]
    await record_corroboration_result(
        db_session,
        claim=strong_claim,
        outside_source="ofac_screen",
        result={"match": True},
        agrees=False,
    )
    await db_session.flush()

    assert await roll_up_status(db_session, strong_claim) == "conflicted"


# --------------------------------------------------------------------------
# Downstream interaction: screening reads claims.status.
# --------------------------------------------------------------------------


async def test_rollup_demotes_a_claim_out_of_screening_trust(db_session, cited_claim):
    """app/services/screening/claims_lookup.py trusts `cited` but not
    `inconclusive`. A reranker-verified claim with no external check
    therefore DISAPPEARS from screening once the roll-up runs over it --
    evaluators go `unknown` and deals get pushed toward human_review.

    That is the intended fail-closed behaviour, but it is a live product
    change, which is why nothing wires the roll-up into the pipeline here
    (that call belongs to SIM-253). Pinned so the effect is documented
    rather than discovered."""
    before = await claims_for_attribute(db_session, cited_claim.deal_id, "revenue")
    assert cited_claim.id in {c.id for c in before}

    await roll_up_status(db_session, cited_claim)
    await db_session.flush()

    after = await claims_for_attribute(db_session, cited_claim.deal_id, "revenue")
    assert cited_claim.id not in {c.id for c in after}


async def test_rollup_keeps_a_strong_claim_inside_screening_trust(db_session, strong_claim):
    """The other side of the same coin: `cited` -> `verified` is still
    trusted, so a well-verified claim survives the roll-up."""
    await roll_up_status(db_session, strong_claim)
    await db_session.flush()

    # Pin the `cited` -> `verified` transition this test's docstring names, not
    # just screening membership: `cited` (a no-op roll-up) and
    # `partially_verified` are both in _TRUSTED_STATUSES too, so a bare
    # membership check would still pass under a strong -> verified regression.
    assert strong_claim.status == "verified"
    after = await claims_for_attribute(db_session, strong_claim.deal_id, "revenue")
    assert strong_claim.id in {c.id for c in after}
