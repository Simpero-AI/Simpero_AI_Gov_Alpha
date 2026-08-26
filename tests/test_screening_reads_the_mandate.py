"""SIM-414 acceptance: the firm's saved mandate actually decides gs_07/gs_08.

The bug this pins closed: PUT /api/mandate wrote the `mandates` table while
screening read `investment_profiles.mandate`, a table with no writer, so
"we only look at deals from Canada" produced gs_07 = unknown on every deal
forever.

End to end, deliberately: the real HTTP handler saves the mandate (real
get_db, real JIT user provisioning, real commit), then the real screening job
runs the real decision engine, and the assertions read the persisted
`screening_result` row back through the owner connection. Nothing between the
two ends is mocked -- a test that stopped at load_workspace_config would not
have caught the original bug, because load_workspace_config was working fine
against the wrong table.

Combines the two existing harnesses: TestClient + get_claims override from
tests/test_phase1_endpoints.py, and owner_conn + direct job invocation from
tests/test_start_deal_screening_job.py.
"""

import asyncio
import importlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.dependencies import get_claims
from app.main import app

job_module = importlib.import_module("app.jobs.tasks.start_deal_screening")


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
    app.dependency_overrides[get_claims] = lambda: {
        "tenant_id": tenant_id,
        "user_id": user_id,
        "org_role": "admin",
        "raw_claims": {},
    }


@pytest.fixture
def seeded_org(owner_conn) -> Iterator[dict[str, Any]]:
    clerk_org_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organisation (clerk_org_id, name, created_at) VALUES (%s, %s, now()) "
            "RETURNING id",
            (clerk_org_id, "Mandate Screening Org"),
        )
        org_pk = cur.fetchone()[0]

    yield {"clerk_org_id": clerk_org_id, "org_pk": org_pk}

    with owner_conn.cursor() as cur:
        # `mandates` before `users` (NOT NULL FK, no ON DELETE).
        for table in (
            "screening_result",
            "mandates",
            "human_audit_log",
            "sessions",
            "claims",
            "analysis_run",
            "deals",
            "users",
        ):
            cur.execute(f"DELETE FROM {table} WHERE org_id = %s", (org_pk,))
        cur.execute("DELETE FROM organisation WHERE id = %s", (org_pk,))


@pytest.fixture
def taxonomy(owner_conn) -> Iterator[dict[str, Any]]:
    """Geographies > Canada > {British Columbia, Ontario}, and Target Sectors
    > SaaS. mandate_categories/mandate_options are GLOBAL (no org_id, no RLS)
    and category is unique, so these rows are uuid-suffixed and torn down
    explicitly -- a db_session rollback would not clean them up."""
    created: dict[str, Any] = {}
    with owner_conn.cursor() as cur:

        def add_category(name: str, slug: str) -> str:
            cur.execute(
                "INSERT INTO mandate_categories (category, slug) VALUES (%s, %s) RETURNING id",
                (f"{name} {uuid.uuid4().hex[:8]}", slug),
            )
            return str(cur.fetchone()[0])

        def add_option(category_id: str, option: str, parent: str | None = None) -> str:
            cur.execute(
                "INSERT INTO mandate_options (category_id, option, parent_option_id) "
                "VALUES (%s, %s, %s) RETURNING id",
                (category_id, option, parent),
            )
            return str(cur.fetchone()[0])

        geo = add_category("Geographies", "geographies")
        sectors = add_category("Target Sectors", "target_sectors")
        canada = add_option(geo, "Canada")
        created = {
            "geographies_id": geo,
            "sectors_id": sectors,
            "canada": canada,
            "british_columbia": add_option(geo, "British Columbia", canada),
            "ontario": add_option(geo, "Ontario", canada),
            "france": add_option(geo, "France"),
            "saas": add_option(sectors, "SaaS"),
        }

    yield created

    with owner_conn.cursor() as cur:
        # Options cascade from the category; drop only the categories this
        # fixture created, never any that pre-existed in the database.
        cur.execute(
            "DELETE FROM mandate_categories WHERE id IN (%s, %s)",
            (created["geographies_id"], created["sectors_id"]),
        )


def _seed_deal(owner_conn, org_pk: int, **fields) -> str:
    columns = ["org_id", "name", *fields]
    values = [org_pk, "Mandate Screening Deal", *fields.values()]
    placeholders = ", ".join(["%s"] * len(columns))
    with owner_conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO deals ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id",
            values,
        )
        return str(cur.fetchone()[0])


def _seed_screening_run(owner_conn, org_pk: int, deal_id: str) -> str:
    with owner_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO analysis_run (org_id, deal_id, job_name, status) "
            "VALUES (%s, %s, 'screening', 'queued') RETURNING id",
            (org_pk, deal_id),
        )
        return str(cur.fetchone()[0])


def _rule_results(owner_conn, org_pk: int) -> dict[str, dict[str, Any]]:
    with owner_conn.cursor() as cur:
        cur.execute(
            "SELECT rule_results FROM screening_result WHERE org_id = %s ORDER BY created_at DESC",
            (org_pk,),
        )
        row = cur.fetchone()
    assert row is not None, "the screening job wrote no screening_result row"
    return {result["rule_id"]: result for result in row[0]}


