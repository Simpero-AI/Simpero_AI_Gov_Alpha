"""Phase 6 wiring check: confirms app/main.py actually mounts uploads.router
at its real prefixed path (/api/uploads/...), through the real app instance --
not the test-local FastAPI() app tests/test_uploads_api.py builds in
isolation. Mirrors tests/test_phase1_endpoints.py's ApiTestClient +
dependency_overrides pattern for app.main.app. Spaces calls are mocked at their
app/api/uploads.py call sites and the queue at its source (app.jobs.queue),
same as test_uploads_api.py -- this test is about route wiring, not re-covering
the guard/dedupe logic already covered there.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import uploads
from app.core.dependencies import get_claims
from app.main import app

_ALLOWED_FILENAME = "financials.xlsx"


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
            (clerk_org_id, "Wiring Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk, "org_name": "Wiring Test Org"}

    with owner_conn.cursor() as cur:
        for table in ("human_audit_log", "data_source", "deals", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Wiring Test Deal"),
        )
        return str(cur.fetchone()[0])


@pytest.fixture
def mocked_spaces_and_queue(monkeypatch: pytest.MonkeyPatch):
    """Same mocking approach as test_uploads_api.py, applied here so hitting
    the real app never opens a real Spaces/Valkey connection."""

    def fake_build_object_key(org_name, clerk_org_id, deal_id, upload_id, filename):
        return f"{org_name}-{clerk_org_id}/{deal_id}/{upload_id}-{filename}"

    def fake_presign_put(key, ttl_seconds):
        return f"https://example-spaces.test/{key}?signed=1"

    def fake_head_object(key: str) -> bool:
        return True

    class _FakeQueue:
        async def enqueue(self, job_name: str, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(uploads, "build_object_key", fake_build_object_key)
    monkeypatch.setattr(uploads, "presign_put", fake_presign_put)
    monkeypatch.setattr(uploads, "head_object", fake_head_object)
    # Patched at the source, not on `uploads`: complete_upload no longer holds a
    # get_queue reference -- it registers enqueue_ingest_data_source as a post-commit
    # hook, and that helper (in app.jobs.queue) resolves get_queue from its own
    # module globals at call time.
    monkeypatch.setattr("app.jobs.queue.get_queue", lambda: _FakeQueue())


def test_presigned_url_reachable_through_real_app(
    client, owner_conn, seeded_org, seeded_deal, mocked_spaces_and_queue
):
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        "/uploads/presigned-url",
        json={
            "dealId": seeded_deal,
            "filename": _ALLOWED_FILENAME,
            "size": 1024,
            "declaredSha256": "a" * 64,
        },
    )

    assert resp.status_code == 200, resp.text
    assert set(resp.json().keys()) == {"uploadId", "presignedUrl", "storageKey"}


def test_complete_reachable_through_real_app(
    client, owner_conn, seeded_org, seeded_deal, mocked_spaces_and_queue
):
    _authed(seeded_org["clerk_org_id"], "user-1")
    upload_id = str(uuid.uuid4())

    resp = client.post(
        f"/uploads/{upload_id}/complete",
        json={
            "dealId": seeded_deal,
            "filename": _ALLOWED_FILENAME,
            "declaredSha256": "b" * 64,
        },
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": upload_id, "status": "pending", "pageCount": None}
