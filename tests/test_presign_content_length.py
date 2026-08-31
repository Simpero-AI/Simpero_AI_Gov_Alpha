"""P3-15 (F9): presign_put's optional content_length ceiling actually binds
into the SigV4 signature -- verified against real botocore signing math, not
by mocking presign_put/boto3 away (a stubbed signer would prove nothing
about the hole this closes: a client declaring a low size, getting a URL
signed for it, then PUTting more bytes).

Real boto3 S3 client with throwaway credentials -- generate_presigned_url
never makes a network call, it's pure local HMAC computation, so this is as
fast as a mocked test. botocore.auth.get_current_datetime is monkeypatched to
a fixed clock so two presign_put calls a few milliseconds apart still land in
the same SigV4 timestamp and are directly comparable.
"""

from __future__ import annotations

import datetime
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import botocore.auth as botocore_auth
import pytest

from app.services.uploads import spaces

FAKE_BUCKET = "test-bucket"
_FIXED_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _real_client_fixed_clock(monkeypatch: pytest.MonkeyPatch):
    fake_settings = SimpleNamespace(
        spaces_bucket=FAKE_BUCKET,
        spaces_region="tor1",
        spaces_endpoint_url="https://tor1.digitaloceanspaces.com",
        spaces_access_key_id="AKIAFAKEACCESSKEY",
        spaces_secret_access_key="fakeSecretKey1234567890",
    )
    monkeypatch.setattr(spaces, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(botocore_auth, "get_current_datetime", lambda: _FIXED_NOW)
    spaces._client.cache_clear()
    yield
    spaces._client.cache_clear()


def _signature(url: str) -> str:
    return parse_qs(urlsplit(url).query)["X-Amz-Signature"][0]


def _signed_headers(url: str) -> list[str]:
    return parse_qs(urlsplit(url).query)["X-Amz-SignedHeaders"][0].split(";")


def test_content_length_is_bound_into_signed_headers():
    url = spaces.presign_put("some/key.pdf", ttl_seconds=600, content_length=1024)
    assert "content-length" in _signed_headers(url)


def test_omitted_content_length_is_not_signed():
    url = spaces.presign_put("some/key.pdf", ttl_seconds=600)
    assert "content-length" not in _signed_headers(url)


def test_same_content_length_signs_identically():
    """Control: same key/ttl/content_length under the same clock must sign
    the same way every time -- otherwise the next test's inequality would be
    meaningless noise rather than proof the size is what changed the result.
    """
    url_1 = spaces.presign_put("some/key.pdf", ttl_seconds=600, content_length=1024)
    url_2 = spaces.presign_put("some/key.pdf", ttl_seconds=600, content_length=1024)
    assert _signature(url_1) == _signature(url_2)


def test_wrong_content_length_fails_signature_check():
    """The security property this ticket closes: a URL signed for N bytes is
    a different signature than one signed for N+1. An S3-compatible server
    recomputes the signature from the actual PUT it receives (using
    X-Amz-SignedHeaders to know Content-Length counts) and rejects on
    mismatch -- so a client that declares N, gets a URL, and then tries to
    upload N+1 bytes gets a signature failure, not a silently-accepted
    oversized object.
    """
    url_n = spaces.presign_put("some/key.pdf", ttl_seconds=600, content_length=1024)
    url_n_plus_1 = spaces.presign_put("some/key.pdf", ttl_seconds=600, content_length=1025)
    assert _signature(url_n) != _signature(url_n_plus_1)
