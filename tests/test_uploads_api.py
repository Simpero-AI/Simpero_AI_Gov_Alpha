"""Contract tests for POST /uploads/presigned-url (SIM-220 Phase 3) and
POST /uploads/{upload_id}/complete (SIM-216/218 Phase 4).

Mirrors tests/test_phase1_endpoints.py's ApiTestClient/dependency_overrides
pattern, but mounts only app.api.uploads.router on a test-local FastAPI
instance rather than the full app -- app/main.py isn't wired to this router
yet (that's Phase 6's job, out of scope here). The Spaces adapter is mocked
at the call sites app/api/uploads.py imports (build_object_key, presign_put,
head_object) so no real network call happens. The job queue
(app.jobs.queue.get_queue) is likewise mocked at its call site in
app/api/uploads.py, never the real Valkey connection.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import uploads
from app.core.dependencies import get_claims
from app.jobs import parse_client

_ALLOWED_FILENAME = "financials.xlsx"
_MAX_BYTES = uploads.MAX_UPLOAD_BYTES


def _claims(tenant_id: str, user_id: str) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "user_id": user_id, "org_role": "admin", "raw_claims": {}}


@pytest.fixture
def app() -> FastAPI:
    # Test-local app instance, uploads router only -- app/main.py is not
    # touched (Phase 6 wires it in), per this phase's scope.
    test_app = FastAPI()
    test_app.include_router(uploads.router)
    return test_app


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_claims, None)


def _authed(app: FastAPI, tenant_id: str, user_id: str) -> None:
    app.dependency_overrides[get_claims] = lambda: _claims(tenant_id, user_id)


@pytest.fixture
def mocked_spaces(monkeypatch: pytest.MonkeyPatch):
    """Mocks the two Spaces call sites app/api/uploads.py imports directly --
    build_object_key/presign_put -- so a happy-path request never opens a
    real network connection."""
    build_calls: list[tuple] = []
    presign_calls: list[tuple] = []

    def fake_build_object_key(org_name, clerk_org_id, deal_id, upload_id, filename):
        build_calls.append((org_name, clerk_org_id, deal_id, upload_id, filename))
        return f"{org_name}-{clerk_org_id}/{deal_id}/{upload_id}-{filename}"

    def fake_presign_put(key, ttl_seconds):
        presign_calls.append((key, ttl_seconds))
        return f"https://example-spaces.test/{key}?signed=1"

    monkeypatch.setattr(uploads, "build_object_key", fake_build_object_key)
    monkeypatch.setattr(uploads, "presign_put", fake_presign_put)
    return {"build_calls": build_calls, "presign_calls": presign_calls}


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Acme Capital"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk, "org_name": "Acme Capital"}

    with owner_conn.cursor() as cur:
        # human_audit_log first -- Phase 4's /complete tests are the first in
        # this file to actually write a row there, and its org_id FK blocks
        # the organisation delete below otherwise (doadmin can still DELETE
        # human_audit_log rows; only dd_app has UPDATE/DELETE revoked).
        for table in ("human_audit_log", "data_source", "deals", "users"):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def seeded_deal(owner_conn, seeded_org) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (seeded_org["org_pk"], "Acme Deal"),
        )
        return str(cur.fetchone()[0])


def _seed_data_source(owner_conn, org_pk: int, deal_id: str, declared_sha256: str, **fields):
    columns = {
        "org_id": org_pk,
        "deal_id": deal_id,
        "storage_key": "existing/key.xlsx",
        "filename": "financials.xlsx",
        "declared_sha256": declared_sha256,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO data_source ({cols}) VALUES ({placeholders}) RETURNING id",
            list(columns.values()),
        )
        return str(cur.fetchone()[0])


def _presign_body(deal_id: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "dealId": deal_id,
        "filename": _ALLOWED_FILENAME,
        "size": 1024,
        "declaredSha256": "a" * 64,
    }
    body.update(overrides)
    return body


# --- size guard --------------------------------------------------------


def test_oversized_declared_size_rejected_no_spaces_call(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_spaces
):
    _authed(app, seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        "/uploads/presigned-url",
        json=_presign_body(seeded_deal, size=_MAX_BYTES + 1),
    )

    assert resp.status_code == 422
    assert not mocked_spaces["build_calls"]
    assert not mocked_spaces["presign_calls"]


# --- type guard ----------------------------------------------------------


def test_disallowed_file_type_rejected_no_spaces_call(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_spaces
):
    _authed(app, seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        "/uploads/presigned-url",
        json=_presign_body(seeded_deal, filename="malware.exe"),
    )

    assert resp.status_code == 422
    assert not mocked_spaces["build_calls"]
    assert not mocked_spaces["presign_calls"]


def test_no_extension_filename_rejected(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_spaces
):
    _authed(app, seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        "/uploads/presigned-url",
        json=_presign_body(seeded_deal, filename="noextension"),
    )

    assert resp.status_code == 422
    assert not mocked_spaces["build_calls"]


# --- dedupe ----------------------------------------------------------------


def test_duplicate_declared_sha256_pending_row_returns_409(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_spaces
):
    declared_hash = "b" * 64
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, declared_hash)
    _authed(app, seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        "/uploads/presigned-url",
        json=_presign_body(seeded_deal, declaredSha256=declared_hash),
    )

    assert resp.status_code == 409
    assert not mocked_spaces["build_calls"]
    assert not mocked_spaces["presign_calls"]


def test_duplicate_via_fingerprint_of_verified_row_returns_409(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_spaces
):
    fingerprint_hash = "c" * 64
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_source (org_id, deal_id, storage_key, filename, declared_sha256) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (seeded_org["org_pk"], seeded_deal, "existing/key2.xlsx", "financials.xlsx", "d" * 64),
        )
        data_source_id = cur.fetchone()[0]
        cur.execute(
            "UPDATE data_source SET status = 'verified', fingerprint = %s, "
            "status_updated_at = now() WHERE id = %s",
            (fingerprint_hash, data_source_id),
        )
    _authed(app, seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        "/uploads/presigned-url",
        json=_presign_body(seeded_deal, declaredSha256=fingerprint_hash),
    )

    assert resp.status_code == 409


def test_mismatch_row_does_not_block_reupload(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_spaces
):
    declared_hash = "e" * 64
    data_source_id = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, declared_hash)
    with owner_conn.cursor() as cur:
        cur.execute(
            "UPDATE data_source SET status = 'mismatch', fingerprint = %s, "
            "status_updated_at = now() WHERE id = %s",
            ("f" * 64, data_source_id),
        )
    _authed(app, seeded_org["clerk_org_id"], "user-1")

    resp = client.post(
        "/uploads/presigned-url",
        json=_presign_body(seeded_deal, declaredSha256=declared_hash),
    )

    assert resp.status_code == 200


# --- happy path --------------------------------------------------------


def test_happy_path_returns_presigned_response_with_matching_key_shape(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_spaces
):
    _authed(app, seeded_org["clerk_org_id"], "user-1")

    resp = client.post("/uploads/presigned-url", json=_presign_body(seeded_deal))

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"uploadId", "presignedUrl", "storageKey"}
    upload_id = body["uploadId"]
    expected_key = (
        f"{seeded_org['org_name']}-{seeded_org['clerk_org_id']}/{seeded_deal}/"
        f"{upload_id}-{_ALLOWED_FILENAME}"
    )
    assert body["storageKey"] == expected_key
    assert body["presignedUrl"].endswith("?signed=1")

    # Exactly one Spaces call each, with the right args -- confirms the
    # handler derives the key/URL itself rather than trusting the client.
    assert len(mocked_spaces["build_calls"]) == 1
    assert len(mocked_spaces["presign_calls"]) == 1
    key_arg, ttl_arg = mocked_spaces["presign_calls"][0]
    assert key_arg == expected_key
    assert ttl_arg == 600

    # No DB write in this handler.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM data_source WHERE deal_id = %s", (seeded_deal,))
        assert cur.fetchone()[0] == 0


# --- POST /uploads/{upload_id}/complete (SIM-216/218 Phase 4) --------------


def _complete_body(deal_id: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "dealId": deal_id,
        "filename": _ALLOWED_FILENAME,
        "declaredSha256": "1" * 64,
    }
    body.update(overrides)
    return body


@pytest.fixture
def mocked_complete(monkeypatch: pytest.MonkeyPatch):
    """Mocks the Spaces/queue call sites app/api/uploads.py imports directly
    (build_object_key, head_object, get_queue) so /complete never opens a
    real network connection. Also patches app.jobs.parse_client.get_parse_queue
    to raise if it's ever called -- that's a DIFFERENT Valkey queue ("parse")
    for a different service's worker; /complete must enqueue only on
    get_queue()'s "simpero" queue, never this one (see CLAUDE.md's documented
    "silent drop" hazard if the two get swapped).
    """
    build_calls: list[tuple] = []
    enqueue_calls: list[tuple[str, dict[str, Any]]] = []
    object_exists = {"value": True}

    def fake_build_object_key(org_name, clerk_org_id, deal_id, upload_id, filename):
        build_calls.append((org_name, clerk_org_id, deal_id, upload_id, filename))
        return f"{org_name}-{clerk_org_id}/{deal_id}/{upload_id}-{filename}"

    def fake_head_object(key: str) -> bool:
        return object_exists["value"]

    class _FakeQueue:
        async def enqueue(self, job_name: str, **kwargs: Any) -> None:
            enqueue_calls.append((job_name, kwargs))
            return None

    fake_queue = _FakeQueue()

    def _fail_if_called() -> None:
        raise AssertionError(
            "get_parse_queue() must never be called from /complete -- see "
            "CLAUDE.md's 'simpero' vs 'parse' queue hazard"
        )

    monkeypatch.setattr(uploads, "build_object_key", fake_build_object_key)
    monkeypatch.setattr(uploads, "head_object", fake_head_object)
    monkeypatch.setattr(uploads, "get_queue", lambda: fake_queue)
    monkeypatch.setattr(parse_client, "get_parse_queue", _fail_if_called)

    return {
        "build_calls": build_calls,
        "enqueue_calls": enqueue_calls,
        "set_object_exists": lambda v: object_exists.__setitem__("value", v),
    }


def _count_data_source(owner_conn, deal_id: str) -> int:
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM data_source WHERE deal_id = %s", (deal_id,))
        return cur.fetchone()[0]


def _count_audit_rows(owner_conn, org_pk: int) -> int:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM human_audit_log WHERE org_id = %s "
            "AND event_type = 'document_upload_completed'",
            (org_pk,),
        )
        return cur.fetchone()[0]


def test_complete_without_prior_put_returns_4xx_no_row_no_audit_no_job(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_complete
):
    mocked_complete["set_object_exists"](False)
    _authed(app, seeded_org["clerk_org_id"], "user-1")
    upload_id = str(uuid.uuid4())

    resp = client.post(f"/uploads/{upload_id}/complete", json=_complete_body(seeded_deal))

    assert 400 <= resp.status_code < 500
    assert _count_data_source(owner_conn, seeded_deal) == 0
    assert _count_audit_rows(owner_conn, seeded_org["org_pk"]) == 0
    assert not mocked_complete["enqueue_calls"]


def test_complete_happy_path_creates_row_enqueues_one_job_writes_one_audit_row(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_complete
):
    _authed(app, seeded_org["clerk_org_id"], "user-1")
    upload_id = str(uuid.uuid4())

    resp = client.post(f"/uploads/{upload_id}/complete", json=_complete_body(seeded_deal))

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"id": upload_id, "status": "pending"}

    # Exactly one data_source row, status='pending'.
    with owner_conn.cursor() as cur:
        cur.execute("SELECT status, storage_key FROM data_source WHERE id = %s", (upload_id,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "pending"
    assert _count_data_source(owner_conn, seeded_deal) == 1

    # Exactly one job enqueued on get_queue()'s "simpero" queue -- never
    # get_parse_queue()'s "parse" queue (mocked_complete raises if that one
    # is ever touched).
    assert len(mocked_complete["enqueue_calls"]) == 1
    job_name, kwargs = mocked_complete["enqueue_calls"][0]
    assert job_name == "ingest_data_source"
    assert kwargs["data_source_id"] == upload_id
    assert kwargs["clerk_org_id"] == seeded_org["clerk_org_id"]
    assert kwargs["storage_key"] == row[1]
    assert kwargs["declared_sha256"] == "1" * 64
    # SAQ's 10s default timeout is shorter than stream_and_hash's real round
    # trip to Spaces -- see app/api/uploads.py's enqueue call.
    assert kwargs["timeout"] == 120
    assert kwargs["retries"] == 2

    # Exactly one human_audit_log row.
    assert _count_audit_rows(owner_conn, seeded_org["org_pk"]) == 1


async def test_complete_row_invisible_to_other_org_via_rls(
    app, client, owner_conn, seeded_org, seeded_deal, mocked_complete, db_session
):
    """RLS check: the row /complete creates for seeded_org (org A) must be
    invisible to a dd_app session scoped to a different org's app.org_id
    (db_session, conftest's fixed test_org_id -- "org B" here) -- confirms the
    insert respected tenant scoping, not just that it succeeded.
    """
    _authed(app, seeded_org["clerk_org_id"], "user-1")
    upload_id = str(uuid.uuid4())

    resp = client.post(f"/uploads/{upload_id}/complete", json=_complete_body(seeded_deal))
    assert resp.status_code == 200

    result = await db_session.execute(
        text("SELECT id FROM data_source WHERE id = :id"), {"id": upload_id}
    )
    assert result.first() is None
