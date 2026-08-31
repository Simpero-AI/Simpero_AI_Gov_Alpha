"""Contract tests for `intakeStatus` on GET /deals/pipeline (P3-06).

Mirrors tests/test_intake_link_generate.py's ApiTestClient/dependency_overrides
pattern. Link rows are seeded directly as the table owner because `status` is
one-way pending -> terminal at the database level, so `submitted` and
`revoked` rows can only be set up at INSERT time.

The acceptance criteria are all about what collapses to `'none'`: no link,
revoked, expired, and -- the one that needs the shared effective-status helper
rather than the raw column -- a row still stored `pending` past its
`expires_at`.
"""

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_claims
from app.main import app
from app.models.deal_intake_link import DealIntakeLink
from app.services.intake_links import compute_pipeline_intake_status

_FUTURE = datetime.now(UTC) + timedelta(days=7)
_PAST = datetime.now(UTC) - timedelta(hours=1)


def _token_hash(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _claims(tenant_id: str, user_id: str) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "user_id": user_id, "org_role": "admin", "raw_claims": {}}


class ApiTestClient(TestClient):
    """Prepends /api -- every route is mounted there (app/main.py)."""

    def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(url, str) and url.startswith("/") and not url.startswith("/api/"):
            url = f"/api{url}"
        return super().request(method, url, *args, **kwargs)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with ApiTestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_claims, None)


def _authed(tenant_id: str, user_id: str) -> None:
    app.dependency_overrides[get_claims] = lambda: _claims(tenant_id, user_id)


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    """A dedicated org so GET /deals/pipeline (which is org-scoped by RLS,
    not filtered by any argument) returns exactly this test's own deals."""
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Pipeline Intake Status Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "deal_intake_link", "analysis_run", "deals", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def _seed_deal(owner_conn, org_pk: int, name: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute("INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id", (org_pk, name))
        return str(cur.fetchone()[0])


def _seed_link(
    owner_conn,
    org_pk: int,
    clerk_org_id: str,
    deal_id: str,
    *,
    expires_at: datetime,
    seed: str,
    status: str = "pending",
    submitted_at: datetime | None = None,
    created_at: datetime | None = None,
) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (org_id, role, login_method, clerk_user_id, clerk_org_id, status) "
            "VALUES (%s, 'admin', 'email', %s, %s, 'active') RETURNING id",
            (org_pk, f"seed-user-{uuid.uuid4().hex[:8]}", clerk_org_id),
        )
        user_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id, status, submitted_at, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now())) RETURNING id",
            (
                org_pk,
                clerk_org_id,
                deal_id,
                _token_hash(seed),
                "recipient@example.com",
                expires_at,
                user_pk,
                status,
                submitted_at,
                created_at,
            ),
        )
        return str(cur.fetchone()[0])


def _row_for(body: list[dict], deal_id: str) -> dict:
    matches = [row for row in body if row["dealId"] == deal_id]
    assert len(matches) == 1, f"expected exactly one pipeline row for {deal_id}"
    return matches[0]


def test_deal_with_no_link_ever_generated_reads_none(client, owner_conn, seeded_org):
    """The common case: the overwhelming majority of deals never touch this
    feature, and must route exactly as they do in production today."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], "No link deal")
    _authed(seeded_org["clerk_org_id"], "user-pipeline-1")

    response = client.get("/deals/pipeline")

    assert response.status_code == 200
    assert _row_for(response.json(), deal_id)["intakeStatus"] == "none"


def test_live_pending_link_reads_pending(client, owner_conn, seeded_org):
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], "Pending deal")
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        deal_id,
        expires_at=_FUTURE,
        seed="pipeline-pending",
    )
    _authed(seeded_org["clerk_org_id"], "user-pipeline-2")

    body = client.get("/deals/pipeline").json()

    assert _row_for(body, deal_id)["intakeStatus"] == "pending"


def test_submitted_link_reads_submitted_even_past_its_expiry(client, owner_conn, seeded_org):
    """`submitted` is terminal -- expiry does not un-submit it, and Step 3
    must stay reachable for a deal whose answers already arrived."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], "Submitted deal")
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        deal_id,
        expires_at=_PAST,
        seed="pipeline-submitted",
        status="submitted",
        submitted_at=datetime.now(UTC),
    )
    _authed(seeded_org["clerk_org_id"], "user-pipeline-3")

    body = client.get("/deals/pipeline").json()

    assert _row_for(body, deal_id)["intakeStatus"] == "submitted"


