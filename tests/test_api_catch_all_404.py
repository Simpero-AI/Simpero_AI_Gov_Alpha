"""The 404-is-always-JSON guarantee in app/main.py.

Any /api/* path no router claims must return a JSON 404, never fall through to
the SPA's index.html at the ingress. This is what made the retired
/api/trpc/investmentProfile.upsert call surface as an "Unexpected token '<'"
HTML-parse error in the frontend (Shane QA category 3) rather than a clean
JSON failure.

It is a 404 exception handler, not a greedy /api/{path:path} route, so it fires
only on a genuine 404 after routing — the regression tests below pin that real
routes keep their 405 (wrong method) and their trailing-slash 307 redirects,
which a catch-all route would have swallowed into 404s.

Pure routing assertions — no auth, DB, or Valkey needed (the rate limiter only
touches Valkey for /api/public/*).
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


UNKNOWN_API_PATHS = [
    "/api/trpc/investmentProfile.upsert",
    "/api/does-not-exist",
    "/api/investment-profile/nope/deeper",
]


@pytest.mark.parametrize("path", UNKNOWN_API_PATHS)
def test_unknown_api_path_returns_json_404(client, path):
    resp = client.get(path)

    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/json")
    # The whole point: parseable JSON, not the SPA's HTML (which begins "<").
    assert not resp.text.lstrip().startswith("<")
    assert resp.json() == {"detail": "Not Found"}


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete"])
def test_unknown_path_is_json_404_for_every_method(client, method):
    """A POST/PUT to a dead route (how the tRPC client called it) 404s as JSON
    just like a GET does — no route matches any method, so it's a real 404."""
    resp = getattr(client, method)("/api/trpc/investmentProfile.upsert")

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Not Found"}


def test_does_not_shadow_a_real_route(client):
    """A real router still wins — GET /api/health is unauthenticated and must
    return its own 200, not a 404 (the handler fires only on a real 404)."""
    resp = client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_wrong_method_on_a_real_route_stays_405(client):
    """Regression guard: a wrong method on an existing route is a 405 with an
    Allow header, NOT a 404 — a greedy /api/{path:path} route would have turned
    this into a 404 and dropped Allow."""
    resp = client.post("/api/health")

    assert resp.status_code == 405
    assert "GET" in resp.headers.get("allow", "")


def test_trailing_slash_on_a_real_route_still_redirects(client):
    """Regression guard: a trailing slash on an existing route still 307s to the
    canonical path — a greedy catch-all would have 404'd it instead."""
    resp = client.get("/api/health/", follow_redirects=False)

    assert resp.status_code == 307
    assert resp.headers["location"].endswith("/api/health")
