"""Admin portal (app/api/admin) tests — Phase 6.

Hits the real app through TestClient with `get_claims` overridden (skips
real Clerk verification) — `get_admin_db` runs for real: SET LOCAL, JIT
provisioning of clerk_admin_users, RLS, the D3 downgrade sync, the lot.
Every Clerk Backend API call is mocked (monkeypatched on the importing
module) — this suite never makes a real network call to Clerk.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.api.admin.invitations as invitations_mod
import app.api.admin.members as members_mod
import app.api.admin.organizations as organizations_mod
import app.api.admin.platform_invitations as platform_invitations_mod
import app.api.admin.platform_members as platform_members_mod
import app.api.admin.platform_organization_delete as platform_org_delete_mod
import app.core.admin_dependencies as admin_deps_mod
from app.core.database import AsyncSessionLocal
from app.core.dependencies import _ensure_user_provisioned, get_claims
from app.main import app
from app.repo.AdminUserRepo import AdminUserRepo

# --- shared helpers ---------------------------------------------------------


def _claims(tenant_id: str, user_id: str, org_role: str | None = "admin") -> dict[str, Any]:
    return {"tenant_id": tenant_id, "user_id": user_id, "org_role": org_role, "raw_claims": {}}


class ApiTestClient(TestClient):
    """Prepends /api — mirrors tests/test_phase1_endpoints.py's idiom."""

    def request(self, method: str, url: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(url, str) and url.startswith("/") and not url.startswith("/api/"):
            url = f"/api{url}"
        return super().request(method, url, *args, **kwargs)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with ApiTestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_claims, None)


def _authed(tenant_id: str, user_id: str, org_role: str | None = "admin") -> None:
    app.dependency_overrides[get_claims] = lambda: _claims(tenant_id, user_id, org_role)


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    """A fresh org, seeded directly (bypasses RLS) — same idiom as
    test_phase1_endpoints.seeded_org. Pre-seeding means _ensure_org_provisioned
    never needs a real Clerk org lookup for these tests."""
    clerk_org_id = f"test-admin-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Admin Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "clerk_admin_users", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


def _seed_admin(owner_conn, org: dict[str, Any], clerk_user_id: str, **fields: Any) -> None:
    columns = {
        "clerk_user_id": clerk_user_id,
        "clerk_org_id": org["clerk_org_id"],
        "org_id": org["org_pk"],
        "admin_type": "client",
        "status": "active",
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO clerk_admin_users ({cols}, created_at) VALUES ({placeholders}, now())",
            list(columns.values()),
        )


def _admin_status(owner_conn, clerk_user_id: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM clerk_admin_users WHERE clerk_user_id = %s", (clerk_user_id,)
        )
        return cur.fetchone()[0]


def _http_status_error(
    status_code: int, body: dict[str, Any] | None = None
) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.clerk.com/v1/x")
    response = httpx.Response(status_code, json=body or {}, request=request)
    return httpx.HTTPStatusError("clerk error", request=request, response=response)


def _fake_invitation(
    email: str, role: str = "org:member", inv_id: str | None = None, status: str = "pending"
) -> dict[str, Any]:
    return {
        "id": inv_id or f"inv_{uuid.uuid4().hex[:8]}",
        "email_address": email,
        "role": role,
        "status": status,
        "created_at": 1700000000000,
    }


def _fake_clerk_org(clerk_org_id: str, name: str, org_type: str | None = None) -> dict[str, Any]:
    return {
        "id": clerk_org_id,
        "name": name,
        "public_metadata": {"type": org_type} if org_type else {},
        "created_at": 1700000000000,
    }


@pytest.fixture
def platform_org(monkeypatch, owner_conn) -> Iterator[dict[str, Any]]:
    """Seeds a local org standing in for the Simpero platform org and points
    settings.simpero_platform_org_id at it. All admin modules share the same
    lru_cache'd Settings instance, so patching one attribute here is visible
    to admin_dependencies/organizations/platform_invitations alike."""
    clerk_org_id = f"test-platform-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Simpero Platform"),
        )
        org_pk = cur.fetchone()[0]
    monkeypatch.setattr(admin_deps_mod.settings, "simpero_platform_org_id", clerk_org_id)

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "clerk_admin_users", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


# --- Phase 1: guard tests (table-authoritative) -----------------------------


def test_guard_seeded_active_row_passes(client, monkeypatch, owner_conn, seeded_org):
    monkeypatch.setattr(members_mod, "list_organization_memberships", _async_return([]))
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id)
    _authed(seeded_org["clerk_org_id"], user_id, "admin")

    resp = client.get("/admin/members")
    assert resp.status_code == 200


def test_guard_inactive_row_denied(client, owner_conn, seeded_org):
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id, status="inactive")
    # org_role "member" (not "admin"): _ensure_admin_provisioned's
    # reactivation path only fires when the JWT currently says admin (see
    # test_admin_provisioned_reactivates_inactive_row_on_readmit_jwt for
    # that case) — an inactive row with a non-admin JWT just stays inactive
    # and the guard denies.
    _authed(seeded_org["clerk_org_id"], user_id, "member")

    resp = client.get("/admin/members")
    assert resp.status_code == 403
    assert _admin_status(owner_conn, user_id) == "inactive"


def test_guard_no_row_denied(client, seeded_org):
    # A brand-new, non-admin caller: _ensure_admin_provisioned's JIT-create
    # branch no-ops (not admin per Clerk) -> no row -> guard 403.
    _authed(seeded_org["clerk_org_id"], f"user-{uuid.uuid4().hex[:8]}", "member")

    resp = client.get("/admin/members")
    assert resp.status_code == 403


def test_guard_wrong_admin_type_denied(client, owner_conn, platform_org):
    # Data inconsistency seeded directly: a "client"-typed row inside the
    # platform org — require_platform_admin must cross-check admin_type,
    # not just tenant_id.
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(
        owner_conn,
        {"clerk_org_id": platform_org["clerk_org_id"], "org_pk": platform_org["org_pk"]},
        user_id,
        admin_type="client",
    )
    _authed(platform_org["clerk_org_id"], user_id, "admin")

    resp = client.get("/admin/organizations")
    assert resp.status_code == 403


def test_guard_tenant_mismatch_denied(client, owner_conn, seeded_org, platform_org):
    # A "platform"-typed row seeded under a non-platform org (inconsistent
    # data) — require_platform_admin must also cross-check tenant_id, not
    # just admin_type.
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id, admin_type="platform")
    _authed(seeded_org["clerk_org_id"], user_id, "admin")

    resp = client.get("/admin/organizations")
    assert resp.status_code == 403


def test_guard_platform_org_unconfigured_denied(client, monkeypatch, owner_conn, seeded_org):
    monkeypatch.setattr(admin_deps_mod.settings, "simpero_platform_org_id", "")
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id, admin_type="platform")
    _authed(seeded_org["clerk_org_id"], user_id, "admin")

    resp = client.get("/admin/organizations")
    assert resp.status_code == 403


# --- Phase 1: JIT provisioning ----------------------------------------------


def test_jit_creates_admin_row_no_users_row(client, monkeypatch, owner_conn, seeded_org):
    monkeypatch.setattr(
        admin_deps_mod, "fetch_clerk_user_primary_email", _async_return("jane@example.com")
    )
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _authed(seeded_org["clerk_org_id"], user_id, "admin")

    resp = client.get("/admin/context")
    assert resp.status_code == 200
    assert resp.json()["isOrgAdmin"] is True

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT admin_type, status, email FROM clerk_admin_users WHERE clerk_user_id = %s",
            (user_id,),
        )
        admin_type, status, email = cur.fetchone()
        assert admin_type == "client"
        assert status == "active"
        assert email == "jane@example.com"

        cur.execute("SELECT id FROM users WHERE clerk_user_id = %s", (user_id,))
        assert cur.fetchone() is None  # admins are admin-only — no product users row


def test_jit_member_token_creates_no_admin_row(client, seeded_org):
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _authed(seeded_org["clerk_org_id"], user_id, "member")

    resp = client.get("/admin/context")
    assert resp.status_code == 200  # /context has no guard
    assert resp.json()["isOrgAdmin"] is False

    resp2 = client.get("/admin/members")
    assert resp2.status_code == 403


def test_jit_clerk_unreachable_provisions_with_null_email(
    client, monkeypatch, owner_conn, seeded_org
):
    monkeypatch.setattr(admin_deps_mod, "fetch_clerk_user_primary_email", _async_return(None))
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _authed(seeded_org["clerk_org_id"], user_id, "admin")

    resp = client.get("/admin/context")
    assert resp.status_code == 200  # request succeeds despite the unreachable lookup

    with owner_conn.cursor() as cur:
        cur.execute("SELECT email FROM clerk_admin_users WHERE clerk_user_id = %s", (user_id,))
        assert cur.fetchone()[0] is None


def _async_return(value: Any):
    async def _fn(*args: Any, **kwargs: Any) -> Any:
        return value

    return _fn


# --- Phase 1: R6 downgrade-only sync -----------------------------------------


def test_r6_downgrade_sync_revokes_on_demotion(client, owner_conn, seeded_org):
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id)  # active

    _authed(seeded_org["clerk_org_id"], user_id, "member")  # demoted per Clerk
    resp = client.get("/admin/members")
    assert resp.status_code == 403
    assert _admin_status(owner_conn, user_id) == "inactive"


def test_r6_active_row_stays_active_with_admin_token(client, monkeypatch, owner_conn, seeded_org):
    monkeypatch.setattr(members_mod, "list_organization_memberships", _async_return([]))
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id)

    _authed(seeded_org["clerk_org_id"], user_id, "admin")
    resp = client.get("/admin/members")
    assert resp.status_code == 200
    assert _admin_status(owner_conn, user_id) == "active"


