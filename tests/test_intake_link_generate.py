"""Contract tests for POST /deals/{deal_id}/intake-link (P3-01, generate/reissue).

Mirrors tests/test_start_analysis_endpoint.py's ApiTestClient/dependency_overrides
pattern against the real app. Covers the acceptance criteria: raw-token exposure
surface, the live-pending-link 409, the lazy-reissue-on-expiry path, the
any-analysis_run 409, normal generation, the compute_intake_link_effective_status
helper's read-only-ness, and a light HTTP-layer tenant-isolation check (RLS
itself is already exercised in depth by tests/test_intake_link_rls.py).
"""

import hashlib
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_claims
from app.main import app
from app.models.deal_intake_link import DealIntakeLink
from app.services.intake_links import compute_intake_link_effective_status

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
            (clerk_org_id, "Intake Link Test Org"),
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
            (seeded_org["org_pk"], "Intake Link Test Deal"),
        )
        return str(cur.fetchone()[0])


@pytest.fixture
def org_a_deal_id(owner_conn, org_a_id) -> str:
    """Same fixture as tests/test_intake_link_rls.py's own -- duplicated here
    per that module's precedent (not shared) since it's only used by the one
    db_session-based unit test below, which needs org_a_id/user_a_id (the
    conftest.py-level org, distinct from this file's own seeded_org/
    seeded_deal used by the HTTP-level tests)."""
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_a_id, "Test Deal"),
        )
        return str(cur.fetchone()[0])


def _seed_pending_link(
    owner_conn,
    org_pk: int,
    clerk_org_id: str,
    deal_id: str,
    expires_at: datetime,
    seed: str,
) -> str:
    """Seeds a deal_intake_link row directly (bypassing the app/route), so
    tests can set up an existing pending link -- live or already past its
    expires_at -- without going through the endpoint first. created_by_user_id
    points at a throwaway user row since the column is NOT NULL."""
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
            "created_by_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                org_pk,
                clerk_org_id,
                deal_id,
                _token_hash(seed),
                "recipient@example.com",
                expires_at,
                user_pk,
            ),
        )
        return str(cur.fetchone()[0])


def _seed_analysis_run(owner_conn, org_pk: int, deal_id: str, status: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status) "
            "VALUES (%s, %s, 'parsing', %s) RETURNING id",
            (org_pk, deal_id, status),
        )
        return str(cur.fetchone()[0])


def _fetch_links(owner_conn, deal_id: str) -> list[dict[str, Any]]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, token_hash FROM deal_intake_link WHERE deal_id = %s",
            (deal_id,),
        )
        cols = ["id", "status", "token_hash"]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


def _fetch_audit_rows(owner_conn, org_pk: int, event_type: str) -> list[dict[str, Any]]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT actor_email, payload FROM human_audit_log "
            "WHERE org_id = %s AND event_type = %s",
            (org_pk, event_type),
        )
        cols = ["actor_email", "payload"]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


# --- (e) normal generation --------------------------------------------------


def test_generate_happy_path(client, owner_conn, seeded_org, seeded_deal):
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        f"/deals/{seeded_deal}/intake-link", json={"recipientEmail": "gp@example.com"}
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert "token" in body and body["token"]
    assert "id" in body
    assert "expiresAt" in body

    rows = _fetch_links(owner_conn, seeded_deal)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    # The stored hash must match the raw token's own sha256 -- proves the row
    # doesn't just happen to have *a* hash, but the hash of the token actually
    # returned to the caller.
    assert rows[0]["token_hash"] == _token_hash(body["token"])

    audit_rows = _fetch_audit_rows(owner_conn, seeded_org["org_pk"], "intake_link_generated")
    assert len(audit_rows) == 1
    assert audit_rows[0]["actor_email"] is None
    assert audit_rows[0]["payload"]["recipient_email"] == "gp@example.com"

    # No 'intake_link_reissued' row on a fresh generation.
    assert _fetch_audit_rows(owner_conn, seeded_org["org_pk"], "intake_link_reissued") == []


