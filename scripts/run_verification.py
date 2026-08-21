"""Run the verification passes over one tenant's already-ingested claims: the
exact-span `proposed -> cited` promoter (SIM-412) first, then the two
claim-to-claim passes that write typed edges + flags -- 3a reconciliation (same
fact across pages/tiers, SIM-371) and 3b consistency (formula reconstruction,
SIM-372/376), and finally the status roll-up (SIM-254, wired here by
SIM-413).

This is the post-ingest step that turns the pipeline from extraction -> claims
into extraction -> claims -> verification -> edges. The passes existed and were
unit-tested, but nothing ran them in sequence on real ingested claims until now;
the sandbox calls this right after scripts/ingest_claims.py.

  uv run python scripts/run_verification.py --org-key sandbox_demo [--commit]

The session is RLS-scoped as dd_app + app.org_id, exactly as the ingest -- the
passes REQUIRE an already-scoped session and do not scope themselves (see their
docstrings). data_source_id=None matches the demo ingest path (its claims carry
a NULL data_source_id). Both passes are idempotent (INSERT ... ON CONFLICT DO
NOTHING against SIM-369's UNIQUE), so a re-run over unchanged claims writes zero
new edges; the promoter is idempotent too (it only selects `proposed` claims,
so a second run finds none). Rolls back unless --commit, the same safety
contract as the ingest.

The claim-status histogram printed at the end is the observable this whole
chain exists for: before SIM-412 it read 100% proposed/missing with zero
`cited`, which is what left screening and every external corroborator with
nothing to read.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models import Claim
from app.services.consistency import reconcile_consistency
from app.services.corroboration import CORROBORATABLE_STATUSES
from app.services.reconciliation import reconcile_same_fact
from app.services.span_promotion import promote_exact_span
from app.services.status_rollup import roll_up_deal


class _Rollback(Exception):
    """Abandon the transaction on a dry run without an error exit."""


async def _run(org_key: str, commit: bool) -> None:
    async with AsyncSessionLocal() as session, session.begin():
        # Same RLS scoping as scripts/ingest_claims.py: drop to dd_app so RLS
        # applies, then scope the session to this tenant. SET is not preparable
        # under asyncpg, so app.org_id is set via set_config, not a bound SET.
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})

        n_claims = await session.scalar(select(func.count()).select_from(Claim))
        print(f"tenant {org_key!r}: {n_claims} claim(s) in scope.")
        if not n_claims:
            print("  nothing to verify -- ingest first (scripts/ingest_claims.py).")

        print("\nclaim status before verification:")
        await _print_status_histogram(session)

        promoted = await promote_exact_span(session, data_source_id=None)
        recon = await reconcile_same_fact(session, data_source_id=None, run_id="sandbox-3a")
        cons = await reconcile_consistency(session, data_source_id=None, run_id="sandbox-3b")

        print("\nexact-span promotion (proposed -> cited):")
        print(f"      claims considered ........ {promoted.claims_considered}")
        print(f"      promoted to cited ........ {promoted.claims_promoted}")
        print(f"      held (binding_unsupported) {promoted.skipped_binding_unsupported}")

        print("\n3a reconciliation (same fact across pages/tiers):")
        print(f"      groups considered ........ {recon.groups_considered}")
        print(f"      same_fact edges .......... {recon.same_fact_edges}")
        print(f"      contradicts edges ........ {recon.contradicts_edges}")
        print(f"      claims flagged ........... {recon.claims_flagged}")

        print("\n3b consistency (formula reconstruction):")
        print(f"      rules evaluated .......... {cons.rules_evaluated}")
        print(f"      derived_from edges ....... {cons.derived_from_edges}")
        print(f"      contradicts edges ........ {cons.contradicts_edges}")
        print(f"      claims flagged ........... {cons.claims_flagged}")
        print(f"      skipped (missing operands) {cons.skipped_missing_operands}")

        # The app's own view of the edge store under this tenant's RLS, grouped
        # by who wrote each edge -- extraction_reducer (from ingest) vs the
        # reconciliation/consistency writers this pass just added.
        rows = (
            await session.execute(
                text(
                    "SELECT created_by, type, count(*) "
                    "FROM edges GROUP BY created_by, type ORDER BY created_by, type"
                )
            )
        ).all()
        print("\nedges in the store (this tenant, by writer / type):")
        if rows:
            for created_by, etype, n in rows:
                print(f"      {created_by:20} {etype:16} {n}")
        else:
            print("      (none)")

        # Last, and reading everything above: the roll-up folds internal
        # disagreement (formula_mismatch flags, contradicts edges) and any
        # external corroboration events into one trust status per claim.
        rolled = await _roll_up_all(session)
        print("\nstatus roll-up (cited -> verified / inconclusive / ...):")
        if rolled:
            for status, n in sorted(rolled.items()):
                print(f"      -> {status:20} {n}")
        else:
            print("      (nothing internally checked yet)")

        # _roll_up_all flushes on the way out; this is belt-and-braces for the
        # aggregate below, since AsyncSessionLocal sets autoflush=False and an
        # unflushed in-place `status` mutation would make the histogram print
        # the BEFORE numbers and read as "nothing happened".
        await session.flush()
        print("\nclaim status after verification:")
        await _print_status_histogram(session)

        if commit:
            print("\n--commit: persisting edges + flags + promoted/rolled-up statuses.")
        else:
            print("\ndry run -- rolling back (pass --commit to persist).")
            raise _Rollback()


async def _roll_up_all(session: AsyncSession) -> dict[str, int]:
    """Roll up every internally-checked claim in scope, returning the resulting
    status counts. The status filter is the guard: roll_up_deal only has a
    verdict for internally-checked claims, which is exactly what `proposed` and
    `missing` claims are not."""
    # autoflush=False, so the promoter's pending proposed -> cited mutations
    # would not be visible to this SELECT and the roll-up would find nothing.
    await session.flush()
    stmt = select(Claim).where(Claim.status.in_(sorted(CORROBORATABLE_STATUSES)))
    claims = list((await session.scalars(stmt)).all())
    await roll_up_deal(session, claims)
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim.status] = counts.get(claim.status, 0) + 1
    await session.flush()
    return counts


async def _print_status_histogram(session: AsyncSession) -> None:
    """The tenant's claims by status, under its own RLS scope."""
    rows = (
        await session.execute(
            select(Claim.status, func.count()).group_by(Claim.status).order_by(func.count().desc())
        )
    ).all()
    if not rows:
        print("      (no claims)")
        return
    for status, n in rows:
        print(f"      {status:20} {n}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-key", required=True, help="clerk_org_id of the tenant to verify.")
    parser.add_argument("--commit", action="store_true", help="Persist. Default is a dry run.")
    args = parser.parse_args(argv)
    with contextlib.suppress(_Rollback):
        asyncio.run(_run(args.org_key, args.commit))


if __name__ == "__main__":
    main()
