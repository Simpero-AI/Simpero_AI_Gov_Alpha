"""Dry-run the grounded Company-tab field synthesis for one deal and print it.

Slice-1 validation surface for app/services/field_synthesis.py: it runs the real
retrieval + grounded-LLM + verification pass over a deal's OWN chunks and prints
the resulting per-section points WITH their page citations, so the answers can be
eyeballed for accuracy on a real deal (e.g. the Apple 10-K) BEFORE any of it is
wired into the page or persisted. Read-only: it writes nothing.

Runs as dd_app under the tenant, exactly as the app would, so RLS + the
org_scoped_search guard are exercised (same discipline as scripts/ingest_chunks).
Retrieval is sparse-only today (embeddings are NULL until a backfill lands), so
this needs no Voyage key; it does need ANTHROPIC_API_KEY for the synthesis LLM
(without it the pass fails soft and prints nothing, same as in production).

    uv run python scripts/synthesize_company.py <deal_id> --org-key <clerk_org_id>
    (ANTHROPIC_API_KEY must be set in the environment for the synthesis LLM.)
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import text

from app.core.database import AsyncSessionLocal
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.DealRepo import DealRepo
from app.services.field_synthesis import synthesize_company_sections


async def _run(deal_id: uuid.UUID, org_key: str) -> None:
    async with AsyncSessionLocal() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE dd_app"))
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})

        deal = await DealRepo(session).get_by_id(deal_id)
        if deal is None:
            print(
                f"deal {deal_id} not found for org {org_key!r} (wrong org-key, or RLS-scoped out)."
            )
            return

        data_sources = await DataSourceRepo(session).list_for_deal(deal_id)
        document_ids = [str(ds.id) for ds in data_sources]
        filenames = {str(ds.id): ds.filename for ds in data_sources}
        print(f"deal {deal.name!r}: {len(document_ids)} document(s).")
        if not document_ids:
            print("no documents -> nothing to retrieve.")
            return

        sections = await synthesize_company_sections(
            session, org_id=org_key, document_ids=document_ids, company=deal.name
        )

    if not sections:
        print(
            "\nNo grounded sections were produced. Either ANTHROPIC_API_KEY is unset, "
            "the deal has no ingested chunks, or nothing survived the grounding gate."
        )
        return

    for section in sections:
        print(f"\n=== {section.title} ({section.key}) ===")
        for point in section.points:
            cites = ", ".join(
                f"{filenames.get(c.document_id, c.document_id)}"
                + (f" p.{c.page}" if c.page is not None else "")
                for c in point.citations
            )
            print(f"  • {point.text}")
            print(f"      ↳ {cites}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("deal_id", type=uuid.UUID)
    parser.add_argument("--org-key", required=True, help="clerk_org_id of the deal's tenant.")
    args = parser.parse_args(argv)
    asyncio.run(_run(args.deal_id, args.org_key))


if __name__ == "__main__":
    main()
