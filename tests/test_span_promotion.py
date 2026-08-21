"""SIM-412: the exact-span `proposed -> cited` promoter
(app/services/span_promotion.py).

Structured around what the promoter must REFUSE, not just what it promotes.
This is the single gate between "the parser said so" and "screening and every
external corroborator will act on it" (app/services/screening/claims_lookup.py
trusts `cited`; app/services/corroboration.py requires it), so a claim
promoted for the wrong reason is a diligence answer resting on a citation
nobody checked.

Every test scopes its claims to its own data_source row and promotes that
scope, the way the job does (one call per document). Counter assertions would
otherwise be order-dependent: other modules commit claims under this same
tenant through owner_conn, outside this session's rollback.

Fixture scaffolding (owner_conn/org_pk/deal_pk/_claim_kwargs) follows
tests/test_corroboration.py, the closest precedent: claims.deal_id is NOT
NULL, so every claim hangs off a real deal row seeded via the RLS-bypassing
owner connection.
"""

import uuid

import pytest
from sqlalchemy import select

from app.models.claim import Claim
from app.services.screening.claims_lookup import claims_for_attribute
from app.services.span_promotion import promote_exact_span

_OTHER_ORG_KEY = "test-tenant-span-other"


def _claim_kwargs(
    org_pk: int,
    deal_id: str,
    data_source_id: uuid.UUID | None,
    *,
    status: str = "proposed",
    verification_method: str | None = None,
    kind: str = "pdf",
    flags: list[str] | None = None,
    attribute: str = "revenue",
) -> dict:
    """Minimal valid claims row satisfying every CHECK constraint.

    `missing` claims get no span (ck_claims_missing_has_no_span); xlsx claims
    carry sheet/cell_ref instead of page (ck_claims_locator_matches_kind).
    """
    has_span = status != "missing"
    row: dict = {
        "org_id": org_pk,
        "deal_id": deal_id,
        "data_source_id": data_source_id,
        "entity": "Acme Corp",
        "attribute": attribute,
        "value": {
            "raw": "$15,295",
            "normalized": 15295000,
            "unit": "USD",
            "value_type": "currency",
        },
        "kind": kind,
        "status": status,
        "verification_method": verification_method,
        "flags": flags,
    }
    if kind == "xlsx":
        row.update(sheet="PnL", cell_ref="B12", char_start=None, char_end=None)
    else:
        row.update(
            page=3,
            char_start=100 if has_span else None,
            char_end=120 if has_span else None,
        )
    return row


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
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_pk, "Span Promotion Test Deal"),
        )
        return cur.fetchone()[0]


def _seed_data_source(owner_conn, org_pk: int, deal_pk: str, filename: str) -> uuid.UUID:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (org_pk, deal_pk, f"k/{filename}", filename, "0" * 64),
        )
        return uuid.UUID(str(cur.fetchone()[0]))


@pytest.fixture
def ds_pk(owner_conn, org_pk, deal_pk) -> uuid.UUID:
    """The document under test. The job promotes one data_source at a time."""
    return _seed_data_source(owner_conn, org_pk, deal_pk, "cim-01.pdf")


async def _add(db_session, **kwargs) -> Claim:
    claim = Claim(**kwargs)
    db_session.add(claim)
    await db_session.flush()
    return claim


# --------------------------------------------------------------------------
# What it promotes
# --------------------------------------------------------------------------


async def test_span_resolved_proposed_claim_becomes_cited(db_session, org_pk, deal_pk, ds_pk):
    claim = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk))

    summary = await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert claim.status == "cited"
    assert claim.verification_method == "exact_span"
    assert summary.claims_considered == 1
    assert summary.claims_promoted == 1
    assert summary.skipped_binding_unsupported == 0


async def test_promoted_row_satisfies_the_checked_requires_method_constraint(
    db_session, org_pk, deal_pk, ds_pk
):
    """ck_claims_checked_requires_method rejects `cited` with a NULL
    verification_method at the DB level, so the flush is the real assertion --
    setting status without the method would raise here, not silently pass."""
    await _add(db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk))

    await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()  # must not raise IntegrityError

    row = (await db_session.scalars(select(Claim).where(Claim.data_source_id == ds_pk))).one()
    assert (row.status, row.verification_method) == ("cited", "exact_span")


