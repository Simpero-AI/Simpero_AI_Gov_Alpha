"""GET /deals/{deal_id}/documents (P3-04) -- closes the TODO in
Simpero_AI_Gov_Web's useUploadDocument.ts and the "no listing endpoint
exists yet" callouts in MaterialsCard/DataRoomPane/OverviewPane.

Same harness as tests/test_start_analysis_endpoint.py -- duplicated
fixtures rather than shared, per that module's own precedent.
"""

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_claims
from app.main import app


def _claims(tenant_id: str, user_id: str) -> dict[str, Any]:
    return {"tenant_id": tenant_id, "user_id": user_id, "org_role": "admin", "raw_claims": {}}


class ApiTestClient(TestClient):
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
    clerk_org_id = f"test-docs-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Documents Test Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
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


def _seed_data_source(
    owner_conn, org_pk: int, deal_id: str, filename: str, status: str = "pending", **fields: Any
) -> str:
    columns = {
        "org_id": org_pk,
        "deal_id": deal_id,
        "storage_key": f"org/{uuid.uuid4().hex[:8]}.pdf",
        "filename": filename,
        "declared_sha256": uuid.uuid4().hex + uuid.uuid4().hex,
        **fields,
    }
    cols = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO data_source ({cols}) VALUES ({placeholders}) RETURNING id",
            list(columns.values()),
        )
        data_source_id = cur.fetchone()[0]
        if status != "pending":
            cur.execute(
                "UPDATE data_source SET status = %s, status_updated_at = now() WHERE id = %s",
                (status, data_source_id),
            )
        return str(data_source_id)


def test_404_when_deal_missing(client, seeded_org):
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{uuid.uuid4()}/documents")

    assert resp.status_code == 404


def test_empty_list_when_no_documents_uploaded(client, seeded_org, seeded_deal):
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/documents")

    assert resp.status_code == 200
    assert resp.json() == []


def test_returns_filename_status_created_at_and_nothing_else(
    client, owner_conn, seeded_org, seeded_deal
):
    """Acceptance criterion: no field distinguishes an org-side upload from
    an external-intake one -- checked here by pinning the exact response
    shape, not just spot-checking a couple of fields."""
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "financials.pdf")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/documents")

    assert resp.status_code == 200
    (document,) = resp.json()
    assert set(document.keys()) == {"id", "filename", "status", "createdAt"}
    assert document["filename"] == "financials.pdf"
    assert document["status"] == "pending"
    assert document["createdAt"] is not None


def test_includes_documents_regardless_of_status(client, owner_conn, seeded_org, seeded_deal):
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "a.pdf", status="pending")
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "b.pdf", status="verified")
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "c.pdf", status="quarantined")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/documents")

    assert resp.status_code == 200
    statuses = {document["filename"]: document["status"] for document in resp.json()}
    assert statuses == {"a.pdf": "pending", "b.pdf": "verified", "c.pdf": "quarantined"}


def test_ordered_by_upload_time(client, owner_conn, seeded_org, seeded_deal):
    first = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "first.pdf")
    second = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "second.pdf")
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/documents")

    assert [document["id"] for document in resp.json()] == [first, second]


def test_scoped_to_the_caller_org(client, owner_conn, seeded_org, seeded_deal):
    """RLS: a second org can't see this deal at all -- 404, not an empty
    list, same idiom as GET /deals/{deal_id}."""
    _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "financials.pdf")

    other_clerk_org = f"test-docs-org-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) "
            "VALUES (%s, %s, now()) RETURNING id",
            (other_clerk_org, "Other Org"),
        )
        other_pk = cur.fetchone()[0]
    try:
        _authed(other_clerk_org, "user-2")
        resp = client.get(f"/deals/{seeded_deal}/documents")
        assert resp.status_code == 404
    finally:
        with owner_conn.cursor() as cur:
            for table in ("human_audit_log", "data_source", "deals", "users"):
                cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (other_pk,))
            cur.execute("DELETE FROM organisation WHERE id = %s", (other_pk,))


def test_a_document_from_the_public_intake_path_looks_identical(
    client, owner_conn, seeded_org, seeded_deal
):
    """P3-10 (public presigned-upload + complete) writes to data_source
    through the exact same DataSourceRepo as the org-side upload path (see
    that ticket's description) -- there is no origin column on data_source
    to seed differently here, which is itself the point: an org-side row
    and a future public-intake row are structurally the same row."""
    org_side = _seed_data_source(owner_conn, seeded_org["org_pk"], seeded_deal, "org-upload.pdf")
    public_side = _seed_data_source(
        owner_conn, seeded_org["org_pk"], seeded_deal, "external-upload.pdf"
    )
    _authed(seeded_org["clerk_org_id"], "user-1")

    resp = client.get(f"/deals/{seeded_deal}/documents")

    by_id = {document["id"]: document for document in resp.json()}
    assert set(by_id[org_side].keys()) == set(by_id[public_side].keys())