def test_admin_provisioned_reactivates_inactive_row_on_readmit_jwt(
    client, monkeypatch, owner_conn, seeded_org
):
    monkeypatch.setattr(members_mod, "list_organization_memberships", _async_return([]))
    # Was test_r6_inactive_row_never_reactivated: that name asserted the OLD
    # invariant ("D3 is revoke-only, an inactive row is never re-activated by
    # the passive JIT sync"). _ensure_admin_provisioned now DOES reactivate
    # on a fresh org_role=admin JWT — closing the gap where a re-invited
    # admin stayed stuck inactive until they happened to hit
    # PATCH /admin/members themselves (which they can't, since they're
    # locked out). See app/core/admin_dependencies.py::_ensure_admin_provisioned.
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id, status="inactive")
    with owner_conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM clerk_admin_users WHERE clerk_user_id = %s", (user_id,))
        old_updated_at = cur.fetchone()[0]

    _authed(seeded_org["clerk_org_id"], user_id, "admin")  # re-invited per Clerk
    resp = client.get("/admin/members")
    assert resp.status_code == 200  # reactivated same-request, guard passes
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status, admin_type, updated_at FROM clerk_admin_users WHERE clerk_user_id = %s",
            (user_id,),
        )
        status, admin_type, updated_at = cur.fetchone()
        assert status == "active"
        assert admin_type == "client"
        assert updated_at is not None
        assert old_updated_at is None or updated_at > old_updated_at


def test_context_is_org_admin_reflects_d3_sync_not_stale_jwt(client, owner_conn, seeded_org):
    # Same demotion setup as test_r6_downgrade_sync_revokes_on_demotion, but
    # against /admin/context (no guard) — proves isOrgAdmin reads the
    # clerk_admin_users row get_admin_db just synced this request, not the
    # demoted token's stale org_role claim.
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id)  # active

    _authed(seeded_org["clerk_org_id"], user_id, "member")  # demoted per Clerk
    resp = client.get("/admin/context")
    assert resp.status_code == 200
    assert resp.json()["isOrgAdmin"] is False
    assert _admin_status(owner_conn, user_id) == "inactive"


def test_context_platform_admin_is_also_org_admin(client, owner_conn, platform_org):
    # Per the plan's "harmless overlap" note: a platform admin's own row
    # also satisfies require_org_admin's check, and /admin/context must
    # match that — not filter by admin_type.
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], user_id, "admin")

    resp = client.get("/admin/context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["isOrgAdmin"] is True
    assert body["isPlatformAdmin"] is True


def test_context_platform_org_member_without_admin_row_is_not_platform_admin(
    client, owner_conn, platform_org
):
    # Regression: isPlatformAdmin must not be a bare tenant-id match. A caller
    # whose active Clerk org is the platform org, but who was never
    # provisioned as an admin (org_role "member" -> _ensure_admin_provisioned's
    # JIT-create branch is skipped, fail-closed, no row created), must read
    # isPlatformAdmin: false.
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _authed(platform_org["clerk_org_id"], user_id, "member")

    resp = client.get("/admin/context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["isPlatformAdmin"] is False
    assert body["isOrgAdmin"] is False


def test_context_platform_org_client_admin_is_not_platform_admin(client, owner_conn, platform_org):
    # Same regression, other branch: an active clerk_admin_users row whose
    # org happens to be the platform org, but whose admin_type is "client"
    # (not "platform"), must also read isPlatformAdmin: false.
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, user_id, admin_type="client")
    _authed(platform_org["clerk_org_id"], user_id, "admin")

    resp = client.get("/admin/context")
    assert resp.status_code == 200
    body = resp.json()
    assert body["isPlatformAdmin"] is False
    assert body["isOrgAdmin"] is True


# --- RLS caveat: cross-org isolation -----------------------------------------


def test_members_never_returns_other_org_rows(client, monkeypatch, owner_conn, seeded_org):
    monkeypatch.setattr(members_mod, "list_organization_memberships", _async_return([]))
    org_b_clerk_id = f"test-admin-org-b-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (org_b_clerk_id, "Org B"),
        )
        org_b_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (org_id, clerk_user_id, clerk_org_id, role, login_method) "
            "VALUES (%s, %s, %s, 'member', 'clerk') RETURNING id",
            (org_b_pk, f"org-b-user-{uuid.uuid4().hex[:8]}", org_b_clerk_id),
        )
        cur.execute(
            "INSERT INTO clerk_admin_users (clerk_user_id, clerk_org_id, org_id, admin_type, "
            "status, created_at) VALUES (%s, %s, %s, 'client', 'active', now())",
            (f"org-b-admin-{uuid.uuid4().hex[:8]}", org_b_clerk_id, org_b_pk),
        )

    try:
        admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
        _seed_admin(owner_conn, seeded_org, admin_user_id)
        _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

        resp = client.get("/admin/members")
        assert resp.status_code == 200
        assert resp.json() == []  # org B's user row never visible to org A's admin
    finally:
        # try/finally (not a bare trailing block): a failed assertion above
        # must not leak org B's rows into the shared test DB permanently.
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE org_id = %s", (org_b_pk,))
            cur.execute("DELETE FROM clerk_admin_users WHERE org_id = %s", (org_b_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


# --- Phase 2: members --------------------------------------------------------


def _seed_member(owner_conn, org: dict[str, Any], **fields: Any) -> tuple[int, str]:
    columns = {
        "org_id": org["org_pk"],
        "clerk_user_id": f"member-{uuid.uuid4().hex[:8]}",
        "clerk_org_id": org["clerk_org_id"],
        "role": "member",
        "login_method": "clerk",
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO users ({cols}) VALUES ({placeholders}) RETURNING id",
            list(columns.values()),
        )
        return cur.fetchone()[0], columns["clerk_user_id"]


def test_list_members_returns_only_caller_org(client, monkeypatch, owner_conn, seeded_org):
    _, member_clerk_id = _seed_member(owner_conn, seeded_org, name="Jane", email="jane@example.com")

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(member_clerk_id, "jane@example.com")]

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.get("/admin/members")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["email"] == "jane@example.com"


def test_list_members_shows_live_role_not_stale_local_role(
    client, monkeypatch, owner_conn, seeded_org
):
    # A role change made directly in the Clerk Dashboard (bypassing this app)
    # never reaches users.role — GET /admin/members must show the live Clerk
    # role, not the stale local one.
    _, member_clerk_id = _seed_member(
        owner_conn, seeded_org, role="member", email="stale@example.com"
    )

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(member_clerk_id, "stale@example.com", role="org:admin")]

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.get("/admin/members")
    assert resp.status_code == 200
    [member] = resp.json()
    assert member["role"] == "org:admin"


def test_list_members_includes_soft_deleted_users_with_status(
    client, monkeypatch, owner_conn, seeded_org
):
    # Was test_list_members_excludes_soft_deleted_users: product decision
    # reversed to keep inactive members visible so admins can see who's been
    # removed and re-invite them from the same screen (see list_members).
    _, active_clerk_id = _seed_member(owner_conn, seeded_org, email="active@example.com")
    _seed_member(
        owner_conn,
        seeded_org,
        email="inactive@example.com",
        status="inactive",
        deactivated_at=datetime.now(UTC).replace(tzinfo=None),
    )

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(active_clerk_id, "active@example.com")]

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.get("/admin/members")
    assert resp.status_code == 200
    body = {m["email"]: m["status"] for m in resp.json()}
    assert body == {"active@example.com": "active", "inactive@example.com": "inactive"}


def test_list_members_dedups_local_inactive_row_present_in_live_clerk(
    client, monkeypatch, owner_conn, seeded_org
):
    # Someone was removed, then re-invited: their Clerk membership is active
    # again but they haven't logged back in yet, so their local `users` row
    # is still "inactive". Live Clerk data wins — the row must appear once,
    # as active, not twice. Own-org counterpart of
    # test_platform_members_list_dedups_local_inactive_row_present_in_live_clerk.
    readmitted_clerk_user_id = f"member-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (org_id, clerk_user_id, clerk_org_id, role, login_method, "
            "name, email, status, deactivated_at) VALUES (%s, %s, %s, 'member', 'clerk', "
            "%s, %s, 'inactive', now())",
            (
                seeded_org["org_pk"],
                readmitted_clerk_user_id,
                seeded_org["clerk_org_id"],
                "Readmitted Bob",
                "bob@example.com",
            ),
        )

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(readmitted_clerk_user_id, "bob@example.com")]

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.get("/admin/members")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1  # not duplicated
    assert body[0]["userId"] == readmitted_clerk_user_id
    assert body[0]["status"] == "active"  # live Clerk wins over the stale local row


