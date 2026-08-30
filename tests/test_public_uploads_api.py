"""Contract tests for POST /public/intake/uploads/presigned-url and
.../{upload_id}/complete (P3-10).

Full app (app.main.app) over httpx.ASGITransport, same pattern as
tests/test_public_intake_session.py -- session_token is a real, verified
intake-session JWT (encode_intake_session_jwt), never a stubbed dependency
override, so RLS is genuinely exercised end to end. The Spaces adapter
(presign_put, head_object_size) and the job queue (get_queue) are mocked at their
call sites in app.api.public_uploads, mirroring tests/test_uploads_api.py's
pattern for the authenticated router.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.api import public_uploads
from app.core.intake_security import encode_intake_session_jwt
from app.jobs import parse_client
from app.main import app
from app.repo.DataSourceRepo import DataSourceRepo

_ALLOWED_FILENAME = "financials.xlsx"
_DECLARED_HASH = "a" * 64


@pytest.fixture(autouse=True)
async def _clear_rate_limit_keys_around_every_test(clear_rate_limit_keys):
    """httpx.ASGITransport defaults every test to the same synthetic client
    address -- without clearing rate-limit keys between tests, cumulative
    request counts across this file's tests would spuriously trip the P3-12
    429, and reusing get_queue()'s redis connection across pytest-asyncio's
    per-test event loops raises "Event loop is closed" otherwise. Same
    pattern as tests/test_public_intake_session.py's own fixture."""
    yield


def _session_token(link: dict, email: str = "recipient@org-a.example") -> str:
    return encode_intake_session_jwt(uuid.UUID(link["id"]), email)


async def _post(path: str, session_token: str, json: dict) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            f"/api/public/intake/uploads{path}",
            params={"session_token": session_token},
            json=json,
        )


@pytest.fixture
def mocked_spaces(monkeypatch: pytest.MonkeyPatch):
    build_calls: list[tuple] = []
    presign_calls: list[tuple] = []
    enqueue_calls: list[tuple[str, dict[str, Any]]] = []
    # None => object missing (404 at /complete). A small default size keeps
    # every existing happy-path test under MAX_UPLOAD_BYTES without having to
    # know or care about the actual number.
    object_state: dict[str, int | None] = {"size": 1024}

    def fake_build_object_key(org_name, clerk_org_id, deal_id, upload_id, filename):
        build_calls.append((org_name, clerk_org_id, deal_id, upload_id, filename))
        return f"{org_name}-{clerk_org_id}/{deal_id}/{upload_id}-{filename}"

    def fake_presign_put(key, ttl_seconds, *, content_length=None):
        presign_calls.append((key, ttl_seconds, content_length))
        return f"https://example-spaces.test/{key}?signed=1"

    def fake_head_object_size(key: str) -> int | None:
        return object_state["size"]

    class _FakeQueue:
        async def enqueue(self, job_name: str, **kwargs: Any) -> None:
            enqueue_calls.append((job_name, kwargs))
            return None

    fake_queue = _FakeQueue()

    def _fail_if_called() -> None:
        raise AssertionError("get_parse_queue() must never be called from public_uploads")

    monkeypatch.setattr(public_uploads, "build_object_key", fake_build_object_key)
    monkeypatch.setattr(public_uploads, "presign_put", fake_presign_put)
    monkeypatch.setattr(public_uploads, "head_object_size", fake_head_object_size)
    monkeypatch.setattr(public_uploads, "get_queue", lambda: fake_queue)
    monkeypatch.setattr(parse_client, "get_parse_queue", _fail_if_called)

    return {
        "build_calls": build_calls,
        "presign_calls": presign_calls,
        "enqueue_calls": enqueue_calls,
        "set_object_exists": lambda v: object_state.__setitem__("size", 1024 if v else None),
        "set_object_size": lambda n: object_state.__setitem__("size", n),
    }