def _screen(owner_conn, seeded_org, **deal_fields) -> dict[str, dict[str, Any]]:
    """Run the real screening job over a deal and return its rule results.

    `sector` must never be one of db_04's prohibited sectors (cannabis,
    gambling, crypto_native, defense_manufacturing): deal-breakers evaluate
    first and short-circuit the run, so gs_07/gs_08 would not appear in
    rule_results at all and these tests would pass vacuously.
    """
    deal_id = _seed_deal(owner_conn, seeded_org["org_pk"], **deal_fields)
    run_id = _seed_screening_run(owner_conn, seeded_org["org_pk"], deal_id)
    # Sync test + asyncio.run: TestClient drives its own blocking portal, so
    # this module can't be an async test without nesting event loops.
    asyncio.run(
        job_module.start_deal_screening(
            {}, analysis_run_id=run_id, clerk_org_id=seeded_org["clerk_org_id"]
        )
    )
    return _rule_results(owner_conn, seeded_org["org_pk"])


def _save_mandate(client, seeded_org, taxonomy) -> None:
    """Canada (no sub_options => all of it) and SaaS, saved through the real
    endpoint in the exact shape the Builder sends."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.put(
        "/mandate",
        json={
            "mandate": [
                {
                    "category_id": taxonomy["geographies_id"],
                    "category": "Geographies",
                    "options": [{"option": "Canada", "option_id": taxonomy["canada"]}],
                },
                {
                    "category_id": taxonomy["sectors_id"],
                    "category": "Target Sectors",
                    "options": [{"option": "SaaS", "option_id": taxonomy["saas"]}],
                },
                {"category": "Check Size Range", "min": 5000000, "max": 10000000},
            ]
        },
    )
    assert resp.status_code == 200, resp.text


def test_deal_inside_the_approved_geography_and_sector_passes(
    client, owner_conn, seeded_org, taxonomy
):
    """The headline case: a firm that saved "Canada" screens a Canadian deal
    as gs_07 = Y instead of unknown."""
    _save_mandate(client, seeded_org, taxonomy)

    results = _screen(owner_conn, seeded_org, hq_geography="Canada", sector="SaaS")

    assert results["gs_07"]["verdict"] == "Y"
    assert results["gs_08"]["verdict"] == "Y"
    assert results["gs_07"]["evidence_ref"] == {
        "kind": "deal_field",
        "field": "hq_geography",
        "value": "Canada",
    }
    assert results["gs_07"]["confidence"] == 1.0


def test_deal_outside_the_approved_set_fails(client, owner_conn, seeded_org, taxonomy):
    _save_mandate(client, seeded_org, taxonomy)

    results = _screen(owner_conn, seeded_org, hq_geography="France", sector="Biotech")

    assert results["gs_07"]["verdict"] == "N"
    assert results["gs_08"]["verdict"] == "N"


def test_province_passes_under_a_country_level_mandate(client, owner_conn, seeded_org, taxonomy):
    """Approving "Canada" with no sub-options approves what the taxonomy says
    is inside Canada -- a deal recorded at province level still passes."""
    _save_mandate(client, seeded_org, taxonomy)

    results = _screen(owner_conn, seeded_org, hq_geography="Ontario", sector="SaaS")

    assert results["gs_07"]["verdict"] == "Y"


def test_case_difference_is_not_a_policy_rejection(client, owner_conn, seeded_org, taxonomy):
    """The deal form and the taxonomy are separate free-text vocabularies; a
    capitalisation difference must not read as "not approved"."""
    _save_mandate(client, seeded_org, taxonomy)

    results = _screen(owner_conn, seeded_org, hq_geography="canada", sector="saas")

    assert results["gs_07"]["verdict"] == "Y"
    assert results["gs_08"]["verdict"] == "Y"


def test_unknown_only_when_the_mandate_is_genuinely_unset(owner_conn, seeded_org, taxonomy):
    """No PUT at all: with nothing selected, screening falls back to the full
    rulebook rather than screening nothing, so gs_07/gs_08 run and stay unknown
    (no policy to check against), and say why."""
    results = _screen(owner_conn, seeded_org, hq_geography="Canada", sector="SaaS")

    assert results["gs_07"]["verdict"] == "unknown"
    assert results["gs_08"]["verdict"] == "unknown"
    assert "mandate" in results["gs_07"]["reason"]
    assert results["gs_07"]["confidence"] == 0.0


def test_a_sector_only_mandate_does_not_screen_geography(client, owner_conn, seeded_org, taxonomy):
    """Per-category gating: saving sectors selects gs_08 but not gs_07 -- a
    geography policy the firm never wrote is simply not screened, rather than run
    and auto-failing (or marked unknown on) every deal."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.put(
        "/mandate",
        json={
            "mandate": [
                {
                    "category_id": taxonomy["sectors_id"],
                    "category": "Target Sectors",
                    "options": [{"option": "SaaS", "option_id": taxonomy["saas"]}],
                }
            ]
        },
    )
    assert resp.status_code == 200

    results = _screen(owner_conn, seeded_org, hq_geography="France", sector="SaaS")

    assert results["gs_08"]["verdict"] == "Y"
    assert "gs_07" not in results


def test_a_sub_option_mandate_excludes_its_siblings(client, owner_conn, seeded_org, taxonomy):
    """Canada > British Columbia approves BC, not Ontario."""
    _authed(seeded_org["clerk_org_id"], "user-1")
    resp = client.put(
        "/mandate",
        json={
            "mandate": [
                {
                    "category_id": taxonomy["geographies_id"],
                    "category": "Geographies",
                    "options": [
                        {
                            "option": "Canada",
                            "option_id": taxonomy["canada"],
                            "sub_options": [
                                {
                                    "option": "British Columbia",
                                    "option_id": taxonomy["british_columbia"],
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )
    assert resp.status_code == 200

    assert (
        _screen(owner_conn, seeded_org, hq_geography="British Columbia")["gs_07"]["verdict"] == "Y"
    )
    assert _screen(owner_conn, seeded_org, hq_geography="Ontario")["gs_07"]["verdict"] == "N"