def test_remove_member_writes_audit_and_soft_deletes_row(
    client, monkeypatch, owner_conn, seeded_org
):
    calls: list[tuple[str, str]] = []
    removed_ids: set[str] = set()

    async def _fake_remove(org_id: str, member_user_id: str) -> None:
        calls.append((org_id, member_user_id))
        removed_ids.add(member_user_id)

    monkeypatch.setattr(members_mod, "remove_organization_membership", _fake_remove)
    monkeypatch.setattr(
        members_mod, "fetch_clerk_user_primary_email", _async_return("bob@example.com")
    )

    member_pk, member_clerk_id = _seed_member(owner_conn, seeded_org, email="bob@example.com")

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        # After removal, the target's live Clerk membership is gone — the
        # second GET below (which re-lists) must fall back to the local
        # soft-deleted row, same dedup rule as list_members's docstring.
        if member_clerk_id in removed_ids:
            return []
        return [_fake_membership(member_clerk_id, "bob@example.com", role="org:member")]

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.delete(f"/admin/members/{member_clerk_id}")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert calls == [(seeded_org["clerk_org_id"], member_clerk_id)]

    with owner_conn.cursor() as cur:
        cur.execute("SELECT status, deactivated_at FROM users WHERE id = %s", (member_pk,))
        row = cur.fetchone()
        assert row is not None  # soft-delete: row still present, not gone
        status, deactivated_at = row
        assert status == "inactive"
        assert deactivated_at is not None
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_member_removed' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        payload = cur.fetchone()[0]
        assert payload["removed_clerk_user_id"] == member_clerk_id
        assert payload["removed_email"] == "bob@example.com"
        assert payload["admin_role_deactivated"] is False
        assert payload["clerk_membership_revoked"] is True

    # Removed member stays visible in the list, now with status "inactive"
    # (list_members no longer filters soft-deleted rows out).
    resp = client.get("/admin/members")
    [member] = resp.json()
    assert member["email"] == "bob@example.com"
    assert member["status"] == "inactive"


def test_remove_member_deactivates_active_admin_row(client, monkeypatch, owner_conn, seeded_org):
    # A member promoted via PATCH /admin/members/{user_id} holds both an
    # active `users` row and an active `clerk_admin_users` row. Deleting them
    # here must not leave the admin row active with dangling /admin access.
    calls: list[tuple[str, str]] = []

    async def _fake_remove(org_id: str, member_user_id: str) -> None:
        calls.append((org_id, member_user_id))

    monkeypatch.setattr(members_mod, "remove_organization_membership", _fake_remove)
    monkeypatch.setattr(
        members_mod, "fetch_clerk_user_primary_email", _async_return("dual@example.com")
    )

    caller_admin_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, caller_admin_id)  # a 2nd active admin, so
    # removing the target below doesn't trip the last-active-admin guard.

    target_pk, target_clerk_id = _seed_member(owner_conn, seeded_org, email="dual@example.com")
    _seed_admin(owner_conn, seeded_org, target_clerk_id)  # dual-state: also an active admin

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(target_clerk_id, "dual@example.com", role="org:member")]

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)

    _authed(seeded_org["clerk_org_id"], caller_admin_id, "admin")
    resp = client.delete(f"/admin/members/{target_clerk_id}")
    assert resp.status_code == 200
    assert calls == [(seeded_org["clerk_org_id"], target_clerk_id)]

    with owner_conn.cursor() as cur:
        cur.execute("SELECT status, deactivated_at FROM users WHERE id = %s", (target_pk,))
        row = cur.fetchone()
        assert row is not None  # soft-delete: row still present
        assert row[0] == "inactive"
        assert row[1] is not None
        cur.execute(
            "SELECT status, updated_at, created_at FROM clerk_admin_users WHERE clerk_user_id = %s",
            (target_clerk_id,),
        )
        status, updated_at, created_at = cur.fetchone()
        assert status == "inactive"
        assert updated_at is not None
        assert updated_at >= created_at
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_member_removed' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        payloads = [row[0] for row in cur.fetchall()]
        assert any(p["removed_clerk_user_id"] == target_clerk_id for p in payloads)
        assert next(
            p["admin_role_deactivated"]
            for p in payloads
            if p["removed_clerk_user_id"] == target_clerk_id
        )


def test_remove_member_admin_with_no_local_user_row(client, monkeypatch, owner_conn, seeded_org):
    # Own-org counterpart of test_remove_org_member_admin_with_no_local_user_row:
    # an admin-only identity (never logged into the product, so no `users`
    # row) must still be removable — the old local-int-id lookup would have
    # 404'd here.
    target_user_id = f"member-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, target_user_id)  # admin-only, no `users` row
    caller_admin_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, caller_admin_id)  # 2nd active admin, guard doesn't trip

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(target_user_id, "target@example.com", role="org:admin")]

    calls: list[tuple[str, str]] = []

    async def _fake_remove(org_id: str, member_user_id: str) -> None:
        calls.append((org_id, member_user_id))

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    monkeypatch.setattr(members_mod, "remove_organization_membership", _fake_remove)
    monkeypatch.setattr(
        members_mod, "fetch_clerk_user_primary_email", _async_return("target@example.com")
    )
    _authed(seeded_org["clerk_org_id"], caller_admin_id, "admin")

    resp = client.delete(f"/admin/members/{target_user_id}")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert calls == [(seeded_org["clerk_org_id"], target_user_id)]

    with owner_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE clerk_user_id = %s", (target_user_id,))
        assert cur.fetchone() is None  # no local Users row -> soft-delete skipped, best-effort
        cur.execute(
            "SELECT status FROM clerk_admin_users WHERE clerk_user_id = %s", (target_user_id,)
        )
        assert cur.fetchone()[0] == "inactive"
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_member_removed' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        payloads = [row[0] for row in cur.fetchall()]
        assert any(
            p["removed_clerk_user_id"] == target_user_id and p["admin_role_deactivated"]
            for p in payloads
        )


def test_remove_member_404_no_live_membership(client, monkeypatch, owner_conn, seeded_org):
    monkeypatch.setattr(members_mod, "list_organization_memberships", _async_return([]))
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.delete("/admin/members/nonexistent-user")
    assert resp.status_code == 404


async def test_admin_user_repo_deactivate_sets_updated_at(owner_conn, seeded_org):
    # Direct repo-level regression: deactivate() is a Core-style bulk
    # update(), which bypasses ClerkAdminUser.updated_at's column-level
    # onupdate=utc_now (that only fires on ORM unit-of-work flushes) — must
    # set updated_at explicitly. Covers the D3 downgrade-sync call site too,
    # not just the new remove_member one.
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)  # active, updated_at NULL

    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.org_id', :tid, true)"),
            {"tid": seeded_org["clerk_org_id"]},
        )
        await AdminUserRepo(session).deactivate(admin_user_id)

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status, updated_at, created_at FROM clerk_admin_users WHERE clerk_user_id = %s",
            (admin_user_id,),
        )
        status, updated_at, created_at = cur.fetchone()
        assert status == "inactive"
        assert updated_at is not None
        assert updated_at >= created_at


async def test_ensure_user_provisioned_reactivates_soft_deleted_row(owner_conn, seeded_org):
    # Re-invitation path (item 7): a previously-removed member's row must
    # come back to status='active' on their next login, without clobbering
    # role/name/email set before removal.
    member_pk, member_clerk_id = _seed_member(
        owner_conn,
        seeded_org,
        role="admin",
        name="Jane",
        email="jane@example.com",
        status="inactive",
        deactivated_at=datetime.now(UTC).replace(tzinfo=None),
    )

    claims = _claims(seeded_org["clerk_org_id"], member_clerk_id, org_role="member")
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(
            text("SELECT set_config('app.org_id', :tid, true)"),
            {"tid": seeded_org["clerk_org_id"]},
        )
        await _ensure_user_provisioned(session, claims)

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT status, deactivated_at, role, name, email FROM users WHERE id = %s",
            (member_pk,),
        )
        status, deactivated_at, role, name, email = cur.fetchone()
        assert status == "active"
        assert deactivated_at is None
        assert role == "admin"
        assert name == "Jane"
        assert email == "jane@example.com"


def test_remove_member_self_removal_denied(client, monkeypatch, owner_conn, seeded_org):
    calls: list[Any] = []
    monkeypatch.setattr(members_mod, "list_organization_memberships", _record_and_fail(calls))
    monkeypatch.setattr(members_mod, "remove_organization_membership", _record_and_fail(calls))
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    # Path param is now the caller's own clerk_user_id — the self-guard fires
    # before any Clerk call, so zero calls are made.
    resp = client.delete(f"/admin/members/{admin_user_id}")
    assert resp.status_code == 403
    assert calls == []


def test_remove_member_member_token_denied(client, owner_conn, seeded_org):
    _, member_clerk_id = _seed_member(owner_conn, seeded_org)
    _authed(seeded_org["clerk_org_id"], f"user-{uuid.uuid4().hex[:8]}", "member")

    resp = client.delete(f"/admin/members/{member_clerk_id}")
    assert resp.status_code == 403


# --- Phase 7: own-org member role change (PATCH) ----------------------------


