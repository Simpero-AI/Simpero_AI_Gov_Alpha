"""Contract tests for DELETE /deals/{deal_id}/intake-link (P3-03, revoke).

Mirrors tests/test_intake_link_generate.py's ApiTestClient/dependency_overrides
pattern. The acceptance criterion -- "a revoked link's token immediately fails
the keyhole policy" -- is asserted end-to-end rather than by proxy: the test
drives the real endpoint, then opens a `dd_public` session with
`app.intake_token_hash` set to that link's hash and checks the row has gone
dark, which is tests/test_intake_keyhole_policies.py's own pattern applied to
a revoke the API actually performed.
"""

import hashlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

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
            (clerk_org_id, "Intake Link Revoke Test Org"),
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
            (seeded_org["org_pk"], "Intake Link Revoke Test Deal"),
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
) -> dict[str, str]:
    """Seeds a deal_intake_link row directly as the table owner -- `status` is
    one-way pending -> terminal at the DB level, so a `submitted` or already-
    `revoked` row can only be set up at INSERT time."""
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
            "created_by_user_id, status, submitted_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
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
            ),
        )
        return {"id": str(cur.fetchone()[0]), "token_hash": _token_hash(seed)}


async def _set_guc(session, name: str, value: str) -> None:
    await session.execute(text(f"SELECT set_config('{name}', :v, true)"), {"v": value})


def _stored_status(owner_conn, link_id: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT status FROM deal_intake_link WHERE id = %s", (link_id,))
        return cur.fetchone()[0]


def test_revokes_the_pending_link_and_writes_one_audit_row(
    client, owner_conn, seeded_org, seeded_deal
):
    link = _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_FUTURE,
        seed="to-revoke",
    )
    _authed(seeded_org["clerk_org_id"], "user-revoke-1")

    response = client.delete(f"/deals/{seeded_deal}/intake-link")

    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert _stored_status(owner_conn, link["id"]) == "revoked"

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, actor_email, deal_id, payload FROM human_audit_log "
            "WHERE org_id = %s AND event_type = 'intake_link_revoked'",
            (seeded_org["org_pk"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, "exactly one audit row per revoke"
    event_type, actor_email, deal_id, payload = rows[0]
    assert event_type == "intake_link_revoked"
    # Q4/PO review pins actor_email NULL for this event -- the org user is
    # already identified by the link row's created_by_user_id.
    assert actor_email is None
    assert str(deal_id) == seeded_deal
    assert payload["intake_link_id"] == link["id"]


async def test_revoked_links_token_immediately_fails_the_keyhole_policy(
    client, owner_conn, seeded_org, seeded_deal, public_db_session
):
    """The ticket's acceptance criterion. intake_token_lookup's predicate
    includes `status = 'pending'`, so the revoke closes the keyhole at the
    policy layer -- the external recipient's token stops resolving to a row
    at all, not merely stops being honoured by application code."""
    link = _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_FUTURE,
        seed="keyhole-revoke",
    )
    _authed(seeded_org["clerk_org_id"], "user-revoke-2")

    await _set_guc(public_db_session, "app.intake_token_hash", link["token_hash"])
    visible_before = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert [str(r[0]) for r in visible_before.fetchall()] == [link["id"]], (
        "precondition: the token resolves while the link is pending"
    )

    assert client.delete(f"/deals/{seeded_deal}/intake-link").status_code == 200

    # A fresh statement in the same dd_public session -- the GUC is unchanged
    # and the token is unchanged; only the row's status moved.
    visible_after = await public_db_session.execute(text("SELECT id FROM deal_intake_link"))
    assert visible_after.fetchall() == []


def test_second_revoke_404s_rather_than_re_revoking(client, owner_conn, seeded_org, seeded_deal):
    """Nothing is left pending after the first call, so there is no row to
    find. This is also what stops a double-click reaching
    trg_deal_intake_link_one_way_status as an unhandled 500."""
    link = _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_FUTURE,
        seed="double-revoke",
    )
    _authed(seeded_org["clerk_org_id"], "user-revoke-3")

    assert client.delete(f"/deals/{seeded_deal}/intake-link").status_code == 200
    assert client.delete(f"/deals/{seeded_deal}/intake-link").status_code == 404
    assert _stored_status(owner_conn, link["id"]) == "revoked"

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM human_audit_log "
            "WHERE org_id = %s AND event_type = 'intake_link_revoked'",
            (seeded_org["org_pk"],),
        )
        assert cur.fetchone()[0] == 1, "the failed second revoke must not audit"


def test_pending_row_past_expires_at_is_409_not_revoked(
    client, owner_conn, seeded_org, seeded_deal
):
    """A functionally dead link is not revocable. Flipping it to `revoked`
    would write a misleading audit row and take the lazy-expire path away
    from P3-01, which the plan pins as the only writer of `expired`."""
    link = _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_PAST,
        seed="stale-pending-revoke",
    )
    _authed(seeded_org["clerk_org_id"], "user-revoke-4")

    response = client.delete(f"/deals/{seeded_deal}/intake-link")

    assert response.status_code == 409
    assert _stored_status(owner_conn, link["id"]) == "pending"

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM human_audit_log "
            "WHERE org_id = %s AND event_type = 'intake_link_revoked'",
            (seeded_org["org_pk"],),
        )
        assert cur.fetchone()[0] == 0


def test_submitted_link_cannot_be_revoked(client, owner_conn, seeded_org, seeded_deal):
    link = _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_FUTURE,
        seed="already-submitted",
        status="submitted",
        submitted_at=datetime.now(UTC),
    )
    _authed(seeded_org["clerk_org_id"], "user-revoke-5")

    assert client.delete(f"/deals/{seeded_deal}/intake-link").status_code == 404
    assert _stored_status(owner_conn, link["id"]) == "submitted"


def test_404_when_the_deal_has_never_had_a_link(client, seeded_org, seeded_deal):
    _authed(seeded_org["clerk_org_id"], "user-revoke-6")

    assert client.delete(f"/deals/{seeded_deal}/intake-link").status_code == 404


def test_404_for_an_unknown_deal(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-revoke-7")

    assert client.delete(f"/deals/{uuid.uuid4()}/intake-link").status_code == 404


def test_another_tenant_cannot_revoke_the_link(client, owner_conn, seeded_org, seeded_deal):
    """RLS is exercised in depth by tests/test_intake_link_rls.py; this is the
    HTTP-layer check that the revoke path inherits it -- the deal itself is
    invisible to the other tenant, so the handler 404s on its own deal lookup
    and never reaches the UPDATE."""
    link = _seed_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        expires_at=_FUTURE,
        seed="cross-tenant-revoke",
    )
    other_clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now())",
            (other_clerk_org_id, "Other Org"),
        )
    _authed(other_clerk_org_id, "user-other-tenant")

    assert client.delete(f"/deals/{seeded_deal}/intake-link").status_code == 404
    assert _stored_status(owner_conn, link["id"]) == "pending"

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE clerk_org_id = %s", (other_clerk_org_id,))
        cur.execute("DELETE FROM organisation WHERE clerk_org_id = %s", (other_clerk_org_id,))
