import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.organisation import Organisation

# SIM-366: the parser's within-page claim relationships (the E1 reducer's output),
# persisted instead of dropped.
#
# Only same_fact and contradicts are produced (by the reducer) or consumed (by
# verification) TODAY. derived_from and supersedes are RESERVED, schema-only: the
# CHECK accepts them, but nothing writes or reads one yet -- do not assume they are
# live just because the column allows them. They sit in the CHECK ahead of use only
# because adding a value later is an ALTER TABLE DROP/ADD CONSTRAINT on a live RLS
# table while listing all four costs one string. derived_from goes live with the
# consistency pass (3b); both land under the SIM-56 epic, not here.
_EDGE_TYPES = ("same_fact", "derived_from", "contradicts", "supersedes")

# SIM-369: which pass wrote this edge. extraction_reducer = the parser's E1
# reducer (via ingest_claims.py, within-page only); reconciliation = 3a
# cross-page/tier same-fact; consistency = 3b arithmetic (derived_from +
# contradicts on mismatch); human = a reviewer-authored edge. Every edge
# names its author so a downstream consumer can weight/filter by provenance.
_CREATED_BY = ("extraction_reducer", "reconciliation", "consistency", "human")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Edge(Base):
    """One within-page relationship between two claims (SIM-341's reducer output),
    resolved through claim_ref -> claim id at ingest. Advisory graph structure: the
    parser emits it, the backend stores it, verification/consistency consume it. The
    shape mirrors contracts/claims.schema.json $defs/edge (type/from/to/basis)."""

    __tablename__ = "edges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    # Tenant; RLS scopes on this, same as claims/chunks.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )
    # SIM-365/366: edges resolve through claim_ref -> the claim's surrogate id at
    # ingest. ON DELETE RESTRICT, deliberately NOT cascade: edge teardown on
    # re-ingest is explicit and ordered (delete edges -> delete/upsert claims ->
    # re-insert edges), and RESTRICT is what forbids deleting a claim while an edge
    # references it -- so the teardown can't be silently skipped or cascaded.
    from_claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    to_claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("claims.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # same_fact | derived_from | contradicts | supersedes (CHECK). String + CHECK,
    # same shape as claims.status -- see the module note on why all four now.
    type: Mapped[str] = mapped_column(Text, nullable=False)
    # Human-readable reason the reducer linked these two claims.
    basis: Mapped[str] = mapped_column(Text, nullable=False)
    # SIM-369: which pass authored this edge. NOT NULL -- every edge names its
    # writer, no exceptions (see _CREATED_BY above).
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    # SIM-369: the authoring run. For extraction_reducer this is the parser
    # run's session_id (see scripts/ingest_claims.py); for reconciliation/
    # consistency, the backend pass's own run id. Nullable for a human-authored
    # edge, which has no "run".
    run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SIM-369: {rule, operands} for derived_from, {value_delta} for contradicts,
    # etc. -- shape varies by type and by writer, deliberately untyped here (see
    # each writer for what it puts in). Attribute name is `metadata_`, not
    # `metadata`: SQLAlchemy's declarative Base reserves the bare `metadata`
    # attribute for the class's own MetaData registry, so the DB column name
    # (`metadata`, per the reference schema) has to be given explicitly.
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(f"type IN ({_sql_list(_EDGE_TYPES)})", name="ck_edges_type"),
        CheckConstraint(f"created_by IN ({_sql_list(_CREATED_BY)})", name="ck_edges_created_by"),
        # SIM-369: no self-edge -- a claim cannot same_fact/contradict/derive
        # itself. The parser already guards a self-contradicts; this makes the
        # invariant DB-enforced too, for backend-authored edges (reconciliation,
        # consistency) that don't go through the parser's guard at all.
        CheckConstraint("from_claim_id <> to_claim_id", name="ck_edges_no_self_edge"),
        # SIM-369: idempotent edge writes. A re-run of the same pass over the
        # same document must not duplicate an edge -- this is the mechanism
        # SIM-367's re-ingest teardown pairs with (delete edges -> re-derive ->
        # re-insert; the UNIQUE makes a redundant re-insert a no-op conflict
        # rather than a duplicate row, for any writer that upserts on it).
        UniqueConstraint(
            "org_id", "from_claim_id", "to_claim_id", "type", name="uq_edges_org_from_to_type"
        ),
    )