def test_update_member_role_promote_success(client, monkeypatch, owner_conn, seeded_org):
    member_pk, member_clerk_id = _seed_member(owner_conn, seeded_org, email="new-admin@example.com")

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(member_clerk_id, "new-admin@example.com", role="org:member")]

    calls: list[tuple[str, str, str]] = []

    async def _fake_update(org_id: str, member_user_id: str, role: str) -> dict[str, Any]:
        calls.append((org_id, member_user_id, role))
        return _fake_membership(member_clerk_id, "new-admin@example.com", role="org:admin")

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    monkeypatch.setattr(members_mod, "update_organization_membership", _fake_update)
    monkeypatch.setattr(
        members_mod, "fetch_clerk_user_primary_email", _async_return("new-admin@example.com")
    )
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch(f"/admin/members/{member_clerk_id}", json={"role": "admin"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "org:admin"
    assert calls == [(seeded_org["clerk_org_id"], member_clerk_id, "org:admin")]

    with owner_conn.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE id = %s", (member_pk,))
        assert cur.fetchone()[0] == "admin"
        cur.execute(
            "SELECT status, admin_type FROM clerk_admin_users WHERE clerk_user_id = %s",
            (member_clerk_id,),
        )
        status, admin_type = cur.fetchone()
        assert status == "active"
        assert admin_type == "client"
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_member_role_changed' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        payload = cur.fetchone()[0]
        assert payload["old_role"] == "org:member"
        assert payload["new_role"] == "org:admin"
        assert payload["target_clerk_user_id"] == member_clerk_id


def test_update_member_role_promote_no_local_user_row(client, monkeypatch, owner_conn, seeded_org):
    # Own-org counterpart of test_update_org_member_role_promote_no_local_user_row:
    # an admin-only identity with no `users` row can still be promoted — the
    # old local-int-id lookup would have 404'd here. Unlike the cross-org
    # path (reactivate_if_exists, UPDATE-only), the own-org promote path uses
    # reactivate_or_create, which DOES insert a fresh clerk_admin_users row.
    target_user_id = f"member-{uuid.uuid4().hex[:8]}"

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(target_user_id, "target@example.com", role="org:member")]

    calls: list[tuple[str, str, str]] = []

    async def _fake_update(org_id: str, member_user_id: str, role: str) -> dict[str, Any]:
        calls.append((org_id, member_user_id, role))
        return _fake_membership(target_user_id, "target@example.com", role="org:admin")

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    monkeypatch.setattr(members_mod, "update_organization_membership", _fake_update)
    monkeypatch.setattr(
        members_mod, "fetch_clerk_user_primary_email", _async_return("target@example.com")
    )
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch(f"/admin/members/{target_user_id}", json={"role": "admin"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "org:admin"
    assert calls == [(seeded_org["clerk_org_id"], target_user_id, "org:admin")]

    with owner_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE clerk_user_id = %s", (target_user_id,))
        assert cur.fetchone() is None  # no local Users row -> write skipped, best-effort
        cur.execute(
            "SELECT status, admin_type FROM clerk_admin_users WHERE clerk_user_id = %s",
            (target_user_id,),
        )
        status, admin_type = cur.fetchone()
        assert status == "active"
        assert admin_type == "client"
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_member_role_changed' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        payload = cur.fetchone()[0]
        assert payload["users_role_updated"] is False


def test_update_member_role_404_no_live_membership(client, monkeypatch, owner_conn, seeded_org):
    monkeypatch.setattr(members_mod, "list_organization_memberships", _async_return([]))
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch("/admin/members/nonexistent-user", json={"role": "admin"})
    assert resp.status_code == 404


def test_update_member_role_demote_success(client, monkeypatch, owner_conn, seeded_org):
    member_pk, member_clerk_id = _seed_member(
        owner_conn, seeded_org, role="admin", email="admin-member@example.com"
    )
    _seed_admin(owner_conn, seeded_org, member_clerk_id)  # target's own admin row, active
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)  # caller — keeps count at 2 pre-demote

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(member_clerk_id, "admin-member@example.com", role="org:admin")]

    calls: list[tuple[str, str, str]] = []

    async def _fake_update(org_id: str, member_user_id: str, role: str) -> dict[str, Any]:
        calls.append((org_id, member_user_id, role))
        return _fake_membership(member_clerk_id, "admin-member@example.com", role="org:member")

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    monkeypatch.setattr(members_mod, "update_organization_membership", _fake_update)
    monkeypatch.setattr(
        members_mod, "fetch_clerk_user_primary_email", _async_return("admin-member@example.com")
    )
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch(f"/admin/members/{member_clerk_id}", json={"role": "member"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "org:member"
    assert calls == [(seeded_org["clerk_org_id"], member_clerk_id, "org:member")]

    with owner_conn.cursor() as cur:
        cur.execute("SELECT role FROM users WHERE id = %s", (member_pk,))
        assert cur.fetchone()[0] == "member"
        cur.execute(
            "SELECT status FROM clerk_admin_users WHERE clerk_user_id = %s", (member_clerk_id,)
        )
        assert cur.fetchone()[0] == "inactive"
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_member_role_changed' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        payload = cur.fetchone()[0]
        assert payload["old_role"] == "org:admin"
        assert payload["new_role"] == "org:member"


def test_update_member_role_self_change_denied(client, monkeypatch, owner_conn, seeded_org):
    calls: list[Any] = []
    monkeypatch.setattr(members_mod, "list_organization_memberships", _record_and_fail(calls))
    monkeypatch.setattr(members_mod, "update_organization_membership", _record_and_fail(calls))
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch(f"/admin/members/{admin_user_id}", json={"role": "member"})
    assert resp.status_code == 403
    assert calls == []


def test_update_member_role_last_admin_demote_denied(client, monkeypatch, owner_conn, seeded_org):
    # count active clerk_admin_users rows for the org is a blunt pre-demotion
    # count (see plan/ponytail note in remove_member) — it's 1 here purely
    # because the caller is the sole active admin row; the target carries
    # a live Clerk role of org:admin but has no admin row of its own (an
    # out-of-sync state), so demoting them wouldn't actually zero out admin
    # access, but the guard still fires as scaffolded.
    calls: list[Any] = []
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _, member_clerk_id = _seed_member(owner_conn, seeded_org, role="admin")

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(member_clerk_id, "target@example.com", role="org:admin")]

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    monkeypatch.setattr(members_mod, "update_organization_membership", _record_and_fail(calls))
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch(f"/admin/members/{member_clerk_id}", json={"role": "member"})
    assert resp.status_code == 403
    assert calls == []
    assert _admin_status(owner_conn, admin_user_id) == "active"


def test_update_member_role_promote_reactivates_inactive_row(
    client, monkeypatch, owner_conn, seeded_org
):
    member_pk, member_clerk_id = _seed_member(
        owner_conn, seeded_org, email="reactivate@example.com"
    )
    _seed_admin(owner_conn, seeded_org, member_clerk_id, status="inactive")
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(member_clerk_id, "reactivate@example.com", role="org:member")]

    async def _fake_update(org_id: str, member_user_id: str, role: str) -> dict[str, Any]:
        return _fake_membership(member_clerk_id, "reactivate@example.com", role="org:admin")

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    monkeypatch.setattr(members_mod, "update_organization_membership", _fake_update)
    monkeypatch.setattr(
        members_mod, "fetch_clerk_user_primary_email", _async_return("reactivate@example.com")
    )
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch(f"/admin/members/{member_clerk_id}", json={"role": "admin"})
    assert resp.status_code == 200
    assert _admin_status(owner_conn, member_clerk_id) == "active"


def test_update_member_role_idempotent_noop(client, monkeypatch, owner_conn, seeded_org):
    calls: list[Any] = []
    _, member_clerk_id = _seed_member(
        owner_conn, seeded_org, role="member", email="noop@example.com"
    )

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(member_clerk_id, "noop@example.com", role="org:member")]

    monkeypatch.setattr(members_mod, "list_organization_memberships", _fake_list)
    monkeypatch.setattr(members_mod, "update_organization_membership", _record_and_fail(calls))
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch(f"/admin/members/{member_clerk_id}", json={"role": "member"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "org:member"
    # The no-op early return must still reflect the target's actual current
    # status (not a stale/hardcoded value) — see _to_org_member_response
    # construction in update_member_role.
    assert resp.json()["status"] == "active"
    assert calls == []

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM human_audit_log WHERE event_type = 'admin_member_role_changed' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        assert cur.fetchone()[0] == 0


def test_d3_grace_window_recently_touched_row_not_deactivated(
    client, monkeypatch, owner_conn, seeded_org
):
    # Contrast with test_r6_downgrade_sync_revokes_on_demotion (old/NULL
    # updated_at): that test must still deactivate unchanged. This row's
    # updated_at is fresh (simulating a row this app just promoted), so the
    # stale-token D3 sync must NOT undo it — the request should succeed.
    monkeypatch.setattr(members_mod, "list_organization_memberships", _async_return([]))
    user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, user_id, updated_at=datetime.now(UTC).replace(tzinfo=None))

    _authed(seeded_org["clerk_org_id"], user_id, "member")  # stale token right after our promote
    resp = client.get("/admin/members")
    assert resp.status_code == 200
    assert _admin_status(owner_conn, user_id) == "active"


# --- Phase 3: invitations -----------------------------------------------------


def test_create_invitation_rejects_non_member_role_with_no_clerk_call(
    client, monkeypatch, owner_conn, seeded_org
):
    # The shared CreateInvitationRequest schema now allows "admin" (platform_invitations.py
    # accepts it), so this endpoint's own explicit guard — not schema validation — is what
    # rejects it here: 403, not 422.
    calls: list[Any] = []
    monkeypatch.setattr(invitations_mod, "create_organization_invitation", _record_and_fail(calls))
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post(
        "/admin/invitations", json={"emailAddress": "x@example.com", "role": "admin"}
    )
    assert resp.status_code == 403
    assert calls == []
    assert calls == []


def test_create_invitation_success(client, monkeypatch, owner_conn, seeded_org):
    captured: dict[str, Any] = {}

    async def _fake_create(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _fake_invitation(kwargs["email"])

    monkeypatch.setattr(invitations_mod, "create_organization_invitation", _fake_create)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post("/admin/invitations", json={"emailAddress": "x@example.com"})
    assert resp.status_code == 201
    assert captured["role"] == "org:member"
    assert captured["redirect_url"].endswith("/sign-up")
    assert captured["org_id"] == seeded_org["clerk_org_id"]

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_invitation_created' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        assert cur.fetchone()[0]["email"] == "x@example.com"


def test_list_invitations(client, monkeypatch, owner_conn, seeded_org):
    async def _fake_list(org_id: str, status: str = "pending") -> list[dict[str, Any]]:
        return [_fake_invitation("pending@example.com")]

    monkeypatch.setattr(invitations_mod, "list_organization_invitations", _fake_list)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.get("/admin/invitations")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["emailAddress"] == "pending@example.com"
    assert body[0]["role"] == "org:member"


def test_revoke_invitation_writes_audit(client, monkeypatch, owner_conn, seeded_org):
    calls: list[tuple[str, str, str]] = []

    async def _fake_revoke(org_id: str, invitation_id: str, requesting_user_id: str) -> dict:
        calls.append((org_id, invitation_id, requesting_user_id))
        return {}

    monkeypatch.setattr(invitations_mod, "revoke_organization_invitation", _fake_revoke)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, seeded_org, admin_user_id)
    _authed(seeded_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.delete("/admin/invitations/inv_abc123")
    assert resp.status_code == 200
    assert calls == [(seeded_org["clerk_org_id"], "inv_abc123", admin_user_id)]

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_invitation_revoked' "
            "AND org_id = %s",
            (seeded_org["org_pk"],),
        )
        assert cur.fetchone()[0]["clerk_invitation_id"] == "inv_abc123"


def _record_and_fail(calls: list[Any]):
    async def _fn(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        raise AssertionError("Clerk adapter should not have been called")

    return _fn


# --- Phase 4: platform-admin organizations -----------------------------------


def test_create_org_non_platform_admin_denied(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], f"user-{uuid.uuid4().hex[:8]}", "admin")

    resp = client.post(
        "/admin/organizations",
        json={"name": "New Co", "accountManagerEmail": "am@example.com"},
    )
    assert resp.status_code == 403


def test_create_org_success_no_local_insert(client, monkeypatch, owner_conn, platform_org):
    new_org_clerk_id = f"test-new-org-{uuid.uuid4().hex[:8]}"
    call_order: list[str] = []

    async def _fake_create_org(**kwargs: Any) -> dict[str, Any]:
        call_order.append("create_organization")
        assert kwargs["created_by"]
        return _fake_clerk_org(new_org_clerk_id, kwargs["name"], "PE Firm")

    async def _fake_invite(**kwargs: Any) -> dict[str, Any]:
        call_order.append("create_organization_invitation")
        assert kwargs["redirect_url"].endswith("/admin/sign-up")
        assert kwargs["role"] == "org:admin"
        return _fake_invitation(kwargs["email"], role="org:admin")

    removed_membership: dict[str, str] = {}

    async def _fake_remove_membership(org_id: str, member_user_id: str) -> None:
        call_order.append("remove_organization_membership")
        removed_membership["org_id"] = org_id
        removed_membership["member_user_id"] = member_user_id

    monkeypatch.setattr(organizations_mod, "create_organization", _fake_create_org)
    monkeypatch.setattr(organizations_mod, "create_organization_invitation", _fake_invite)
    monkeypatch.setattr(
        organizations_mod, "remove_organization_membership", _fake_remove_membership
    )

    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(
        owner_conn,
        platform_org,
        admin_user_id,
        admin_type="platform",
    )
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post(
        "/admin/organizations",
        json={"name": "New Co", "type": "PE Firm", "accountManagerEmail": "am@example.com"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["clerkOrgId"] == new_org_clerk_id
    assert call_order == [
        "create_organization",
        "create_organization_invitation",
        "remove_organization_membership",
    ]
    # R1 reversed: the calling platform admin's own Clerk membership on the
    # new org is removed immediately after creation, not left in place.
    assert removed_membership == {"org_id": new_org_clerk_id, "member_user_id": admin_user_id}

    with owner_conn.cursor() as cur:
        cur.execute("SELECT id FROM organisation WHERE clerk_org_id = %s", (new_org_clerk_id,))
        assert cur.fetchone() is None  # no local organisation row inserted

        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_organization_created' "
            "AND org_id = %s",
            (platform_org["org_pk"],),
        )
        payload = cur.fetchone()[0]
        assert payload["clerk_org_id"] == new_org_clerk_id
        assert payload["creator_membership_removed"] is True


def test_create_org_membership_removal_failure_does_not_fail_the_request(
    client, monkeypatch, owner_conn, platform_org
):
    """Best-effort per the docstring: a Clerk-side failure removing the
    creator's membership must not undo the org/invitation that already
    exist, and must still show up (as False) on the audit row."""
    new_org_clerk_id = f"test-new-org-{uuid.uuid4().hex[:8]}"

    async def _fake_create_org(**kwargs: Any) -> dict[str, Any]:
        return _fake_clerk_org(new_org_clerk_id, kwargs["name"], "PE Firm")

    async def _fake_invite(**kwargs: Any) -> dict[str, Any]:
        return _fake_invitation(kwargs["email"], role="org:admin")

    async def _fake_remove_membership_fails(org_id: str, member_user_id: str) -> None:
        raise _http_status_error(500)

    monkeypatch.setattr(organizations_mod, "create_organization", _fake_create_org)
    monkeypatch.setattr(organizations_mod, "create_organization_invitation", _fake_invite)
    monkeypatch.setattr(
        organizations_mod, "remove_organization_membership", _fake_remove_membership_fails
    )

    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post(
        "/admin/organizations",
        json={"name": "New Co", "type": "PE Firm", "accountManagerEmail": "am@example.com"},
    )
    assert resp.status_code == 201  # request still succeeds

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_organization_created' "
            "AND org_id = %s",
            (platform_org["org_pk"],),
        )
        assert cur.fetchone()[0]["creator_membership_removed"] is False


def test_list_organizations_excludes_platform_org(client, monkeypatch, owner_conn, platform_org):
    other_org_id = f"test-client-org-{uuid.uuid4().hex[:8]}"

    async def _fake_list(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            _fake_clerk_org(platform_org["clerk_org_id"], "Simpero"),
            _fake_clerk_org(other_org_id, "Client Co"),
        ]

    monkeypatch.setattr(organizations_mod, "list_organizations", _fake_list)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.get("/admin/organizations")
    assert resp.status_code == 200
    body = resp.json()
    assert [o["clerkOrgId"] for o in body] == [other_org_id]


# --- Phase 5: platform member-invite into a client org (D2) ------------------


def test_platform_invite_non_platform_admin_denied_no_clerk_call(
    client, monkeypatch, owner_conn, seeded_org
):
    # Explicit, distinct platform org id: this test targets the "caller is
    # not a platform admin" guard path specifically (via the admin_type/
    # tenant cross-check), not the separate "unconfigured" fail-closed path
    # covered by test_guard_platform_org_unconfigured_denied.
    monkeypatch.setattr(
        admin_deps_mod.settings, "simpero_platform_org_id", "some-other-platform-org"
    )
    calls: list[Any] = []
    monkeypatch.setattr(
        platform_invitations_mod, "fetch_clerk_organization", _record_and_fail(calls)
    )
    monkeypatch.setattr(
        platform_invitations_mod, "create_organization_invitation", _record_and_fail(calls)
    )
    _authed(seeded_org["clerk_org_id"], f"user-{uuid.uuid4().hex[:8]}", "admin")

    resp = client.post(
        f"/admin/organizations/{uuid.uuid4().hex}/invitations",
        json={"emailAddress": "x@example.com"},
    )
    assert resp.status_code == 403
    assert calls == []


def test_platform_invite_into_platform_org_denied(client, monkeypatch, owner_conn, platform_org):
    calls: list[Any] = []
    monkeypatch.setattr(
        platform_invitations_mod, "fetch_clerk_organization", _record_and_fail(calls)
    )
    monkeypatch.setattr(
        platform_invitations_mod, "create_organization_invitation", _record_and_fail(calls)
    )
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post(
        f"/admin/organizations/{platform_org['clerk_org_id']}/invitations",
        json={"emailAddress": "x@example.com"},
    )
    assert resp.status_code == 403
    assert calls == []


def test_platform_invite_nonexistent_org_404(client, monkeypatch, owner_conn, platform_org):
    async def _fake_fetch(clerk_org_id: str) -> dict[str, Any]:
        raise _http_status_error(404)

    calls: list[Any] = []
    monkeypatch.setattr(platform_invitations_mod, "fetch_clerk_organization", _fake_fetch)
    monkeypatch.setattr(
        platform_invitations_mod, "create_organization_invitation", _record_and_fail(calls)
    )
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post(
        "/admin/organizations/test-missing-org/invitations",
        json={"emailAddress": "x@example.com"},
    )
    assert resp.status_code == 404
    assert calls == []


def test_platform_invite_success_audited_in_simpero_trail(
    client, monkeypatch, owner_conn, platform_org
):
    target_org_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    captured: dict[str, Any] = {}

    async def _fake_fetch(clerk_org_id: str) -> dict[str, Any]:
        return _fake_clerk_org(clerk_org_id, "Client Co")

    async def _fake_invite(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _fake_invitation(kwargs["email"])

    monkeypatch.setattr(platform_invitations_mod, "fetch_clerk_organization", _fake_fetch)
    monkeypatch.setattr(platform_invitations_mod, "create_organization_invitation", _fake_invite)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post(
        f"/admin/organizations/{target_org_id}/invitations",
        json={"emailAddress": "new-user@example.com"},
    )
    assert resp.status_code == 201
    assert captured["org_id"] == target_org_id
    assert captured["role"] == "org:member"
    assert captured["redirect_url"].endswith("/sign-up")
    # Regression: Clerk rejects create_organization_invitation with "not a
    # member" if inviter_user_id names someone who isn't actually a member
    # of org_id — true of every platform admin on every pre-existing client
    # org (R1), so this call must never pass it. Confirmed against the real
    # Clerk API, not just inferred.
    assert "inviter_user_id" not in captured

    with owner_conn.cursor() as cur:
        # Scoped by org_id (not just event_type): the shared dev DB can carry
        # real leftover admin_member_invited_by_platform rows from manual
        # testing against the live app, which would otherwise inflate this
        # count unrelated to what this test just did.
        cur.execute(
            "SELECT payload FROM human_audit_log "
            "WHERE event_type = 'admin_member_invited_by_platform' AND org_id = %s",
            (platform_org["org_pk"],),
        )
        rows = cur.fetchall()
        assert len(rows) == 1
        payload = rows[0][0]
        assert payload["target_clerk_org_id"] == target_org_id


# --- Phase 6: platform view of an arbitrary org's members --------------------


def _fake_membership(
    user_id: str, email: str, role: str = "org:member", first_name: str = "Jane"
) -> dict[str, Any]:
    return {
        "id": f"orgmem_{uuid.uuid4().hex[:8]}",
        "role": role,
        "public_user_data": {
            "user_id": user_id,
            "first_name": first_name,
            "last_name": "Doe",
            "identifier": email,
        },
    }


def test_platform_members_non_platform_admin_denied_no_clerk_call(
    client, monkeypatch, owner_conn, seeded_org
):
    calls: list[Any] = []
    monkeypatch.setattr(
        platform_members_mod, "list_organization_memberships", _record_and_fail(calls)
    )
    _authed(seeded_org["clerk_org_id"], f"user-{uuid.uuid4().hex[:8]}", "admin")

    resp = client.get(f"/admin/organizations/{uuid.uuid4().hex}/members")
    assert resp.status_code == 403
    assert calls == []


def test_platform_members_list_success_maps_fields(client, monkeypatch, owner_conn, platform_org):
    target_org_id = f"test-client-org-{uuid.uuid4().hex[:8]}"

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        assert org_id == target_org_id
        return [_fake_membership("user_abc", "admin@client.co", role="org:admin")]

    monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.get(f"/admin/organizations/{target_org_id}/members")
    assert resp.status_code == 200
    [member] = resp.json()
    assert member["userId"] == "user_abc"
    assert member["email"] == "admin@client.co"
    assert member["name"] == "Jane Doe"
    assert member["role"] == "org:admin"
    assert member["status"] == "active"


def test_platform_members_list_merges_locally_inactive_row(
    client, monkeypatch, owner_conn, platform_org
):
    # A removed member's Clerk membership is fully revoked, so they never
    # appear in the live list — list_org_members merges in their local
    # soft-deleted `users` row instead, status="inactive".
    target_org_clerk_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    removed_clerk_user_id = f"member-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (target_org_clerk_id, "Client Co"),
        )
        target_org_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (org_id, clerk_user_id, clerk_org_id, role, login_method, "
            "name, email, status, deactivated_at) VALUES (%s, %s, %s, 'member', 'clerk', "
            "%s, %s, 'inactive', now())",
            (
                target_org_pk,
                removed_clerk_user_id,
                target_org_clerk_id,
                "Removed Bob",
                "bob@client.co",
            ),
        )

    try:

        async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
            return []  # membership fully revoked, no longer in the live list

        monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
        admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
        _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
        _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

        resp = client.get(f"/admin/organizations/{target_org_clerk_id}/members")
        assert resp.status_code == 200
        [member] = resp.json()
        assert member["userId"] == removed_clerk_user_id
        assert member["id"] == removed_clerk_user_id
        assert member["name"] == "Removed Bob"
        assert member["email"] == "bob@client.co"
        assert member["role"] == "org:member"
        assert member["status"] == "inactive"
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (target_org_pk,))