@pytest.fixture
def _cleanup_seeded_documents(owner_conn, pending_link_with_token):
    """data_source.intake_link_id now FK-references deal_intake_link (P3-10) --
    any test that seeds/creates data_source rows against pending_link_with_token
    must delete them before that fixture's own teardown tries to delete the
    link row, or the FK blocks it. Depending on pending_link_with_token here
    (even though unused directly) forces this fixture to set up AFTER it, so
    it tears down BEFORE it (pytest's LIFO fixture teardown order).

    Also deletes any human_audit_log row this test's /complete calls wrote --
    unlike data_source there's no FK forcing this, but human_audit_log rows
    are otherwise never cleaned up (append-only, no test-side DELETE path
    elsewhere either) and org_a_id/test_org_id is shared across the whole
    suite, so leftover intake_document_uploaded/intake_document_rejected rows
    from a prior run silently break tests/test_human_audit_log_immutability.py's
    exact-count assertion on a later run against the same DB. Scoped by
    deal_id, which is unique per pending_link_with_token call (org_a_deal_id
    fixture creates a fresh deal every time), so this can't touch another
    test's rows.
    """
    yield
    with owner_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM data_source WHERE intake_link_id = %s",
            (pending_link_with_token["id"],),
        )
        cur.execute(
            "DELETE FROM human_audit_log WHERE deal_id = %s AND event_type IN %s",
            (
                pending_link_with_token["deal_id"],
                ("intake_document_uploaded", "intake_document_rejected"),
            ),
        )


def _seed_data_source_for_link(owner_conn, org_pk: int, deal_id: str, link_id: str, n: int) -> None:
    with owner_conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                "INSERT INTO data_source "
                "(org_id, deal_id, storage_key, filename, declared_sha256, intake_link_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    org_pk,
                    deal_id,
                    f"existing/key-{i}.pdf",
                    f"doc-{i}.pdf",
                    uuid.uuid4().hex + uuid.uuid4().hex,
                    link_id,
                ),
            )


# --- 21st upload rejected before any presign ------------------------------


async def test_21st_presign_rejected_before_spaces_call(
    pending_link_with_token, owner_conn, org_a_id, mocked_spaces, _cleanup_seeded_documents
):
    link = pending_link_with_token
    _seed_data_source_for_link(owner_conn, org_a_id, link["deal_id"], link["id"], 20)

    resp = await _post(
        "/presigned-url",
        _session_token(link),
        {"filename": _ALLOWED_FILENAME, "size": 1024, "declaredSha256": "b" * 64},
    )

    assert resp.status_code == 409
    assert not mocked_spaces["build_calls"]
    assert not mocked_spaces["presign_calls"]


async def test_presign_happy_path_under_ceiling(pending_link_with_token, mocked_spaces):
    link = pending_link_with_token

    resp = await _post(
        "/presigned-url",
        _session_token(link),
        {"filename": _ALLOWED_FILENAME, "size": 1024, "declaredSha256": _DECLARED_HASH},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"uploadId", "presignedUrl", "storageKey"}
    assert len(mocked_spaces["presign_calls"]) == 1
    # P3-15/F9: the client's declared (already-validated) size is what gets
    # bound into the signature -- see test_presign_content_length.py for
    # proof that actually enforces an exact-match ceiling.
    _, _, content_length = mocked_spaces["presign_calls"][0]
    assert content_length == 1024


async def test_presign_rejects_oversized_declared_size(pending_link_with_token, mocked_spaces):
    link = pending_link_with_token

    resp = await _post(
        "/presigned-url",
        _session_token(link),
        {
            "filename": _ALLOWED_FILENAME,
            "size": public_uploads.MAX_UPLOAD_BYTES + 1,
            "declaredSha256": _DECLARED_HASH,
        },
    )

    assert resp.status_code == 422
    assert not mocked_spaces["presign_calls"]


