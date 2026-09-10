"""Refresh the web-collected Market/Company facts for ONE existing deal, without
re-parsing or re-running the whole analysis chain.

Web facts (kind="web", status="cited") are normally minted only inside the
corroboration job (verify -> corroboration -> screening). This script calls the
same two service functions directly -- gather_web_facts (the Anthropic web_search
pass) then persist_web_facts -- so an already-analyzed deal (e.g. the Apple 10-K)
picks up fresh, better market-definition / competitive-position web claims after
a prompt change, with no re-parse.

Safe to re-run: persist_web_facts is upsert-idempotent (ON CONFLICT DO NOTHING on
org_id+data_source_id+claim_ref), so an identical fact from the same URL never
duplicates. Caveat: web_search is non-deterministic, so a re-run typically finds
DIFFERENT URLs and mints net-new claims -- there is no teardown of stale web
claims here, so repeated runs accumulate. For a one-off refresh that is fine; a
clean slate needs a full re-analysis.

Needs ANTHROPIC_API_KEY in the environment (gather_web_facts is a no-op without
it, same as in production). Dry run by default -- pass --commit to persist.

    uv run python scripts/refresh_web_facts.py <deal_id> --org-key <clerk_org_id> [--commit]
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.repo.DealRepo import DealRepo
from app.services.web_search_collect import gather_web_facts, persist_web_facts


class _Rollback(Exception):
    """Abandon the transaction on a dry run without an error exit."""


async def _scope(session, org_key: str) -> None:
    # Same RLS scoping as scripts/run_verification.py: drop to dd_app so RLS
    # applies, then scope the session to this tenant. SET LOCAL is per-transaction,
    # so this must be re-issued inside each `session.begin()` block.
    await session.execute(text("SET LOCAL ROLE dd_app"))
    await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})


async def _run(deal_id: uuid.UUID, org_key: str, commit: bool) -> None:
    settings = get_settings()
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set -- gather_web_facts is a no-op. Nothing to do.")
        return

    async with AsyncSessionLocal() as session:
        # Phase A (read): load the deal's identity under RLS. Capture plain values
        # (not the ORM row) so nothing is accessed after the txn closes.
        async with session.begin():
            await _scope(session, org_key)
            deal = await DealRepo(session).get_by_id(deal_id)
            if deal is None:
                print(
                    f"deal {deal_id} not found for org {org_key!r} "
                    "(wrong org-key, or RLS-scoped out)."
                )
                return
            company = deal.name
            sector = deal.sector
            org_id = deal.org_id
        print(f"deal {company!r} (sector={sector!r}): gathering web facts…")

        # Phase B (NO transaction): the Anthropic web_search HTTP call.
        candidates = await gather_web_facts(
            company=company,
            sector=sector,
            api_key=settings.anthropic_api_key,
            model=settings.web_search_model,
        )
        print(f"{len(candidates)} web-fact candidate(s) gathered (allowlist-passed).")
        for c in candidates:
            raw = str(c.value.get("raw", ""))[:120]
            print(f"  [{c.assertion_class}] {c.entity}: {raw}  ({c.source_url})")
        if not candidates:
            print(
                "\nNo candidates. Either the model found nothing citable, or the allowlist "
                "dropped/400'd the search (check the web-collect WARNING in the logs)."
            )
            return

        # Phase C (write): mint under RLS. org_id is the INTEGER organisation id
        # (deal.org_id), distinct from the clerk_org_id string used for the RLS SET.
        async with session.begin():
            await _scope(session, org_key)
            minted = await persist_web_facts(
                session, deal_id=deal_id, org_id=org_id, candidates=candidates
            )
            if not commit:
                raise _Rollback()
            print(f"--commit: minted {minted} new web claim(s).")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deal_id", type=uuid.UUID)
    parser.add_argument("--org-key", required=True, help="clerk_org_id of the deal's tenant.")
    parser.add_argument("--commit", action="store_true", help="Persist. Default is a dry run.")
    args = parser.parse_args(argv)
    try:
        asyncio.run(_run(args.deal_id, args.org_key, args.commit))
    except _Rollback:
        print("dry run: rolled back, nothing persisted. Pass --commit to mint the web claims.")


if __name__ == "__main__":
    main()