def test_platform_members_list_dedups_local_inactive_row_present_in_live_clerk(
    client, monkeypatch, owner_conn, platform_org
):
    # Someone was removed, then re-invited: their Clerk membership is active
    # again but they haven't logged back in yet, so their local `users` row
    # is still "inactive". Live Clerk data wins — the row must appear once,
    # as active, not twice.
    target_org_clerk_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    readmitted_clerk_user_id = f"member-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (target_org_clerk_id, "Client Co"),
        )
        target_org_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (org_id, clerk_user_id, clerk_org_id, role, login_method, "
            "name, email, status, deactivated_at) VALUES (%s, %s, %s, 'member', 'clerk', "
            "%s, %s, 'inactive', now())",
            (
                target_org_pk,
                readmitted_clerk_user_id,
                target_org_clerk_id,
                "Readmitted Bob",
                "bob@client.co",
            ),
        )

    try:

        async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [_fake_membership(readmitted_clerk_user_id, "bob@client.co")]

        monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
        admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
        _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
        _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

        resp = client.get(f"/admin/organizations/{target_org_clerk_id}/members")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1  # not duplicated
        assert body[0]["userId"] == readmitted_clerk_user_id
        assert body[0]["status"] == "active"  # live Clerk wins over the stale local row
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (target_org_pk,))


