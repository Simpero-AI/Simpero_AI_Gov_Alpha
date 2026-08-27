import httpx
import pytest

import app.core.rate_limit_middleware as rate_limit_middleware
from app.main import app

# Any path under /api/public/ is enough to exercise the middleware -- it
# intercepts every request under that prefix before routing, so a
# nonexistent route (plain 404 from FastAPI) is sufficient and avoids
# touching Postgres for a test that's only about the Valkey-backed counter.
_PROBE_PATH = "/api/public/rate-limit-probe"


@pytest.fixture(autouse=True)
async def _clear(clear_rate_limit_keys):
    yield


async def _get(xff: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(_PROBE_PATH, headers={"X-Forwarded-For": xff})


async def test_exceeding_limit_returns_429_with_retry_after(monkeypatch):
    monkeypatch.setattr(rate_limit_middleware, "IP_LIMIT", 2)
    monkeypatch.setattr(rate_limit_middleware, "IP_WINDOW_SECONDS", 2)

    for _ in range(2):
        resp = await _get("9.9.9.1")
        assert resp.status_code == 404  # route doesn't exist, but passes the limiter

    resp = await _get("9.9.9.1")
    assert resp.status_code == 429
    assert resp.json() == {"detail": "Too Many Requests"}
    assert resp.headers["retry-after"] == "2"


async def test_different_ips_tracked_independently(monkeypatch):
    monkeypatch.setattr(rate_limit_middleware, "IP_LIMIT", 1)
    monkeypatch.setattr(rate_limit_middleware, "IP_WINDOW_SECONDS", 2)

    resp_a1 = await _get("1.1.1.1")
    assert resp_a1.status_code == 404
    # Different IP -- not throttled by IP A's usage even though A is now at
    # its limit.
    resp_b1 = await _get("2.2.2.2")
    assert resp_b1.status_code == 404

    resp_a2 = await _get("1.1.1.1")
    assert resp_a2.status_code == 429
    resp_b2 = await _get("2.2.2.2")
    assert resp_b2.status_code == 429


async def test_last_xff_entry_is_trusted_not_first(monkeypatch):
    monkeypatch.setattr(rate_limit_middleware, "IP_LIMIT", 2)
    monkeypatch.setattr(rate_limit_middleware, "IP_WINDOW_SECONDS", 2)

    # Same LAST entry ("9.9.9.9"), different spoofed FIRST entry each time --
    # if the last entry is what's actually trusted, these all land in one
    # bucket and the 3rd request (limit=2) is throttled.
    resp1 = await _get("1.2.3.4, 9.9.9.9")
    assert resp1.status_code == 404
    resp2 = await _get("5.6.7.8, 9.9.9.9")
    assert resp2.status_code == 404
    resp3 = await _get("6.6.6.6, 9.9.9.9")
    assert resp3.status_code == 429

    # Inverse: same FIRST entry, different LAST entry -- must NOT share a
    # bucket, so both stay under the limit independently.
    resp4 = await _get("8.8.8.8, 1.1.1.1")
    assert resp4.status_code == 404
    resp5 = await _get("8.8.8.8, 1.1.1.1")
    assert resp5.status_code == 404
    resp6 = await _get("8.8.8.8, 2.2.2.2")
    assert resp6.status_code == 404  # different last entry -> fresh bucket, not throttled