async def test_docx_claims_are_promoted_too(db_session, org_pk, deal_pk, ds_pk):
    """char_start/char_end on a DOCX claim are offsets into extracted text,
    exactly as on a PDF -- the exact-span method applies unchanged."""
    kwargs = _claim_kwargs(org_pk, deal_pk, ds_pk, kind="docx")
    kwargs.update(page=None, paragraph=7)  # ck_claims_locator_matches_kind
    claim = await _add(db_session, **kwargs)

    await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert claim.status == "cited"


async def test_promotion_is_what_makes_a_claim_visible_to_screening(
    db_session, org_pk, deal_pk, ds_pk
):
    """The product consequence this ticket exists for: claims_lookup trusts
    `cited`, so a `proposed` claim is invisible to every screening evaluator
    until this pass runs."""
    claim = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk, attribute="revenue"))
    deal_uuid = uuid.UUID(str(deal_pk))

    assert await claims_for_attribute(db_session, deal_uuid, "revenue") == []

    await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    after = await claims_for_attribute(db_session, deal_uuid, "revenue")
    assert [c.id for c in after] == [claim.id]


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------


async def test_binding_unsupported_claim_is_held_at_proposed(db_session, org_pk, deal_pk, ds_pk):
    """The binding auditor (SIM-359) found the cited span does not support the
    value. Promoting it would launder a known-bad citation into a trusted
    one."""
    claim = await _add(
        db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk, flags=["binding_unsupported"])
    )

    summary = await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert claim.status == "proposed"
    assert claim.verification_method is None
    assert summary.claims_considered == 1
    assert summary.claims_promoted == 0
    assert summary.skipped_binding_unsupported == 1


async def test_binding_unsupported_is_caught_alongside_other_flags(
    db_session, org_pk, deal_pk, ds_pk
):
    """flags is a list, and the auditor's flag routinely travels with others --
    the check must be membership, not equality against a single-flag list."""
    claim = await _add(
        db_session,
        **_claim_kwargs(
            org_pk,
            deal_pk,
            ds_pk,
            flags=["scale_assumed", "binding_unsupported", "ragged_table_rows"],
        ),
    )

    await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert claim.status == "proposed"


async def test_unrelated_flags_do_not_block_promotion(db_session, org_pk, deal_pk, ds_pk):
    """Ticket-exact scope: only `binding_unsupported` disqualifies. Narrowing
    further on other quality flags is a deliberate follow-up, and this test
    pins today's behaviour so that change is visible when it happens."""
    claim = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk, flags=["scale_assumed"]))

    await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert claim.status == "cited"


async def test_missing_claim_stays_missing(db_session, org_pk, deal_pk, ds_pk):
    claim = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk, status="missing"))

    summary = await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert claim.status == "missing"
    assert claim.verification_method is None
    assert summary.claims_considered == 0


@pytest.mark.parametrize(
    ("status", "verification_method"),
    [
        ("rejected", None),
        ("cited", "reranker"),
        ("verified", "exact_span"),
        ("conflicted", "exact_span"),
        ("inconclusive", "reranker"),
        ("partially_verified", "reranker"),
    ],
)
async def test_claims_past_proposed_are_never_re_decided(
    db_session, org_pk, deal_pk, ds_pk, status, verification_method
):
    """This pass owns exactly one transition. A claim the roll-up already moved
    on to `verified`/`inconclusive`, or one a weaker method already cited, must
    not be dragged back to `cited`/`exact_span`."""
    claim = await _add(
        db_session,
        **_claim_kwargs(
            org_pk, deal_pk, ds_pk, status=status, verification_method=verification_method
        ),
    )

    summary = await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert claim.status == status
    assert claim.verification_method == verification_method
    assert summary.claims_considered == 0


async def test_xlsx_claim_is_left_alone(db_session, org_pk, deal_pk, ds_pk):
    """A literal XLSX cell is born cited/direct_read ("reading the bytes IS the
    verification"), and an xlsx claim still at `proposed` is not one an exact
    TEXT span could vindicate -- overwriting direct_read with exact_span would
    misreport how the claim was checked."""
    already_cited = await _add(
        db_session,
        **_claim_kwargs(
            org_pk, deal_pk, ds_pk, kind="xlsx", status="cited", verification_method="direct_read"
        ),
    )
    still_proposed = await _add(
        db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk, kind="xlsx", status="proposed")
    )

    summary = await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert (already_cited.status, already_cited.verification_method) == ("cited", "direct_read")
    assert still_proposed.status == "proposed"
    assert summary.claims_considered == 0