# --- Phase 8: platform-admin cross-org member role change (PATCH) -----------


def test_update_org_member_role_platform_org_denied(client, monkeypatch, owner_conn, platform_org):
    calls: list[Any] = []
    monkeypatch.setattr(
        platform_members_mod, "list_organization_memberships", _record_and_fail(calls)
    )
    monkeypatch.setattr(
        platform_members_mod, "update_organization_membership", _record_and_fail(calls)
    )
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch(
        f"/admin/organizations/{platform_org['clerk_org_id']}/members/some-user",
        json={"role": "member"},
    )
    assert resp.status_code == 403
    assert calls == []


def test_update_org_member_role_cross_org_self_change_denied(
    client, monkeypatch, owner_conn, platform_org
):
    # Defense-in-depth regression: R1's membership removal after org
    # creation is best-effort and can fail, which would leave a platform
    # admin as a real member of a client org. This guard must reject the
    # caller targeting their own clerk_user_id regardless of which org is
    # named in the path, with zero Clerk calls made.
    calls: list[Any] = []
    monkeypatch.setattr(
        platform_members_mod, "list_organization_memberships", _record_and_fail(calls)
    )
    monkeypatch.setattr(
        platform_members_mod, "update_organization_membership", _record_and_fail(calls)
    )
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    target_org_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    resp = client.patch(
        f"/admin/organizations/{target_org_id}/members/{admin_user_id}",
        json={"role": "admin"},
    )
    assert resp.status_code == 403
    assert calls == []


def test_update_org_member_role_promote_no_local_user_row(
    client, monkeypatch, owner_conn, platform_org
):
    target_org_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    target_user_id = f"member-{uuid.uuid4().hex[:8]}"

    async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_fake_membership(target_user_id, "target@example.com", role="org:member")]

    calls: list[tuple[str, str, str]] = []

    async def _fake_update(org_id: str, member_user_id: str, role: str) -> dict[str, Any]:
        calls.append((org_id, member_user_id, role))
        return _fake_membership(target_user_id, "target@example.com", role="org:admin")

    monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
    monkeypatch.setattr(platform_members_mod, "update_organization_membership", _fake_update)
    monkeypatch.setattr(
        platform_members_mod, "fetch_clerk_user_primary_email", _async_return("target@example.com")
    )
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.patch(
        f"/admin/organizations/{target_org_id}/members/{target_user_id}", json={"role": "admin"}
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "org:admin"
    assert calls == [(target_org_id, target_user_id, "org:admin")]

    with owner_conn.cursor() as cur:
        cur.execute("SELECT id FROM users WHERE clerk_user_id = %s", (target_user_id,))
        assert cur.fetchone() is None  # no local Users row for this org -> write skipped
        cur.execute("SELECT id FROM clerk_admin_users WHERE clerk_user_id = %s", (target_user_id,))
        assert cur.fetchone() is None  # reactivate_if_exists is UPDATE-only, never inserts
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_member_role_changed' "
            "AND org_id = %s",
            (platform_org["org_pk"],),
        )
        payload = cur.fetchone()[0]
        assert payload["target_clerk_org_id"] == target_org_id
        assert payload["new_role"] == "org:admin"
        assert payload["users_role_updated"] is False


def test_update_org_member_role_promote_reactivates_inactive_row_cross_org(
    client, monkeypatch, owner_conn, platform_org
):
    """Cross-org counterpart of test_update_member_role_promote_reactivates_inactive_row
    — exercises AdminUserRepo.reactivate_if_exists actually flipping an
    EXISTING inactive row back to active (unlike
    test_update_org_member_role_promote_no_local_user_row, where no row
    exists at all and the UPDATE-only method matches nothing). Also asserts
    updated_at is refreshed, since the D3 grace window (_ensure_admin_provisioned)
    depends on that for this path too, not just the own-org
    reactivate_or_create path."""
    target_org_clerk_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    target_user_id = f"member-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (target_org_clerk_id, "Client Co"),
        )
        target_org_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO clerk_admin_users (clerk_user_id, clerk_org_id, org_id, admin_type, "
            "status, created_at) VALUES (%s, %s, %s, 'client', 'inactive', now())",
            (target_user_id, target_org_clerk_id, target_org_pk),
        )

    try:

        async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [_fake_membership(target_user_id, "target@example.com", role="org:member")]

        async def _fake_update(org_id: str, member_user_id: str, role: str) -> dict[str, Any]:
            return _fake_membership(target_user_id, "target@example.com", role="org:admin")

        monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
        monkeypatch.setattr(platform_members_mod, "update_organization_membership", _fake_update)
        monkeypatch.setattr(
            platform_members_mod,
            "fetch_clerk_user_primary_email",
            _async_return("target@example.com"),
        )
        admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
        _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
        _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

        resp = client.patch(
            f"/admin/organizations/{target_org_clerk_id}/members/{target_user_id}",
            json={"role": "admin"},
        )
        assert resp.status_code == 200

        with owner_conn.cursor() as cur:
            cur.execute(
                "SELECT status, admin_type, updated_at FROM clerk_admin_users "
                "WHERE clerk_user_id = %s",
                (target_user_id,),
            )
            status, admin_type, updated_at = cur.fetchone()
            assert status == "active"
            assert admin_type == "client"
            assert updated_at is not None
            assert (datetime.now(UTC).replace(tzinfo=None) - updated_at) < timedelta(seconds=10)
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM human_audit_log WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM clerk_admin_users WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (target_org_pk,))


