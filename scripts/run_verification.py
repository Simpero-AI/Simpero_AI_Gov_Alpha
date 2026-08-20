"""Run the claim-to-claim verification passes over one tenant's already-ingested
claims and write their typed edges + flags: 3a reconciliation (same fact across
pages/tiers, SIM-371) then 3b consistency (formula reconstruction, SIM-372/376).

This is the post-ingest step that turns the pipeline from extraction -> claims
into extraction -> claims -> verification -> edges. The passes existed and were
unit-tested, but nothing ran them in sequence on real ingested claims until now;
the sandbox calls this right after scripts/ingest_claims.py.

  uv run python scripts/run_verification.py --org-key sandbox_demo \
      [--session-id <uuid>] [--commit]

The session is RLS-scoped as dd_app + app.org_id, exactly as the ingest -- the
passes REQUIRE an already-scoped session and do not scope themselves (see their
docstrings). data_source_id=None matches the demo ingest path (its claims carry
a NULL data_source_id). Both passes are idempotent (INSERT ... ON CONFLICT DO
NOTHING against SIM-369's UNIQUE), so a re-run over unchanged claims writes zero
new edges. Rolls back unless --commit, the same safety contract as the ingest.

SIM-389 -- run scoping: pass the SAME --session-id the ingest ran under to
verify only THAT run's claims. Without it this pass reconciles every claim in
the tenant's RLS scope, so a second run without a database wipe forms edges
ACROSS runs and inflates the counts (and, in 3b, silently suppresses rules --
a duplicated operand key reads as ambiguous and is skipped). The edges' run_id
is then the session id, so per-run attribution and cleanup actually work;
without --session-id it falls back to the old constants, which is honest about
the fact that such a pass is not attributable to one run.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import uuid

from sqlalchemy import func, select, text

from app.core.database import AsyncSessionLocal
from app.models import Claim
from app.services.consistency import reconcile_consistency
from app.services.reconciliation import reconcile_same_fact


class _Rollback(Exception):
    """Abandon the transaction on a dry run without an error exit."""


async def _run(org_key: str, commit: bool, session_id: uuid.UUID | None) -> None:
    async with AsyncSessionLocal() as session, session.begin():
        # Same RLS scoping as scripts/ingest_claims.py: drop to dd_app so RLS
        # applies, then scope the session to this tenant. SET is not preparable
        # under asyncpg, so app.org_id is set via set_config, not a bound SET.
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})

        # Count what the passes will actually see, not the whole tenant --
        # otherwise the printed number silently disagrees with the run being
        # verified, which is the confusion this ticket exists to remove.
        count_stmt = select(func.count()).select_from(Claim)
        if session_id is not None:
            count_stmt = count_stmt.where(Claim.session_id == session_id)
        n_claims = await session.scalar(count_stmt)
        scope = f"run {session_id}" if session_id is not None else "ALL runs"
        print(f"tenant {org_key!r}: {n_claims} claim(s) in scope ({scope}).")
        if not n_claims:
            print("  nothing to verify -- ingest first (scripts/ingest_claims.py).")
        if session_id is None:
            print(
                "  note: no --session-id, so this reconciles every run in scope. "
                "Edges may span runs and the counts below may be inflated."
            )

        # One run_id per run, shared by both passes: an edge's PASS is already
        # recorded in created_by (reconciliation vs consistency, see the
        # group-by below), so run_id is free to mean the run and only the run.
        recon_run_id = str(session_id) if session_id is not None else "sandbox-3a"
        cons_run_id = str(session_id) if session_id is not None else "sandbox-3b"

        recon = await reconcile_same_fact(
            session, data_source_id=None, session_id=session_id, run_id=recon_run_id
        )
        cons = await reconcile_consistency(
            session, data_source_id=None, session_id=session_id, run_id=cons_run_id
        )

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

        if commit:
            print("\n--commit: persisting edges + flags.")
        else:
            print("\ndry run -- rolling back (pass --commit to persist).")
            raise _Rollback()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org-key", required=True, help="clerk_org_id of the tenant to verify.")
    parser.add_argument(
        "--session-id",
        help=(
            "Verify only this ingest run's claims (the session_id "
            "scripts/ingest_claims.py ran under). Omit to reconcile every run "
            "in scope -- edges may then span runs."
        ),
    )
    parser.add_argument("--commit", action="store_true", help="Persist. Default is a dry run.")
    args = parser.parse_args(argv)
    session_id = uuid.UUID(args.session_id) if args.session_id else None
    with contextlib.suppress(_Rollback):
        asyncio.run(_run(args.org_key, args.commit, session_id))


if __name__ == "__main__":
    main()