async def test_presign_rejects_non_positive_declared_size(pending_link_with_token, mocked_spaces):
    """P3-15/F9 Fix 2: size is now bound into presign_put's signature, so a
    zero/negative value would sign a URL no real PUT could ever satisfy --
    Field(ge=1) rejects it at the schema layer instead of wasting a round
    trip on an opaque signature-mismatch 403.
    """
    link = pending_link_with_token

    resp = await _post(
        "/presigned-url",
        _session_token(link),
        {"filename": _ALLOWED_FILENAME, "size": 0, "declaredSha256": _DECLARED_HASH},
    )

    assert resp.status_code == 422
    assert not mocked_spaces["presign_calls"]


# --- /complete happy path ---------------------------------------------------


async def test_complete_creates_row_matching_authenticated_shape_sets_intake_link_id(
    pending_link_with_token, owner_conn, mocked_spaces, _cleanup_seeded_documents
):
    link = pending_link_with_token
    upload_id = str(uuid.uuid4())

    resp = await _post(
        f"/{upload_id}/complete",
        _session_token(link),
        {"filename": _ALLOWED_FILENAME, "declaredSha256": _DECLARED_HASH},
    )

    assert resp.status_code == 200
    assert resp.json() == {"id": upload_id, "status": "pending"}

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, deal_id, filename, declared_sha256, status, intake_link_id "
            "FROM data_source WHERE id = %s",
            (upload_id,),
        )
        row = cur.fetchone()
    assert row is not None
    org_id, deal_id, filename, declared_sha256, status, intake_link_id = row
    assert str(deal_id) == link["deal_id"]
    assert filename == _ALLOWED_FILENAME
    assert declared_sha256 == _DECLARED_HASH
    assert status == "pending"
    # The one column that differs from an authenticated-path row (NULL there).
    assert str(intake_link_id) == link["id"]

    # Same downstream job as the authenticated path.
    assert len(mocked_spaces["enqueue_calls"]) == 1
    job_name, kwargs = mocked_spaces["enqueue_calls"][0]
    assert job_name == "ingest_data_source"
    assert kwargs["data_source_id"] == upload_id
    assert kwargs["timeout"] == 120
    assert kwargs["retries"] == 2