def test_update_org_member_role_demote_success_with_local_user_row(
    client, monkeypatch, owner_conn, platform_org
):
    target_org_clerk_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    target_user_id = f"member-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (target_org_clerk_id, "Client Co"),
        )
        target_org_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (org_id, clerk_user_id, clerk_org_id, role, login_method, email) "
            "VALUES (%s, %s, %s, 'admin', 'clerk', %s)",
            (target_org_pk, target_user_id, target_org_clerk_id, "target@example.com"),
        )
        # target's own admin row, plus a second so the demote isn't blocked as "last admin"
        cur.execute(
            "INSERT INTO clerk_admin_users (clerk_user_id, clerk_org_id, org_id, admin_type, "
            "status, created_at) VALUES (%s, %s, %s, 'client', 'active', now())",
            (target_user_id, target_org_clerk_id, target_org_pk),
        )
        cur.execute(
            "INSERT INTO clerk_admin_users (clerk_user_id, clerk_org_id, org_id, admin_type, "
            "status, created_at) VALUES (%s, %s, %s, 'client', 'active', now())",
            (f"other-admin-{uuid.uuid4().hex[:8]}", target_org_clerk_id, target_org_pk),
        )

    try:

        async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [_fake_membership(target_user_id, "target@example.com", role="org:admin")]

        calls: list[tuple[str, str, str]] = []

        async def _fake_update(org_id: str, member_user_id: str, role: str) -> dict[str, Any]:
            calls.append((org_id, member_user_id, role))
            return _fake_membership(target_user_id, "target@example.com", role="org:member")

        monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
        monkeypatch.setattr(platform_members_mod, "update_organization_membership", _fake_update)
        monkeypatch.setattr(
            platform_members_mod,
            "fetch_clerk_user_primary_email",
            _async_return("target@example.com"),
        )
        admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
        _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
        _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

        resp = client.patch(
            f"/admin/organizations/{target_org_clerk_id}/members/{target_user_id}",
            json={"role": "member"},
        )
        assert resp.status_code == 200
        assert resp.json()["role"] == "org:member"
        assert calls == [(target_org_clerk_id, target_user_id, "org:member")]

        with owner_conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE clerk_user_id = %s", (target_user_id,))
            assert cur.fetchone()[0] == "member"
            cur.execute(
                "SELECT status FROM clerk_admin_users WHERE clerk_user_id = %s", (target_user_id,)
            )
            assert cur.fetchone()[0] == "inactive"
            cur.execute(
                "SELECT payload FROM human_audit_log WHERE event_type = 'admin_member_role_changed' "
                "AND org_id = %s",
                (platform_org["org_pk"],),
            )
            assert cur.fetchone()[0]["users_role_updated"] is True
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM human_audit_log WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM clerk_admin_users WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM users WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (target_org_pk,))


def test_update_org_member_role_last_admin_demote_denied_scoped_to_target_org(
    client, monkeypatch, owner_conn, platform_org
):
    # The caller's own (platform) org may have several active admins — the
    # count guarding "last active admin" must be scoped to the TARGET org,
    # not the platform org, via _set_org_scope. The platform org is seeded
    # with 2 active admins (>1) while the target org has exactly 1: if the
    # guard were (incorrectly) counting the platform org's admins instead of
    # the target's, active_admins would be 2 and the demote would be
    # WRONGLY allowed (calls != [], 200) instead of blocked — so this setup
    # actually distinguishes correct from incorrect scoping, unlike a case
    # where both counts happen to be 1.
    target_org_clerk_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    target_user_id = f"member-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (target_org_clerk_id, "Client Co"),
        )
        target_org_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO clerk_admin_users (clerk_user_id, clerk_org_id, org_id, admin_type, "
            "status, created_at) VALUES (%s, %s, %s, 'client', 'active', now())",
            (target_user_id, target_org_clerk_id, target_org_pk),
        )

    try:

        async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [_fake_membership(target_user_id, "target@example.com", role="org:admin")]

        calls: list[Any] = []
        monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
        monkeypatch.setattr(
            platform_members_mod, "update_organization_membership", _record_and_fail(calls)
        )
        admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
        _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
        # A second active platform-org admin — makes the platform org's count
        # (2) diverge from the target org's count (1), see comment above.
        _seed_admin(
            owner_conn,
            platform_org,
            f"other-platform-admin-{uuid.uuid4().hex[:8]}",
            admin_type="platform",
        )
        _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

        resp = client.patch(
            f"/admin/organizations/{target_org_clerk_id}/members/{target_user_id}",
            json={"role": "member"},
        )
        assert resp.status_code == 403
        assert calls == []
        with owner_conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM clerk_admin_users WHERE clerk_user_id = %s", (target_user_id,)
            )
            assert cur.fetchone()[0] == "active"
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM clerk_admin_users WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (target_org_pk,))


# --- Phase 9: platform-admin cross-org member removal (DELETE) --------------


def test_remove_org_member_admin_with_no_local_user_row(
    client, monkeypatch, owner_conn, platform_org
):
    target_org_clerk_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    target_user_id = f"member-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (target_org_clerk_id, "Client Co"),
        )
        target_org_pk = cur.fetchone()[0]
        # target's own admin row, plus a second so removal isn't blocked as "last admin"
        cur.execute(
            "INSERT INTO clerk_admin_users (clerk_user_id, clerk_org_id, org_id, admin_type, "
            "status, created_at) VALUES (%s, %s, %s, 'client', 'active', now())",
            (target_user_id, target_org_clerk_id, target_org_pk),
        )
        cur.execute(
            "INSERT INTO clerk_admin_users (clerk_user_id, clerk_org_id, org_id, admin_type, "
            "status, created_at) VALUES (%s, %s, %s, 'client', 'active', now())",
            (f"other-admin-{uuid.uuid4().hex[:8]}", target_org_clerk_id, target_org_pk),
        )

    try:

        async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [_fake_membership(target_user_id, "target@example.com", role="org:admin")]

        calls: list[tuple[str, str]] = []

        async def _fake_remove(org_id: str, member_user_id: str) -> None:
            calls.append((org_id, member_user_id))

        monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
        monkeypatch.setattr(platform_members_mod, "remove_organization_membership", _fake_remove)
        monkeypatch.setattr(
            platform_members_mod,
            "fetch_clerk_user_primary_email",
            _async_return("target@example.com"),
        )
        admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
        _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
        _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

        resp = client.delete(f"/admin/organizations/{target_org_clerk_id}/members/{target_user_id}")
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert calls == [(target_org_clerk_id, target_user_id)]

        with owner_conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM clerk_admin_users WHERE clerk_user_id = %s", (target_user_id,)
            )
            assert cur.fetchone()[0] == "inactive"
            cur.execute(
                "SELECT payload FROM human_audit_log WHERE event_type = 'admin_member_removed' "
                "AND org_id = %s",
                (platform_org["org_pk"],),
            )
            payload = cur.fetchone()[0]
            assert payload["admin_role_deactivated"] is True
            assert payload["clerk_membership_revoked"] is True
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM human_audit_log WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM clerk_admin_users WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (target_org_pk,))


def test_remove_org_member_plain_member_with_local_user_row(
    client, monkeypatch, owner_conn, platform_org
):
    # Exercises the RLS re-scoping + flush-before-rescope path — same class
    # of test as test_update_org_member_role_demote_success_with_local_user_row.
    # Without the db.flush() call before _set_org_scope re-points RLS back to
    # the platform org, this UPDATE would be deferred to commit time (when
    # app.org_id is the platform org, not the target org) and silently affect
    # 0 rows — so this test would fail without that flush.
    target_org_clerk_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    target_user_id = f"member-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (target_org_clerk_id, "Client Co"),
        )
        target_org_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (org_id, clerk_user_id, clerk_org_id, role, login_method, email) "
            "VALUES (%s, %s, %s, 'member', 'clerk', %s)",
            (target_org_pk, target_user_id, target_org_clerk_id, "target@example.com"),
        )

    try:

        async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [_fake_membership(target_user_id, "target@example.com", role="org:member")]

        calls: list[tuple[str, str]] = []

        async def _fake_remove(org_id: str, member_user_id: str) -> None:
            calls.append((org_id, member_user_id))

        monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
        monkeypatch.setattr(platform_members_mod, "remove_organization_membership", _fake_remove)
        monkeypatch.setattr(
            platform_members_mod,
            "fetch_clerk_user_primary_email",
            _async_return("target@example.com"),
        )
        admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
        _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
        _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

        resp = client.delete(f"/admin/organizations/{target_org_clerk_id}/members/{target_user_id}")
        assert resp.status_code == 200
        assert calls == [(target_org_clerk_id, target_user_id)]

        with owner_conn.cursor() as cur:
            cur.execute(
                "SELECT status, deactivated_at FROM users WHERE clerk_user_id = %s",
                (target_user_id,),
            )
            status, deactivated_at = cur.fetchone()
            assert status == "inactive"
            assert deactivated_at is not None
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM human_audit_log WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM users WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (target_org_pk,))