async def test_proposed_claim_without_a_span_is_not_promoted(db_session, org_pk, deal_pk, ds_pk):
    """ck_claims_found_requires_span exempts xlsx only, so this shape is not
    reachable for a PDF today -- the span filter is defence in depth: a claim
    can never be promoted on a citation that was never resolved."""
    kwargs = _claim_kwargs(org_pk, deal_pk, ds_pk, kind="xlsx")
    kwargs.update(char_start=None, char_end=None)
    claim = await _add(db_session, **kwargs)

    await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert claim.status == "proposed"


# --------------------------------------------------------------------------
# Scoping and re-runs
# --------------------------------------------------------------------------


async def test_data_source_scoping_leaves_other_documents_alone(
    db_session, owner_conn, org_pk, deal_pk, ds_pk
):
    """The job calls this once per document. A second document's claims are not
    this call's business, even inside the same deal."""
    other_ds = _seed_data_source(owner_conn, org_pk, deal_pk, "appendix.pdf")
    mine = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk))
    theirs = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, other_ds))

    summary = await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert mine.status == "cited"
    assert theirs.status == "proposed"
    assert summary.claims_promoted == 1


async def test_null_data_source_scope_matches_the_demo_ingest_path(
    db_session, org_pk, deal_pk, ds_pk
):
    """scripts/ingest_claims.py leaves data_source_id NULL, and the sandbox
    verification run passes data_source_id=None to match -- that scope must
    select the NULL-document claims and only those."""
    demo = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, None))
    uploaded = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk))

    await promote_exact_span(db_session, data_source_id=None)
    await db_session.flush()

    assert demo.status == "cited"
    assert uploaded.status == "proposed"


async def test_rerun_is_idempotent(db_session, org_pk, deal_pk, ds_pk):
    """No ON CONFLICT needed: filtering on `status == 'proposed'` means the
    second run has nothing left to select."""
    claim = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk))

    first = await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()
    second = await promote_exact_span(db_session, data_source_id=ds_pk)
    await db_session.flush()

    assert (first.claims_considered, first.claims_promoted) == (1, 1)
    assert (second.claims_considered, second.claims_promoted) == (0, 0)
    assert (claim.status, claim.verification_method) == ("cited", "exact_span")


async def test_another_tenants_claims_are_never_promoted(
    db_session, owner_conn, org_pk, deal_pk, ds_pk
):
    """RLS, not a WHERE org_id clause, is what scopes this pass. The other
    tenant's claim is deliberately hung off the SAME data_source_id, so the
    scope filter cannot be what hides it -- only the RLS policy can."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "ON CONFLICT (clerk_org_id) DO NOTHING",
            (_OTHER_ORG_KEY, "Org B"),
        )
        cur.execute("SELECT id FROM organisation WHERE clerk_org_id = %s", (_OTHER_ORG_KEY,))
        other_org = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (other_org, "Other Tenant Deal"),
        )
        other_deal = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO claims (org_id, deal_id, data_source_id, entity, attribute, value, "
            "kind, page, char_start, char_end, status) "
            "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s) RETURNING id",
            (
                other_org,
                other_deal,
                str(ds_pk),
                "Other Corp",
                "revenue",
                '{"normalized": 1}',
                "pdf",
                1,
                10,
                20,
                "proposed",
            ),
        )
        other_claim_id = cur.fetchone()[0]

    try:
        mine = await _add(db_session, **_claim_kwargs(org_pk, deal_pk, ds_pk))

        summary = await promote_exact_span(db_session, data_source_id=ds_pk)
        await db_session.flush()

        assert mine.status == "cited"
        assert summary.claims_considered == 1

        with owner_conn.cursor() as cur:
            cur.execute("SELECT status FROM claims WHERE id = %s", (other_claim_id,))
            assert cur.fetchone()[0] == "proposed"
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM claims WHERE id = %s", (other_claim_id,))
            cur.execute("DELETE FROM deals WHERE id = %s", (other_deal,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (other_org,))
