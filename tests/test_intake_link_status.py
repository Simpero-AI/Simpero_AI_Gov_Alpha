"""Contract tests for GET /deals/{deal_id}/intake-link (P3-02, status read).

Mirrors tests/test_intake_link_generate.py's ApiTestClient/dependency_overrides
pattern and reuses its direct-seed approach, since this endpoint has to be
exercised against link rows in states (`submitted`, `revoked`, already past
`expires_at`) that no route can currently produce.

Covers the acceptance criteria: no `token_hash` field under ANY status, and a
row stored `pending` but past `expires_at` reading as `expired` before any
generate call has run P3-01's lazy-expire write.
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
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Intake Link Status Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "deal_intake_link", "analysis_run", "deals", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Intake Link Status Test Deal"),
        )
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
    recipient_email: str = "recipient@example.com",
    created_at: datetime | None = None,
) -> str:
    """Seeds a deal_intake_link row directly as the table owner. Needed
    because `status` is one-way pending -> terminal at the DB level (the
    trg_deal_intake_link_one_way_status trigger) and no route yet writes
    `submitted`, so `submitted`/`revoked` rows can only be set up at INSERT
    time, not by transitioning a row the endpoint created."""
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
                recipient_email,
                expires_at,
                user_pk,
                status,
                submitted_at,
                created_at,
            ),
        )
        return str(cur.fetchone()[0])


def test_returns_effective_status_and_recipient_for_a_live_pending_link(
    client, owner_conn, seeded_org, seeded_deal
):
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_FUTURE,
        seed="live-pending",
        recipient_email="founder@example.com",
    )
    _authed(seeded_org["clerk_org_id"], "user-status-1")

    response = client.get(f"/deals/{seeded_deal}/intake-link")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending"
    assert body["recipientEmail"] == "founder@example.com"
    assert body["submittedAt"] is None
    assert body["expiresAt"] is not None


def test_pending_row_past_expires_at_reads_expired_without_any_write(
    client, owner_conn, seeded_org, seeded_deal
):
    """The ticket's headline acceptance criterion: the effective status must
    not lag the stored column. The row stays stored `pending` afterwards --
    P3-01's generate call is the only thing that ever persists `expired`."""
    link_id = _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_PAST,
        seed="stale-pending",
    )
    _authed(seeded_org["clerk_org_id"], "user-status-2")

    response = client.get(f"/deals/{seeded_deal}/intake-link")

    assert response.status_code == 200
    assert response.json()["status"] == "expired"

    with owner_conn.cursor() as cur:
        cur.execute("SELECT status FROM deal_intake_link WHERE id = %s", (link_id,))
        assert cur.fetchone()[0] == "pending", "P3-02 must never write the terminal status"


@pytest.mark.parametrize(
    ("stored_status", "expires_at", "expected"),
    [
        ("pending", _FUTURE, "pending"),
        ("pending", _PAST, "expired"),
        ("submitted", _FUTURE, "submitted"),
        ("submitted", _PAST, "submitted"),
        ("revoked", _FUTURE, "revoked"),
        ("revoked", _PAST, "revoked"),
        ("expired", _PAST, "expired"),
    ],
)
def test_no_token_hash_field_under_any_status(
    client, owner_conn, seeded_org, seeded_deal, stored_status, expires_at, expected
):
    """The other acceptance criterion, asserted across every status the column
    can hold -- and, on the terminal rows, that a past expires_at does NOT
    rewrite an already-terminal status to `expired`."""
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=expires_at,
        seed=f"status-{stored_status}-{expires_at.isoformat()}",
        status=stored_status,
        submitted_at=datetime.now(UTC) if stored_status == "submitted" else None,
    )
    _authed(seeded_org["clerk_org_id"], f"user-{stored_status}")

    response = client.get(f"/deals/{seeded_deal}/intake-link")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == expected
    assert set(body) == {"status", "recipientEmail", "expiresAt", "submittedAt"}
    assert "tokenHash" not in body
    assert "token_hash" not in body
    assert "token" not in body


def test_reports_the_most_recent_link_not_the_first(client, owner_conn, seeded_org, seeded_deal):
    """A reissue (P3-01) leaves the older, now-terminal row behind. The panel
    must describe the link that is actually live, not the dead one."""
    older = datetime.now(UTC) - timedelta(days=3)
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_PAST,
        seed="superseded",
        status="expired",
        recipient_email="old@example.com",
        created_at=older,
    )
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_FUTURE,
        seed="reissued",
        recipient_email="new@example.com",
    )
    _authed(seeded_org["clerk_org_id"], "user-status-3")

    response = client.get(f"/deals/{seeded_deal}/intake-link")

    assert response.status_code == 200
    body = response.json()
    assert body["recipientEmail"] == "new@example.com"
    assert body["status"] == "pending"


def test_404_when_the_deal_has_never_had_a_link(client, seeded_org, seeded_deal):
    _authed(seeded_org["clerk_org_id"], "user-status-4")

    response = client.get(f"/deals/{seeded_deal}/intake-link")

    assert response.status_code == 404


def test_404_for_an_unknown_deal(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-status-5")

    response = client.get(f"/deals/{uuid.uuid4()}/intake-link")

    assert response.status_code == 404


def test_another_tenant_cannot_read_the_link_status(client, owner_conn, seeded_org, seeded_deal):
    """RLS is exercised in depth by tests/test_intake_link_rls.py; this is the
    light HTTP-layer check that the endpoint inherits it rather than reading
    around it -- the deal itself is invisible to the other tenant, so the
    handler's own deal lookup 404s first."""
    _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_FUTURE,
        seed="cross-tenant",
    )
    other_clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now())",
            (other_clerk_org_id, "Other Org"),
        )
    _authed(other_clerk_org_id, "user-other-tenant")

    response = client.get(f"/deals/{seeded_deal}/intake-link")

    assert response.status_code == 404

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE clerk_org_id = %s", (other_clerk_org_id,))
        cur.execute("DELETE FROM organisation WHERE clerk_org_id = %s", (other_clerk_org_id,))