def test_questions_snapshot_shape_is_the_wrapper_object(
    client, owner_conn, seeded_org, seeded_deal
):
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deal_intake_questions "
            "(question_key, prompt, input_type, required, display_order, is_active) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            ("company_name", "What is the company's legal name?", "text", True, 0, True),
        )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        f"/deals/{seeded_deal}/intake-link", json={"recipientEmail": "gp@example.com"}
    )
    assert resp.status_code == 201

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT questions_snapshot FROM deal_intake_link WHERE deal_id = %s", (seeded_deal,)
        )
        snapshot = cur.fetchone()[0]

    assert set(snapshot.keys()) == {"snapshot_version", "captured_at", "questions"}
    assert snapshot["snapshot_version"] == 1
    assert isinstance(snapshot["questions"], list)
    assert snapshot["questions"][0]["question_key"] == "company_name"

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deal_intake_questions WHERE question_key = 'company_name'")


# --- (a) raw token appears only in the create response ----------------------


def test_token_field_never_reappears_after_the_create_response(
    client, owner_conn, seeded_org, seeded_deal, caplog
):
    """No GET/list endpoint for intake links exists in this ticket's scope
    (grep of app/api confirms only the POST route references
    app.schemas.intake_link) -- so the only surface to check is the create
    response's own JSON keys and the stored row, which must hold the hash,
    never the raw value."""
    _authed(seeded_org["clerk_org_id"], "user-1")

    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            f"/deals/{seeded_deal}/intake-link", json={"recipientEmail": "gp@example.com"}
        )
    body = resp.json()
    raw_token = body["token"]

    with owner_conn.cursor() as cur:
        cur.execute("SELECT token_hash FROM deal_intake_link WHERE deal_id = %s", (seeded_deal,))
        stored_hash = cur.fetchone()[0]

    assert stored_hash != raw_token
    assert stored_hash == _token_hash(raw_token)

    # deals.py DOES log now (P3-05's _parse_answer reports a malformed stored
    # answers entry by response id), so the original blanket
    # "no logging at all" proxy for "cannot leak the token" no longer holds
    # and would fail for a reason unrelated to token safety. Replaced with the
    # two checks that actually pin the property.
    import inspect
    import re

    from app.api import deals as deals_module

    # 1. The real one the old comment said could not be written: nothing
    #    emitted while minting a link contains the raw token.
    assert raw_token not in caplog.text

    # 2. Static backstop for call sites this request never reaches: no
    #    logger.* call in deals.py interpolates anything token-derived.
    source = inspect.getsource(deals_module)
    log_calls = re.findall(r"logger\.\w+\([^)]*\)", source, re.DOTALL)
    assert log_calls, "expected at least one logger call; update this test if logging was removed"
    assert not [c for c in log_calls if "token" in c.lower()]


# --- (b) second call while a live pending link exists -----------------------


def test_second_call_with_live_pending_link_returns_409_no_second_row(
    client, owner_conn, seeded_org, seeded_deal
):
    _seed_pending_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        _FUTURE,
        "live-pending",
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        f"/deals/{seeded_deal}/intake-link", json={"recipientEmail": "gp@example.com"}
    )

    assert resp.status_code == 409
    rows = _fetch_links(owner_conn, seeded_deal)
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"


# --- (c) lazy reissue when the only existing row is pending but expired -----


def test_reissue_flips_expired_pending_link_and_inserts_new_one(
    client, owner_conn, seeded_org, seeded_deal
):
    old_id = _seed_pending_link(
        owner_conn,
        seeded_org["org_pk"],
        seeded_org["clerk_org_id"],
        seeded_deal,
        _PAST,
        "stale-pending",
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        f"/deals/{seeded_deal}/intake-link", json={"recipientEmail": "gp@example.com"}
    )

    assert resp.status_code == 201
    body = resp.json()

    rows = _fetch_links(owner_conn, seeded_deal)
    assert len(rows) == 2
    by_id = {str(r["id"]): r for r in rows}
    assert by_id[old_id]["status"] == "expired"
    assert by_id[body["id"]]["status"] == "pending"

    audit_rows = _fetch_audit_rows(owner_conn, seeded_org["org_pk"], "intake_link_reissued")
    assert len(audit_rows) == 1
    assert audit_rows[0]["actor_email"] is None

    assert _fetch_audit_rows(owner_conn, seeded_org["org_pk"], "intake_link_generated") == []


