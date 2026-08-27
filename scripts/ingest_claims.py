"""Demo: ingest claims JSON (from the parse service) into the claims spine.

The right half of the C3 seam -- reads the JSON scripts/emit_claims.py produced
in the parse repo, validates each claim against contracts/claims.schema.json,
and INSERTs into the claims table through the app's real model + session, as the
app role, under a tenant context. This is the embryo of SIM-63's backend ingest.

SAFETY, by construction:
- Dry run by DEFAULT. Everything happens inside one transaction that is ROLLED
  BACK unless --commit is passed. A plain run leaves the database exactly as it
  found it.
- Inserts run as dd_app (via SET LOCAL ROLE), not the owner, so RLS is actually
  exercised -- the same path the app takes. doadmin would bypass RLS and prove
  nothing.
- --org-key names a demo tenant; the demo organisation row is created in the
  same transaction, so it too vanishes on rollback.
- NOT idempotent -- insert-only. Re-running with --commit on a document that was
  already ingested INSERTS a second copy of its claims AND edges; it does not
  replace them. The ordered teardown that makes re-ingest safe (delete edges ->
  upsert claims -> re-insert edges) is SIM-367, and lands with the shared
  production-ingest core. Until then: ingest a document once, or clear its rows
  first. RESTRICT on the edge FKs is what will force that teardown's ordering.

    uv run python scripts/ingest_claims.py claims.json --org-key demo_e2e
    uv run python scripts/ingest_claims.py claims.json --org-key demo_e2e --commit

The nested seam location ({kind, page, char_start, ...}) is flattened into the
table's per-format columns here -- that translation IS the ingest's job.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from pathlib import Path

from sqlalchemy import func, select, text

from app.core.database import AsyncSessionLocal
from app.models import Claim, Deal, Edge, Organisation
from app.models.organisation import OrgType

logger = logging.getLogger(__name__)

_CONTRACT = Path(__file__).parent.parent / "contracts" / "claims.schema.json"

# location keys that map to their own flat column; everything else in a location
# (kind is handled explicitly; file/document_* are debug-only and not stored).
_LOCATION_COLUMNS = ("page", "char_start", "char_end", "bbox", "sheet", "cell_ref", "paragraph")


def _row_from_claim(claim: dict, org_id: int, deal_id: uuid.UUID, session_id: uuid.UUID) -> Claim:
    """One seam-JSON claim -> one Claim ORM row, flattening the location."""
    location = claim["location"]
    row = Claim(
        org_id=org_id,
        deal_id=deal_id,
        # Which run produced this claim. Tagging every row makes two extraction
        # runs over the same document independently queryable in one table --
        # which is what a golden-set diff needs: compare THIS run against THAT
        # one, rather than against whatever happens to be in the table.
        session_id=session_id,
        entity=claim["entity"],
        attribute=claim["attribute"],
        attribute_raw=claim.get("attribute_raw"),
        # SIM-365/364: carry the parser's stable id and assertion type across the seam.
        # claim_type is contract-required; default to "unknown" defensively for any
        # pre-field payload rather than failing the row.
        claim_ref=claim.get("claim_ref"),
        claim_type=claim.get("claim_type", "unknown"),
        value=claim["value"],
        # The parse service's emit.py cannot carry a period yet, but the
        # contract and this table both have the columns, so anything upstream
        # that did determine one is preserved rather than dropped on the floor.
        period_year=claim.get("period_year"),
        period_kind=claim.get("period_kind"),
        status=claim["status"],
        verification_method=claim.get("verification_method"),
        section=claim.get("section"),
        flags=claim.get("flags") or None,
        # Absent means quantitative; a qualitative claim carries both. Dropping
        # these would make a qualitative claim indistinguishable in the store
        # from a numeric one downgraded to text -- the exact split claim_kind
        # exists to preserve.
        claim_kind=claim.get("claim_kind"),
        assertion_class=claim.get("assertion_class"),
        kind=location["kind"],
    )
    for key in _LOCATION_COLUMNS:
        if key in location:
            setattr(row, key, location[key])
    return row


def _validate(claims: list[dict]) -> None:
    """Fail loudly before any write if the JSON does not match the contract.

    The seam's whole point: a shape the store cannot hold must be caught here,
    not discovered as a half-written row.
    """
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(json.loads(_CONTRACT.read_text()))
    for i, claim in enumerate(claims):
        errors = sorted(validator.iter_errors(claim), key=str)
        if errors:
            raise SystemExit(f"claim {i} violates the contract: {errors[0].message}")


async def _run(payload: dict, org_key: str, commit: bool, session_id: uuid.UUID) -> None:
    claims = payload["claims"]
    _validate(claims)
    print(f"{len(claims)} claims validated against the contract.")
    print(f"session_id for this run: {session_id}")

    async with AsyncSessionLocal() as session, session.begin():
        # Establish the app's identity and tenant FIRST, so the entire insert
        # path runs exactly as the app would: as dd_app (not the table owner, so
        # RLS applies), under a tenant context. This works whether the connection
        # is dd_app directly (the sandbox) or an admin role (SET ROLE then drops
        # to dd_app) -- and it means the organisation insert below is itself
        # subject to RLS's WITH CHECK, not slipped in as the owner.
        await session.execute(text("SET LOCAL ROLE dd_app"))
        # set_config(..., is_local=true) is the parameterizable form of
        # `SET LOCAL app.org_id = ...`. A bare `SET LOCAL x = :p` cannot bind
        # a parameter under asyncpg (SET is not a preparable statement) --
        # which is a latent bug in app/core/dependencies.py::get_db today.
        await session.execute(text("SELECT set_config('app.org_id', :k, true)"), {"k": org_key})

        # The demo tenant. Get-or-create: in reality the org already exists (it
        # comes from signup), and re-running the demo must not trip the unique
        # clerk_org_id. The SELECT runs under RLS, so it can only ever find the
        # current tenant's own row.
        org = await session.scalar(select(Organisation).where(Organisation.clerk_org_id == org_key))
        if org is None:
            # WITH CHECK passes because clerk_org_id equals app.org_id, set above.
            org = Organisation(
                clerk_org_id=org_key, name=f"E2E demo ({org_key})", type=OrgType.PE_FIRM
            )
            session.add(org)
            await session.flush()  # assigns org.id
        org_id = org.id

        # The demo deal. Get-or-create, same reuse rationale as the org above:
        # claims.deal_id is a required FK now, and this demo path has no real
        # deal to point at, so it needs one of its own to reuse across reruns.
        deal = await session.scalar(select(Deal).where(Deal.org_id == org_id))
        if deal is None:
            deal = Deal(org_id=org_id, name=f"E2E demo deal ({org_key})")
            session.add(deal)
            await session.flush()  # assigns deal.id
        deal_id = deal.id

        rows = [_row_from_claim(c, org_id, deal_id, session_id) for c in claims]
        session.add_all(rows)
        await session.flush()

        # SIM-366: persist the reducer's edges. Resolve each endpoint's claim_ref to
        # the claim just ingested; an unresolved from/to is a missing endpoint -- skip
        # that one edge and record it (fail-closed but non-blocking) rather than
        # failing the document or dropping it silently. (Idempotent delete-then-
        # reinsert teardown is SIM-367, part of the shared production-ingest core;
        # this demo path is insert-only, the same as it is for claims -- see the
        # module docstring.)
        ref_to_id = {r.claim_ref: r.id for r in rows if r.claim_ref is not None}
        # An edge is advisory, so a bad one is skipped and recorded, never fatal to
        # the document the way a bad claim is. Validate each edge against the
        # contract's edge schema FIRST: that is what catches a malformed edge (a
        # missing key) or a `type` outside the contract enum -- both of which
        # cross-repo contract drift can produce -- before it reaches a hard e[...]
        # read (KeyError) or a raw insert the CHECK rejects (IntegrityError), either
        # of which would roll back every claim already flushed above.
        from jsonschema import Draft202012Validator

        edge_validator = Draft202012Validator(json.loads(_CONTRACT.read_text())["$defs"]["edge"])
        edge_rows: list[Edge] = []
        skipped: list[str] = []
        for i, e in enumerate(payload.get("edges", [])):
            errors = sorted(edge_validator.iter_errors(e), key=str)
            if errors:
                skipped.append(f"edge {i} violates the contract: {errors[0].message}")
                continue
            # Required-and-typed by the contract now, so these reads cannot raise.
            from_id = ref_to_id.get(e["from"])
            to_id = ref_to_id.get(e["to"])
            if from_id is None or to_id is None:
                missing = ", ".join(
                    label for label, got in (("from", from_id), ("to", to_id)) if got is None
                )
                skipped.append(
                    f"{e['type']} {e['from']!r}->{e['to']!r} (missing endpoint: {missing})"
                )
                continue
            # SIM-369: CONTRADICTS is symmetric (neither side is "the" canonical
            # claim, see the contract's edge $defs), so two runs that emit the
            # same pair in opposite from/to order must still land as ONE row --
            # otherwise the UNIQUE(org_id, from, to, type) below cannot dedupe
            # them. Canonicalize to from < to by UUID so both orderings collapse
            # onto the same row. same_fact is directional (from=corroborator,
            # to=higher-precision source per the contract) and is NOT reordered.
            if e["type"] == "contradicts" and from_id > to_id:
                from_id, to_id = to_id, from_id
            edge_rows.append(
                Edge(
                    org_id=org_id,
                    from_claim_id=from_id,
                    to_claim_id=to_id,
                    type=e["type"],
                    basis=e["basis"],
                    # SIM-369: this ingest path is the parser's E1 reducer output
                    # only (within-page same_fact/contradicts) -- reconciliation
                    # and consistency write their own edges directly, elsewhere.
                    created_by="extraction_reducer",
                    run_id=str(session_id),
                    # The parser doesn't emit rule/operand/value_delta metadata
                    # for these edge types; nothing to carry here today.
                    metadata_=None,
                )
            )
        session.add_all(edge_rows)
        await session.flush()
        print(f"{len(edge_rows)} edge(s) inserted, {len(skipped)} skipped.")
        # A skipped edge is dropped on purpose, but the drop must not be console-only:
        # log it structured (keyed on the run's session_id + tenant) so it stays
        # queryable after the fact, not just visible in this one terminal. `detail`
        # already carries the edge's identity (type + from/to claim_ref) and reason.
        for detail in skipped:
            logger.warning("edge skipped: session=%s org=%s %s", session_id, org_key, detail)

        # Read back as dd_app under this tenant: the app's own view.
        seen = await session.scalar(select(func.count()).select_from(Claim))
        print(f"dd_app, tenant {org_key!r}: sees {seen} claims (inserted {len(claims)}).")

        # Prove isolation: a different tenant sees none of them.
        await session.execute(text("SELECT set_config('app.org_id', 'some_other_org', true)"))
        other = await session.scalar(select(func.count()).select_from(Claim))
        print(f"dd_app, a DIFFERENT tenant: sees {other} claims (RLS isolation).")

        if commit:
            print("--commit: persisting.")
        else:
            # Nothing above this line survives.
            raise _Rollback()


class _Rollback(Exception):
    """Sentinel to abandon the transaction on a dry run without an error exit."""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("claims_json", type=Path)
    parser.add_argument("--org-key", required=True, help="clerk_org_id for the demo tenant.")
    parser.add_argument("--commit", action="store_true", help="Persist. Default is a dry run.")
    parser.add_argument(
        "--session-id",
        help="Tag every claim with this run id (UUID). Defaults to a fresh one. "
        "Use it to compare two extraction runs over the same document.",
    )
    args = parser.parse_args(argv)

    payload = json.loads(args.claims_json.read_text())
    session_id = uuid.UUID(args.session_id) if args.session_id else uuid.uuid4()
    try:
        asyncio.run(_run(payload, args.org_key, args.commit, session_id))
    except _Rollback:
        print("dry run: transaction rolled back, database unchanged. Pass --commit to persist.")


if __name__ == "__main__":
    main()
