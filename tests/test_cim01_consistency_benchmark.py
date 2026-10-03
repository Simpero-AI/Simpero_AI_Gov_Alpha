"""SIM-390: a scorer for the SIM-373 cim_01.yaml 3b benchmark fixture.

The fixture existed with no scorer -- hand-annotated relationships sitting in
a yaml file nothing ever ran. This seeds every `relationships` (real,
expected: holds) and `synthetic_conflicts` (injected, expected: conflicts)
row as claims, runs reconcile_consistency once, and checks the edge each row
promises actually got written: derived_from for a holding rule, contradicts
for a synthetic one. `not_engine_scoreable` rows (rule: null) are skipped, as
the fixture's own header instructs.

Real and synthetic rows share one seed/one pass but not one entity: a
synthetic row reuses a real row's period+attribute (e.g. 2018F gross_profit)
on purpose, to perturb a real answer -- so it is seeded under a second entity
name. Without that, the two claims would collide on reconcile_consistency's
(entity, period_year, period_kind, attribute) grouping key and both would be
skipped as ambiguous, scoring neither.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psycopg2
import pytest
import yaml
from sqlalchemy import select, text

from app.core.database import AsyncSessionLocal
from app.models import Claim, Edge, Organisation
from app.models.organisation import OrgType
from app.services.consistency import reconcile_consistency

ORG = "sim390-cim01-benchmark-org"
REAL_ENTITY = "cim-01"
SYNTHETIC_ENTITY = "cim-01-synthetic"
FIXTURE = Path(__file__).parent.parent / "benchmarks" / "consistency" / "cim_01.yaml"


def _owner_dsn() -> str:
    return (
        os.environ.get("ALEMBIC_DATABASE_URL", "").replace("+psycopg2", "").replace("+asyncpg", "")
    )


def _db_available() -> bool:
    if not _owner_dsn():
        return False
    try:
        conn = psycopg2.connect(_owner_dsn())
        cur = conn.cursor()
        cur.execute("SELECT to_regclass('public.edges')")
        row = cur.fetchone()
        conn.close()
        return row is not None and row[0] is not None
    except Exception:
        return False


requires_db = pytest.mark.skipif(
    not _db_available(), reason="no pgvector Postgres with the edges table (run ./sandbox/up.sh)"
)


def _delete_org(org_key: str) -> None:
    conn = psycopg2.connect(_owner_dsn())
    conn.autocommit = True
    cur = conn.cursor()
    for table in ("edges", "claims"):
        cur.execute(
            f"DELETE FROM {table} WHERE org_id IN "
            "(SELECT id FROM organisation WHERE clerk_org_id = %s)",
            (org_key,),
        )
    cur.execute("DELETE FROM organisation WHERE clerk_org_id = %s", (org_key,))
    conn.close()


def _claim_key(entity: str, period: dict[str, Any], attribute: str) -> str:
    return f"{entity}::{period['year']}::{period['kind']}::{attribute}"


def _claim_from(
    entity: str, period: dict[str, Any], operand: dict[str, Any], *, computational: bool
) -> Claim:
    return Claim(
        entity=entity,
        attribute=operand["attribute"],
        period_year=period["year"],
        period_kind=period["kind"],
        claim_type="computational" if computational else "numerical",
        value={
            "raw": str(operand["value"]),
            "normalized": operand["value"],
            "unit": None,
            "scale_multiplier": 1,
            "scale_source": "explicit_in_value",
            "value_type": operand["value_type"],
        },
        kind="pdf",
        page=1,
        char_start=0,
        char_end=1,
        status="proposed",
    )


def _collect_claims(rows: list[dict[str, Any]], entity: str, claims: dict[str, Claim]) -> None:
    """Add every operand and derived claim named by `rows`, deduped by
    (entity, period, attribute) -- the same claim reused across rows (e.g.
    revenue, checked by both the cogs-path and margin-path gross_profit
    rules) must be seeded once, or reconcile_consistency sees it as an
    ambiguous duplicate and skips it rather than scoring it."""
    for row in rows:
        period = row["period"]
        for operand in row["operands"]:
            key = _claim_key(entity, period, operand["attribute"])
            claims.setdefault(key, _claim_from(entity, period, operand, computational=False))
        key = _claim_key(entity, period, row["derived"]["attribute"])
        claims[key] = _claim_from(entity, period, row["derived"], computational=True)


@requires_db
async def test_cim01_benchmark_recall_and_precision() -> None:
    fixture = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    holds = [r for r in fixture["relationships"] if r["expected"] == "holds"]
    conflicts = fixture["synthetic_conflicts"]
    assert holds and conflicts, "fixture must carry at least one case of each kind to score"

    claims: dict[str, Claim] = {}
    _collect_claims(holds, REAL_ENTITY, claims)
    _collect_claims(conflicts, SYNTHETIC_ENTITY, claims)

    _delete_org(ORG)
    try:
        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE dd_app"))
            await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": ORG})
            org = Organisation(clerk_org_id=ORG, name="cim01 benchmark", type=OrgType.PE_FIRM)
            session.add(org)
            await session.flush()
            for c in claims.values():
                c.org_id = org.id
            session.add_all(claims.values())
            await session.flush()
            ids = {label: c.id for label, c in claims.items()}

        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(text("SET LOCAL ROLE dd_app"))
            await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": ORG})
            await reconcile_consistency(session, data_source_id=None, run_id="sim390-benchmark")

        async with AsyncSessionLocal() as session, session.begin():
            await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": ORG})
            edges = (await session.execute(select(Edge))).scalars().all()

        def has_edge(entity: str, row: dict[str, Any], edge_type: str) -> bool:
            derived_id = ids[_claim_key(entity, row["period"], row["derived"]["attribute"])]
            return all(
                any(
                    e.type == edge_type
                    and {e.from_claim_id, e.to_claim_id}
                    == {derived_id, ids[_claim_key(entity, row["period"], operand["attribute"])]}
                    for e in edges
                )
                for operand in row["operands"]
            )

        recalled = sum(1 for r in holds if has_edge(REAL_ENTITY, r, "derived_from"))
        recall = recalled / len(holds)
        assert recall == 1.0, f"expected every engine-scoreable holds row to derive; got {recall}"

        precise = sum(1 for r in conflicts if has_edge(SYNTHETIC_ENTITY, r, "contradicts"))
        precision = precise / len(conflicts)
        assert precision == 1.0, f"expected every synthetic conflict to flag; got {precision}"
    finally:
        _delete_org(ORG)