@pytest.mark.parametrize(
    ("stored_status", "expires_at", "label"),
    [
        ("revoked", _FUTURE, "revoked"),
        ("expired", _PAST, "expired"),
        # The one that needs the shared effective-status helper rather than
        # the raw column: nothing has written `expired` to this row yet.
        ("pending", _PAST, "stale-pending"),
    ],
)
def test_functionally_dead_links_read_none(
    client, owner_conn, seeded_org, stored_status, expires_at, label
):
    """The grid falls back to normal analysis routing for anything that can
    no longer receive a submission -- it deliberately has no fourth state,
    and must never send the org user to a waiting panel for a dead link."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], f"{label} deal")
    link_id = _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        deal_id,
        expires_at=expires_at,
        seed=f"pipeline-{label}",
        status=stored_status,
    )
    _authed(seeded_org["clerk_org_id"], f"user-pipeline-{label}")

    body = client.get("/deals/pipeline").json()

    assert _row_for(body, deal_id)["intakeStatus"] == "none"

    with owner_conn.cursor() as cur:
        cur.execute("SELECT status FROM deal_intake_link WHERE id = %s", (link_id,))
        assert cur.fetchone()[0] == stored_status, "the grid read must never write status"


def test_reads_the_most_recent_link_not_the_first(client, owner_conn, seeded_org):
    """A reissue leaves the older terminal row behind. The grid must route on
    the link that is actually live, not on a dead predecessor."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], "Reissued deal")
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        deal_id,
        expires_at=_PAST,
        seed="pipeline-superseded",
        status="expired",
        created_at=datetime.now(UTC) - timedelta(days=3),
    )
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        deal_id,
        expires_at=_FUTURE,
        seed="pipeline-reissued",
    )
    _authed(seeded_org["clerk_org_id"], "user-pipeline-4")

    body = client.get("/deals/pipeline").json()

    assert _row_for(body, deal_id)["intakeStatus"] == "pending"


def test_each_deal_gets_its_own_status_in_one_response(client, owner_conn, seeded_org):
    """The batched lookup keys by deal_id -- this is what would catch it
    smearing one deal's link across the whole grid, or dropping the deals
    that have no link at all out of the dict lookup."""
    org_pk, clerk_org_id = seeded_org["org_pk"], seeded_org["clerk_org_id"]
    none_deal = _seed_deal(owner_conn, org_pk, "Grid: none")
    pending_deal = _seed_deal(owner_conn, org_pk, "Grid: pending")
    submitted_deal = _seed_deal(owner_conn, org_pk, "Grid: submitted")
    revoked_deal = _seed_deal(owner_conn, org_pk, "Grid: revoked")

    _seed_link(
        owner_conn, org_pk, clerk_org_id, pending_deal, expires_at=_FUTURE, seed="grid-pending"
    )
    _seed_link(
        owner_conn,
        org_pk,
        clerk_org_id,
        submitted_deal,
        expires_at=_FUTURE,
        seed="grid-submitted",
        status="submitted",
        submitted_at=datetime.now(UTC),
    )
    _seed_link(
        owner_conn,
        org_pk,
        clerk_org_id,
        revoked_deal,
        expires_at=_FUTURE,
        seed="grid-revoked",
        status="revoked",
    )
    _authed(clerk_org_id, "user-pipeline-5")

    body = client.get("/deals/pipeline").json()

    assert _row_for(body, none_deal)["intakeStatus"] == "none"
    assert _row_for(body, pending_deal)["intakeStatus"] == "pending"
    assert _row_for(body, submitted_deal)["intakeStatus"] == "submitted"
    assert _row_for(body, revoked_deal)["intakeStatus"] == "none"


def test_another_tenants_link_never_reaches_this_orgs_grid(client, owner_conn, seeded_org):
    """RLS is exercised in depth by tests/test_intake_link_rls.py; this is the
    HTTP-layer check that the new batched lookup inherits it -- an unscoped
    `IN (...)` query would be exactly the way to lose that."""
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], "This org's deal")
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        deal_id,
        expires_at=_FUTURE,
        seed="own-org-link",
    )

    other_clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (other_clerk_org_id, "Other Org"),
        )
        other_org_pk = cur.fetchone()[0]
    _authed(other_clerk_org_id, "user-other-tenant")

    body = client.get("/deals/pipeline").json()

    assert [row for row in body if row["dealId"] == deal_id] == []

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE org_id = %s", (other_org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (other_org_pk,))


def test_helper_maps_every_status_without_touching_the_database():
    """compute_pipeline_intake_status in isolation -- the four stored
    statuses plus the no-link case, with no session anywhere in its
    signature, which is what keeps this read path incapable of writing."""
    assert compute_pipeline_intake_status(None) == "none"

    for stored, expires_at, expected in (
        ("pending", _FUTURE, "pending"),
        ("pending", _PAST, "none"),
        ("submitted", _FUTURE, "submitted"),
        ("submitted", _PAST, "submitted"),
        ("revoked", _FUTURE, "none"),
        ("expired", _PAST, "none"),
    ):
        link = DealIntakeLink(status=stored, expires_at=expires_at)
        assert compute_pipeline_intake_status(link) == expected, (stored, expires_at)