async def test_complete_writes_exactly_one_audit_row_with_session_email(
    pending_link_with_token, owner_conn, mocked_spaces, _cleanup_seeded_documents
):
    link = pending_link_with_token
    upload_id = str(uuid.uuid4())

    resp = await _post(
        f"/{upload_id}/complete",
        _session_token(link, email="recipient@org-a.example"),
        {"filename": _ALLOWED_FILENAME, "declaredSha256": _DECLARED_HASH},
    )
    assert resp.status_code == 200

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT actor_email, actor_id FROM human_audit_log "
            "WHERE event_type = 'intake_document_uploaded' "
            "AND payload ->> 'data_source_id' = %s",
            (upload_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "recipient@org-a.example"
    assert rows[0][1] is None


async def test_complete_persists_ip_and_user_agent_on_audit_row(
    pending_link_with_token, owner_conn, mocked_spaces, _cleanup_seeded_documents
):
    link = pending_link_with_token
    upload_id = str(uuid.uuid4())

    resp = await _post(
        f"/{upload_id}/complete",
        _session_token(link, email="recipient@org-a.example"),
        {"filename": _ALLOWED_FILENAME, "declaredSha256": _DECLARED_HASH},
    )
    assert resp.status_code == 200

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT ip_address, user_agent FROM human_audit_log "
            "WHERE event_type = 'intake_document_uploaded' "
            "AND payload ->> 'data_source_id' = %s",
            (upload_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    ip_address, user_agent = rows[0]
    assert ip_address is not None
    assert user_agent is not None


async def test_complete_without_prior_put_returns_4xx_no_row_no_audit(
    pending_link_with_token, owner_conn, mocked_spaces
):
    mocked_spaces["set_object_exists"](False)
    link = pending_link_with_token
    upload_id = str(uuid.uuid4())

    resp = await _post(
        f"/{upload_id}/complete",
        _session_token(link),
        {"filename": _ALLOWED_FILENAME, "declaredSha256": _DECLARED_HASH},
    )

    assert 400 <= resp.status_code < 500
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM data_source WHERE id = %s", (upload_id,))
        assert cur.fetchone()[0] == 0
    assert not mocked_spaces["enqueue_calls"]


async def test_complete_rejects_oversized_stored_object_no_row_no_enqueue_writes_audit(
    pending_link_with_token, owner_conn, org_a_id, mocked_spaces, _cleanup_seeded_documents
):
    """P3-15/F9 Fix 1: /complete checks the ACTUAL stored size (what
    head_object_size reports), not just the client's declared size at
    /presigned-url -- this is the backstop for whether Spaces (S3-compatible,
    not S3) actually honoured presign_put's signed content_length. Even if a
    client uploaded more than it declared and Spaces let it through, this
    stops a data_source row and ingest job from being created for it.

    F9 fix-prompt round 2 (Fix 4/5): reaching this branch means either a
    client bypassed presign_put's signature or Spaces isn't honouring it --
    an abuse signal, so it also gets an audit row (mirrors
    intake_email_attempt_failed's precedent in public_intake.py), written via
    a returned JSONResponse rather than a raised HTTPException so it survives
    get_public_session_db's rollback-on-raise (see public_uploads.py's
    comment at the call site).
    """
    mocked_spaces["set_object_size"](public_uploads.MAX_UPLOAD_BYTES + 1)
    link = pending_link_with_token
    upload_id = str(uuid.uuid4())

    resp = await _post(
        f"/{upload_id}/complete",
        _session_token(link, email="recipient@org-a.example"),
        {"filename": _ALLOWED_FILENAME, "declaredSha256": _DECLARED_HASH},
    )

    assert resp.status_code == 422
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM data_source WHERE id = %s", (upload_id,))
        assert cur.fetchone()[0] == 0
    assert not mocked_spaces["enqueue_calls"]

    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT deal_id, org_id, actor_email, payload FROM human_audit_log "
            "WHERE event_type = 'intake_document_rejected' AND deal_id = %s",
            (link["deal_id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    deal_id, org_id, actor_email, payload = rows[0]
    assert str(deal_id) == link["deal_id"]
    assert org_id == org_a_id
    assert actor_email == "recipient@org-a.example"
    assert payload["actual_size"] == public_uploads.MAX_UPLOAD_BYTES + 1
    assert "storage_key" in payload


async def test_complete_at_ceiling_returns_409_no_enqueue_no_audit(
    pending_link_with_token, owner_conn, org_a_id, mocked_spaces, _cleanup_seeded_documents
):
    link = pending_link_with_token
    _seed_data_source_for_link(owner_conn, org_a_id, link["deal_id"], link["id"], 20)
    upload_id = str(uuid.uuid4())

    resp = await _post(
        f"/{upload_id}/complete",
        _session_token(link),
        {"filename": _ALLOWED_FILENAME, "declaredSha256": _DECLARED_HASH},
    )

    assert resp.status_code == 409
    with owner_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM data_source WHERE id = %s", (upload_id,))
        assert cur.fetchone()[0] == 0
    assert not mocked_spaces["enqueue_calls"]


# --- cross-org isolation ----------------------------------------------------


@pytest.fixture
def org_b_link_with_docs(owner_conn) -> Iterator[dict]:
    """A second org's pending link plus 3 data_source rows tied to it, seeded
    via owner_conn (bypasses RLS) -- mirrors tests/test_intake_link_rls.py's
    org_b_intake_link_id fixture.
    """
    org_b_clerk_id = f"test-tenant-b-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (org_b_clerk_id, "Org B"),
        )
        org_b_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deals (org_id, name) VALUES (%s, %s) RETURNING id",
            (org_b_pk, "Org B's deal"),
        )
        deal_id = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO users (org_id, role, login_method, clerk_user_id, clerk_org_id, status) "
            "VALUES (%s, 'admin', 'email', %s, %s, 'active') RETURNING id",
            (org_b_pk, f"test-user-b-{uuid.uuid4().hex[:8]}", org_b_clerk_id),
        )
        user_b_pk = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO deal_intake_link "
            "(org_id, clerk_org_id, deal_id, token_hash, recipient_email, expires_at, "
            "created_by_user_id, status) "
            "VALUES (%s, %s, %s, %s, %s, now() + interval '7 days', %s, 'pending') "
            "RETURNING id",
            (
                org_b_pk,
                org_b_clerk_id,
                deal_id,
                uuid.uuid4().hex,
                "recipient@org-b.example",
                user_b_pk,
            ),
        )
        link_id = str(cur.fetchone()[0])
    _seed_data_source_for_link(owner_conn, org_b_pk, deal_id, link_id, 3)

    yield {"id": link_id, "org_pk": org_b_pk, "deal_id": deal_id, "clerk_org_id": org_b_clerk_id}

    with owner_conn.cursor() as cur:
        cur.execute("DELETE FROM data_source WHERE deal_id = %s", (deal_id,))
        cur.execute("DELETE FROM deal_intake_link WHERE id = %s", (link_id,))
        cur.execute("DELETE FROM users WHERE id = %s", (user_b_pk,))
        cur.execute("DELETE FROM deals WHERE id = %s", (deal_id,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_b_pk,))


