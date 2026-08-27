"""Pipeline Inspector -- a standalone, self-contained diagnostic page.

Renders a deal's claims from the claims spine in a plain-language, interpretive
way for a non-technical reviewer: each fact's journey up the trust ladder
(extracted -> located in the source -> verified), where it came from in the
document, and how its cross-checks landed. Deliberately its own surface: a
single server-rendered HTML page with everything inline, sharing nothing with
the React app so it can't disturb it.

It is a READ-ONLY view -- one `SELECT` over claims + edges, no writes. Auth and
tenant scoping are the same as every other route: `get_db` requires a Clerk
bearer token and issues `SET LOCAL app.org_id`, so RLS scopes the query to the
caller's org (a raw browser tab has no token and 401s -- the React app fetches
this with its token and opens the returned HTML in a new window).
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependencies import get_db
from app.models.claim import Claim
from app.models.edge import Edge
from app.repo.DataSourceRepo import DataSourceRepo
from app.repo.DealRepo import DealRepo
from app.services.uploads.spaces import presign_get

router = APIRouter(prefix="/inspector", tags=["inspector"])

_TEMPLATE = (Path(__file__).parent / "templates" / "pipeline_inspector.html").read_text()
_PLACEHOLDER = "__PIPELINE_DATA__"

# A signed source-document URL lives about as long as a review session. The
# inspector page is a detached blob with no auth context, so it opens the source
# straight from this pre-signed URL rather than through an authed endpoint.
_SOURCE_URL_TTL_SECONDS = 3600


def _source_url(storage_key: str, filename: str) -> str | None:
    """A short-lived signed URL to open the document inline in the browser, or
    None if signing fails (the inspector then just shows the location as text)."""
    content_type = "application/pdf" if filename.lower().endswith(".pdf") else None
    try:
        return presign_get(storage_key, _SOURCE_URL_TTL_SECONDS, content_type=content_type)
    except Exception:  # noqa: BLE001 -- a missing source must not break the whole page
        return None


def _location(claim: Claim) -> dict[str, Any]:
    return {
        "kind": claim.kind,
        "page": claim.page,
        "char_start": claim.char_start,
        "char_end": claim.char_end,
        "sheet": claim.sheet,
        "cell_ref": claim.cell_ref,
        "paragraph": claim.paragraph,
    }


def _claim_json(claim: Claim, same_fact_count: int, contradicts: bool) -> dict[str, Any]:
    return {
        "id": str(claim.id),
        "entity": claim.entity,
        "attribute": claim.attribute,
        "attribute_raw": claim.attribute_raw,
        "value": claim.value,
        "period_year": claim.period_year,
        "period_kind": claim.period_kind,
        "status": claim.status,
        "verification_method": claim.verification_method,
        "section": claim.section,
        "claim_type": claim.claim_type,
        "claim_kind": claim.claim_kind,
        "assertion_class": claim.assertion_class,
        "flags": list(claim.flags or []),
        "data_source_id": str(claim.data_source_id) if claim.data_source_id else None,
        "location": _location(claim),
        "same_fact_count": same_fact_count,
        "contradicts": contradicts,
    }


def _render(data: dict[str, Any]) -> str:
    # Escape only the one sequence that could break out of the <script> data
    # island; JSON.parse on the client reads it back verbatim. Claim text is
    # untrusted document content, so it is never interpolated into HTML -- the
    # page renders every value via textContent.
    payload = json.dumps(data, default=str).replace("</", "<\\/")
    return _TEMPLATE.replace(_PLACEHOLDER, payload)


@router.get("/{deal_id}", response_class=HTMLResponse)
async def pipeline_inspector(
    deal_id: uuid.UUID, db: AsyncSession = Depends(get_db)
) -> HTMLResponse:
    """The self-contained inspector page for one deal (RLS-scoped by get_db)."""
    deal = await DealRepo(db).get_by_id(deal_id)
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deal not found")

    claims = list(
        (await db.execute(select(Claim).where(Claim.deal_id == deal_id).order_by(Claim.created_at)))
        .scalars()
        .all()
    )

    # same_fact corroboration count and contradiction flag per claim, from the
    # edges touching this deal's claims (edges are RLS-scoped to the org, so
    # restrict to these claim ids). Both edge directions count.
    same_fact: dict[uuid.UUID, int] = {}
    conflicting: set[uuid.UUID] = set()
    claim_ids = [c.id for c in claims]
    if claim_ids:
        edges = (
            (
                await db.execute(
                    select(Edge).where(
                        or_(Edge.from_claim_id.in_(claim_ids), Edge.to_claim_id.in_(claim_ids))
                    )
                )
            )
            .scalars()
            .all()
        )
        for edge in edges:
            for endpoint in (edge.from_claim_id, edge.to_claim_id):
                if edge.type == "same_fact":
                    same_fact[endpoint] = same_fact.get(endpoint, 0) + 1
                elif edge.type == "contradicts":
                    conflicting.add(endpoint)

    data_sources = await DataSourceRepo(db).list_for_deal(deal_id)
    documents = [
        {
            "data_source_id": str(ds.id),
            "filename": ds.filename,
            # A signed URL so a fact can open its source page (PDF #page=N) in a tab.
            "source_url": _source_url(ds.storage_key, ds.filename),
        }
        for ds in data_sources
    ]

    data = {
        "deal": {"id": str(deal.id), "name": deal.name},
        "documents": documents,
        # The parser's grounded organizing pass, if this deal has one. The page
        # folds entities into these subjects and leads with this metric order;
        # when it is null the page falls back to deterministic frequency grouping.
        "dashboard_structure": deal.dashboard_structure,
        "generated_at": datetime.now(UTC).isoformat(),
        "claims": [
            # A same_fact edge is counted once per endpoint above, so a claim
            # seen as both `from` and `to` on distinct edges tallies correctly;
            # halve nothing -- each edge is one corroboration link.
            _claim_json(c, same_fact.get(c.id, 0), c.id in conflicting)
            for c in claims
        ],
    }
    return HTMLResponse(_render(data))