# --- (d) any analysis_run row (any status) blocks generation ----------------


@pytest.mark.parametrize("run_status", ["queued", "in_progress", "successful", "failed"])
def test_any_analysis_run_status_blocks_link_generation(
    client, owner_conn, seeded_org, seeded_deal, run_status
):
    _seed_analysis_run(owner_conn, seeded_org["org_pk"], seeded_deal, run_status)
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        f"/deals/{seeded_deal}/intake-link", json={"recipientEmail": "gp@example.com"}
    )

    assert resp.status_code == 409
    assert _fetch_links(owner_conn, seeded_deal) == []


def test_404_when_deal_missing(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        f"/deals/{uuid.uuid4()}/intake-link", json={"recipientEmail": "gp@example.com"}
    )

    assert resp.status_code == 404


# --- (g) tenant isolation sanity check --------------------------------------


def test_cannot_generate_a_link_for_another_orgs_deal(client, owner_conn, seeded_org, seeded_deal):
    """RLS itself (org_isolation on deals/deal_intake_link) is already proven
    in depth by tests/test_deals_rls.py and tests/test_intake_link_rls.py --
    this is a light HTTP-layer check that the route inherits that boundary:
    an org A session naming org B's deal_id gets a 404 (DealRepo.get_by_id is
    RLS-scoped, so the row is invisible), not a 409/201 leaking its existence."""
    other_clerk_org_id = f"test-tenant-other-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (other_clerk_org_id, "Other Org"),
        )
        other_org_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (other_org_pk, "Other Org's Deal"),
        )
        other_deal_id = str(cur.fetchone()[0])

    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.post(
        f"/deals/{other_deal_id}/intake-link", json={"recipientEmail": "gp@example.com"}
    )
    assert resp.status_code == 404

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM deals WHERE id = %s", (other_deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (other_org_pk,))


# --- (f) compute_intake_link_effective_status is pure and read-only --------


def _fake_link(status: str, expires_at: datetime) -> DealIntakeLink:
    link = DealIntakeLink()
    link.status = status
    link.expires_at = expires_at
    return link


def test_pending_future_expiry_is_pending():
    assert compute_intake_link_effective_status(_fake_link("pending", _FUTURE)) == "pending"


def test_pending_past_expiry_is_expired():
    assert compute_intake_link_effective_status(_fake_link("pending", _PAST)) == "expired"


@pytest.mark.parametrize("stored_status", ["submitted", "revoked", "expired"])
@pytest.mark.parametrize("expires_at", [_FUTURE, _PAST])
def test_terminal_statuses_pass_through_regardless_of_expiry(stored_status, expires_at):
    assert compute_intake_link_effective_status(_fake_link(stored_status, expires_at)) == (
        stored_status
    )


async def test_compute_effective_status_does_not_mutate_the_db_row(
    db_session, org_a_id, org_a_deal_id, user_a_id, test_org_id
):
    """db_session's transaction is never committed (it's rolled back at
    fixture teardown, per its own docstring), so re-checking via a separate
    connection (owner_conn) would just see an empty table -- the re-fetch has
    to go through db_session itself, expiring its identity-map cache first so
    a stale in-memory `status` can't paper over a real UPDATE having run."""
    from app.repo.IntakeLinkRepo import IntakeLinkRepo

    repo = IntakeLinkRepo(db_session)
    link = await repo.create(
        {
            "org_id": org_a_id,
            "clerk_org_id": test_org_id,
            "deal_id": org_a_deal_id,
            "token_hash": _token_hash("read-only-check"),
            "recipient_email": "recipient@org-a.example",
            "expires_at": _PAST,
            "created_by_user_id": user_a_id,
        }
    )
    await db_session.flush()
    link_id = link.id

    assert compute_intake_link_effective_status(link) == "expired"

    db_session.expire(link)
    refetched = await repo.get_by_id(link_id)
    assert refetched is not None
    assert refetched.status == "pending"
