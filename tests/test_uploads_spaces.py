"""app/services/uploads/spaces.py -- the boto3 adapter for document uploads
(SIM-220/216/218 Phase 2).

No moto dependency in this repo (checked pyproject.toml before writing this --
not adding one). boto3.client is mocked directly instead; what matters here is
the *call shape* (right bucket/key/params) and the streaming behavior of
stream_and_hash, not a real S3-compatible backend.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import UUID

import pytest
from botocore.exceptions import ClientError

from app.services.uploads import spaces

FAKE_BUCKET = "test-bucket"


@pytest.fixture(autouse=True)
def _isolated_client(monkeypatch: pytest.MonkeyPatch):
    """Every test gets its own fake Settings + a fresh, mocked boto3 client --
    _client() is an lru_cache singleton in production (same idiom as
    get_queue/get_parse_queue), so it must be cleared between tests or one
    test's mock client would leak into the next.
    """
    fake_settings = SimpleNamespace(
        spaces_bucket=FAKE_BUCKET,
        spaces_region="tor1",
        spaces_endpoint_url="https://tor1.digitaloceanspaces.com",
        spaces_access_key_id="fake-key",
        spaces_secret_access_key="fake-secret",
    )
    monkeypatch.setattr(spaces, "get_settings", lambda: fake_settings)

    from unittest.mock import MagicMock

    mock_client = MagicMock(name="boto3_s3_client")
    monkeypatch.setattr(spaces.boto3, "client", MagicMock(return_value=mock_client))
    spaces._client.cache_clear()
    yield mock_client
    spaces._client.cache_clear()


DEAL_ID = UUID("11111111-1111-1111-1111-111111111111")
UPLOAD_ID = UUID("22222222-2222-2222-2222-222222222222")


def test_build_object_key_shape():
    key = spaces.build_object_key("Acme Corp", "org_abc123", DEAL_ID, UPLOAD_ID, "financials.xlsx")
    assert key == f"Acme_Corp-org_abc123/{DEAL_ID}/{UPLOAD_ID}-financials.xlsx"


def test_build_object_key_sanitizes_org_name():
    # Slashes, colons, spaces -- anything outside [A-Za-z0-9._-] -- become "_".
    key = spaces.build_object_key("Acme/Corp: Q4 Fund", "org_abc123", DEAL_ID, UPLOAD_ID, "f.pdf")
    org_part = key.split("-org_abc123")[0]
    assert org_part == "Acme_Corp__Q4_Fund"


def test_build_object_key_does_not_include_bucket():
    key = spaces.build_object_key("Acme", "org_abc123", DEAL_ID, UPLOAD_ID, "f.pdf")
    assert FAKE_BUCKET not in key


def test_presign_put_calls_generate_presigned_url_with_right_params(_isolated_client):
    mock_client = _isolated_client
    mock_client.generate_presigned_url.return_value = "https://example.com/signed"

    url = spaces.presign_put("acme-org_abc/deal/upload-file.pdf", ttl_seconds=600)

    assert url == "https://example.com/signed"
    mock_client.generate_presigned_url.assert_called_once_with(
        "put_object",
        Params={"Bucket": FAKE_BUCKET, "Key": "acme-org_abc/deal/upload-file.pdf"},
        ExpiresIn=600,
    )


def test_presign_put_with_content_length_adds_it_to_params(_isolated_client):
    """The authenticated app/api/uploads.py path never passes content_length
    -- this call shape is exercised only by public_uploads.py (P3-15/F9).
    See tests/test_presign_content_length.py for proof this actually binds
    into the signature, not just the call shape.
    """
    mock_client = _isolated_client
    mock_client.generate_presigned_url.return_value = "https://example.com/signed"

    spaces.presign_put("acme-org_abc/deal/upload-file.pdf", ttl_seconds=600, content_length=1024)

    mock_client.generate_presigned_url.assert_called_once_with(
        "put_object",
        Params={
            "Bucket": FAKE_BUCKET,
            "Key": "acme-org_abc/deal/upload-file.pdf",
            "ContentLength": 1024,
        },
        ExpiresIn=600,
    )


def test_head_object_true_on_success(_isolated_client):
    mock_client = _isolated_client
    mock_client.head_object.return_value = {}

    assert spaces.head_object("some-key") is True
    mock_client.head_object.assert_called_once_with(Bucket=FAKE_BUCKET, Key="some-key")


def test_head_object_false_on_404(_isolated_client):
    mock_client = _isolated_client
    mock_client.head_object.side_effect = ClientError(
        {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
    )

    assert spaces.head_object("missing-key") is False


def test_head_object_reraises_non_404_errors(_isolated_client):
    mock_client = _isolated_client
    mock_client.head_object.side_effect = ClientError(
        {"Error": {"Code": "403", "Message": "Forbidden"}}, "HeadObject"
    )

    with pytest.raises(ClientError):
        spaces.head_object("forbidden-key")


class _FakeStreamingBody:
    """Mimics botocore's StreamingBody: read(n) returns at most n bytes per
    call, never the whole payload in one shot -- exactly what stream_and_hash
    must be able to consume without ever calling read() with no size arg.
    """

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0
        self.read_calls: list[int | None] = []

    def read(self, size: int | None = None) -> bytes:
        self.read_calls.append(size)
        if size is None:
            raise AssertionError("stream_and_hash must never call read() unbounded")
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk


def test_stream_and_hash_computes_correct_sha256_via_bounded_reads(_isolated_client):
    mock_client = _isolated_client
    payload = b"x" * (3 * spaces._CHUNK_SIZE + 17)  # spans several chunk-sized reads
    body = _FakeStreamingBody(payload)
    mock_client.get_object.return_value = {"Body": body}

    digest = spaces.stream_and_hash("some-key")

    assert digest == hashlib.sha256(payload).hexdigest()
    mock_client.get_object.assert_called_once_with(Bucket=FAKE_BUCKET, Key="some-key")
    # Every read() call is bounded by _CHUNK_SIZE -- never one big read of the
    # whole object (the whole point of chunked hashing).
    assert body.read_calls
    assert all(size == spaces._CHUNK_SIZE for size in body.read_calls)
    assert len(body.read_calls) > 1


def test_stream_and_hash_raises_and_stops_reading_past_max_bytes(_isolated_client):
    mock_client = _isolated_client
    max_bytes = spaces._CHUNK_SIZE * 2
    # Object is far larger than max_bytes -- if stream_and_hash read it to
    # completion before checking the ceiling, read_calls would number in the
    # hundreds instead of a handful.
    payload = b"y" * (spaces._CHUNK_SIZE * 500)
    body = _FakeStreamingBody(payload)
    mock_client.get_object.return_value = {"Body": body}

    with pytest.raises(spaces.ObjectTooLargeError):
        spaces.stream_and_hash("big-key", max_bytes=max_bytes)

    # Bailed out shortly after crossing the ceiling, not after reading the
    # whole (much larger) object.
    assert len(body.read_calls) <= 4