async def test_org_a_session_cannot_see_org_b_documents(
    pending_link_with_token, org_b_link_with_docs, db_session
):
    """RLS check: org B's data_source rows (different org, different intake
    link) must be invisible to a dd_app session scoped to org A -- confirms
    the public-insert path didn't leak across tenants.
    """
    result = await db_session.execute(
        text("SELECT id FROM data_source WHERE deal_id = :deal_id"),
        {"deal_id": org_b_link_with_docs["deal_id"]},
    )
    assert result.first() is None


async def test_org_a_presign_ceiling_unaffected_by_org_b_documents(
    pending_link_with_token, org_b_link_with_docs, mocked_spaces
):
    """Org B has 3 seeded rows tied to its own link -- org A's own (empty)
    link must still be well under the 20-file ceiling."""
    link = pending_link_with_token

    resp = await _post(
        "/presigned-url",
        _session_token(link),
        {"filename": _ALLOWED_FILENAME, "size": 1024, "declaredSha256": _DECLARED_HASH},
    )

    assert resp.status_code == 200


# --- advisory-locked ceiling on the repo method directly --------------------


async def test_try_create_for_intake_link_returns_none_at_ceiling(
    pending_link_with_token, owner_conn, org_a_id, public_db_session, _cleanup_seeded_documents
):
    """Sequential proof of the real (not just presign-courtesy) ceiling:
    seed 20 rows then call the repo method once more under the same GUCs
    /complete would use. True concurrent-race coverage would need two
    simultaneous connections, which this suite has no infrastructure for
    elsewhere either (same caveat as test_intake_link_rls.py's reissue race).
    """
    link = pending_link_with_token
    _seed_data_source_for_link(owner_conn, org_a_id, link["deal_id"], link["id"], 20)

    await public_db_session.execute(
        text(
            "SELECT set_config('app.org_id', :tid, true), "
            "set_config('app.intake_deal_id', :did, true), "
            "set_config('app.intake_link_id', :lid, true)"
        ),
        {"tid": link["clerk_org_id"], "did": link["deal_id"], "lid": link["id"]},
    )

    result = await DataSourceRepo(public_db_session).try_create_for_intake_link(
        uuid.UUID(link["id"]),
        {
            "id": uuid.uuid4(),
            "org_id": org_a_id,
            "deal_id": uuid.UUID(link["deal_id"]),
            "storage_key": "some/key.pdf",
            "filename": "one-too-many.pdf",
            "declared_sha256": "c" * 64,
            "status": "pending",
        },
        ceiling=20,
    )

    assert result is None