def test_remove_org_member_self_change_denied(client, monkeypatch, owner_conn, platform_org):
    calls: list[Any] = []
    monkeypatch.setattr(
        platform_members_mod, "list_organization_memberships", _record_and_fail(calls)
    )
    monkeypatch.setattr(
        platform_members_mod, "remove_organization_membership", _record_and_fail(calls)
    )
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    target_org_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    resp = client.delete(f"/admin/organizations/{target_org_id}/members/{admin_user_id}")
    assert resp.status_code == 403
    assert calls == []


def test_remove_org_member_platform_org_denied(client, monkeypatch, owner_conn, platform_org):
    calls: list[Any] = []
    monkeypatch.setattr(
        platform_members_mod, "list_organization_memberships", _record_and_fail(calls)
    )
    monkeypatch.setattr(
        platform_members_mod, "remove_organization_membership", _record_and_fail(calls)
    )
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.delete(f"/admin/organizations/{platform_org['clerk_org_id']}/members/some-user")
    assert resp.status_code == 403
    assert calls == []


def test_remove_org_member_last_admin_denied_scoped_to_target_org(
    client, monkeypatch, owner_conn, platform_org
):
    # Mirror of test_update_org_member_role_last_admin_demote_denied_scoped_to_target_org
    # — the platform org is seeded with 2 active admins (>1) while the target
    # org has exactly 1: if the guard were (incorrectly) counting the
    # platform org's admins, active_admins would be 2 and removal would be
    # WRONGLY allowed instead of blocked.
    target_org_clerk_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    target_user_id = f"member-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (target_org_clerk_id, "Client Co"),
        )
        target_org_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO clerk_admin_users (clerk_user_id, clerk_org_id, org_id, admin_type, "
            "status, created_at) VALUES (%s, %s, %s, 'client', 'active', now())",
            (target_user_id, target_org_clerk_id, target_org_pk),
        )

    try:

        async def _fake_list(org_id: str, **kwargs: Any) -> list[dict[str, Any]]:
            return [_fake_membership(target_user_id, "target@example.com", role="org:admin")]

        calls: list[Any] = []
        monkeypatch.setattr(platform_members_mod, "list_organization_memberships", _fake_list)
        monkeypatch.setattr(
            platform_members_mod, "remove_organization_membership", _record_and_fail(calls)
        )
        admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
        _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
        # A second active platform-org admin — makes the platform org's count
        # (2) diverge from the target org's count (1), see comment above.
        _seed_admin(
            owner_conn,
            platform_org,
            f"other-platform-admin-{uuid.uuid4().hex[:8]}",
            admin_type="platform",
        )
        _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

        resp = client.delete(f"/admin/organizations/{target_org_clerk_id}/members/{target_user_id}")
        assert resp.status_code == 403
        assert calls == []
        with owner_conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM clerk_admin_users WHERE clerk_user_id = %s", (target_user_id,)
            )
            assert cur.fetchone()[0] == "active"
    finally:
        with owner_conn.cursor() as cur:
            cur.execute("DELETE FROM clerk_admin_users WHERE org_id = %s", (target_org_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (target_org_pk,))


# --- accountManagerEmail optional on org creation ----------------------------


def test_create_org_no_account_manager_email_skips_invitation(
    client, monkeypatch, owner_conn, platform_org
):
    new_org_clerk_id = f"test-new-org-{uuid.uuid4().hex[:8]}"
    calls: list[Any] = []

    async def _fake_create_org(**kwargs: Any) -> dict[str, Any]:
        return _fake_clerk_org(new_org_clerk_id, kwargs["name"], "PE Firm")

    async def _fake_remove_membership(org_id: str, member_user_id: str) -> None:
        pass

    monkeypatch.setattr(organizations_mod, "create_organization", _fake_create_org)
    monkeypatch.setattr(
        organizations_mod, "create_organization_invitation", _record_and_fail(calls)
    )
    monkeypatch.setattr(
        organizations_mod, "remove_organization_membership", _fake_remove_membership
    )

    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post("/admin/organizations", json={"name": "New Co", "type": "PE Firm"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["invitation"] is None
    assert calls == []  # create_organization_invitation never called

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log WHERE event_type = 'admin_organization_created' "
            "AND org_id = %s",
            (platform_org["org_pk"],),
        )
        payload = cur.fetchone()[0]
        assert payload["account_manager_email"] is None
        assert payload["seed_invitation_id"] is None


# --- platform admin can invite an org admin -----------------------------------


def test_platform_invite_admin_role_uses_org_admin_and_admin_sign_up(
    client, monkeypatch, owner_conn, platform_org
):
    target_org_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    captured: dict[str, Any] = {}

    async def _fake_fetch(clerk_org_id: str) -> dict[str, Any]:
        return _fake_clerk_org(clerk_org_id, "Client Co")

    async def _fake_invite(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _fake_invitation(kwargs["email"], role="org:admin")

    monkeypatch.setattr(platform_invitations_mod, "fetch_clerk_organization", _fake_fetch)
    monkeypatch.setattr(platform_invitations_mod, "create_organization_invitation", _fake_invite)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post(
        f"/admin/organizations/{target_org_id}/invitations",
        json={"emailAddress": "new-admin@example.com", "role": "admin"},
    )
    assert resp.status_code == 201
    assert captured["role"] == "org:admin"
    assert captured["redirect_url"].endswith("/admin/sign-up")

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log "
            "WHERE event_type = 'admin_member_invited_by_platform' AND org_id = %s",
            (platform_org["org_pk"],),
        )
        assert cur.fetchone()[0]["role"] == "admin"


def test_platform_invite_member_role_still_uses_org_member_and_sign_up(
    client, monkeypatch, owner_conn, platform_org
):
    target_org_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    captured: dict[str, Any] = {}

    async def _fake_fetch(clerk_org_id: str) -> dict[str, Any]:
        return _fake_clerk_org(clerk_org_id, "Client Co")

    async def _fake_invite(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _fake_invitation(kwargs["email"])

    monkeypatch.setattr(platform_invitations_mod, "fetch_clerk_organization", _fake_fetch)
    monkeypatch.setattr(platform_invitations_mod, "create_organization_invitation", _fake_invite)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.post(
        f"/admin/organizations/{target_org_id}/invitations",
        json={"emailAddress": "new-member@example.com", "role": "member"},
    )
    assert resp.status_code == 201
    assert captured["role"] == "org:member"
    assert captured["redirect_url"].endswith("/sign-up")
    assert not captured["redirect_url"].endswith("/admin/sign-up")


# --- Delete a client organization (Clerk-only) --------------------------------


def test_delete_org_success_audits_and_calls_clerk(client, monkeypatch, owner_conn, platform_org):
    target_org_id = f"test-client-org-{uuid.uuid4().hex[:8]}"
    deleted: list[str] = []

    async def _fake_fetch(clerk_org_id: str) -> dict[str, Any]:
        return _fake_clerk_org(clerk_org_id, "Client Co")

    async def _fake_delete(org_id: str) -> None:
        deleted.append(org_id)

    monkeypatch.setattr(platform_org_delete_mod, "fetch_clerk_organization", _fake_fetch)
    monkeypatch.setattr(platform_org_delete_mod, "delete_organization", _fake_delete)
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.delete(f"/admin/organizations/{target_org_id}")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert deleted == [target_org_id]

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM human_audit_log "
            "WHERE event_type = 'admin_organization_deleted' AND org_id = %s",
            (platform_org["org_pk"],),
        )
        payload = cur.fetchone()[0]
        assert payload["clerk_org_id"] == target_org_id
        assert payload["name"] == "Client Co"


def test_delete_org_platform_org_denied_no_clerk_call(
    client, monkeypatch, owner_conn, platform_org
):
    calls: list[Any] = []
    monkeypatch.setattr(
        platform_org_delete_mod, "fetch_clerk_organization", _record_and_fail(calls)
    )
    monkeypatch.setattr(platform_org_delete_mod, "delete_organization", _record_and_fail(calls))
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.delete(f"/admin/organizations/{platform_org['clerk_org_id']}")
    assert resp.status_code == 403
    assert calls == []


def test_delete_org_nonexistent_org_404(client, monkeypatch, owner_conn, platform_org):
    async def _fake_fetch(clerk_org_id: str) -> dict[str, Any]:
        raise _http_status_error(404)

    calls: list[Any] = []
    monkeypatch.setattr(platform_org_delete_mod, "fetch_clerk_organization", _fake_fetch)
    monkeypatch.setattr(platform_org_delete_mod, "delete_organization", _record_and_fail(calls))
    admin_user_id = f"user-{uuid.uuid4().hex[:8]}"
    _seed_admin(owner_conn, platform_org, admin_user_id, admin_type="platform")
    _authed(platform_org["clerk_org_id"], admin_user_id, "admin")

    resp = client.delete("/admin/organizations/test-missing-org")
    assert resp.status_code == 404
    assert calls == []


def test_delete_org_non_platform_admin_denied(client, owner_conn, seeded_org):
    _authed(seeded_org["clerk_org_id"], f"user-{uuid.uuid4().hex[:8]}", "admin")

    resp = client.delete(f"/admin/organizations/{uuid.uuid4().hex}")
    assert resp.status_code == 403
