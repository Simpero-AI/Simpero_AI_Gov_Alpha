import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.deal import Deal
from app.models.organisation import Organisation

# DS-A-BGCHK-1 (SIM-262). Write-once, enforced at the database layer:
#   REVOKE UPDATE, DELETE ON entity_resolution FROM dd_app;
# (see this table's migration). Do NOT add application-level guards -- same
# reasoning as screening_result.py and human_audit_log.py: they can be bypassed
# by another code path and give false assurance.
#
# Write-once matters here because this row is the ANCHOR every downstream check
# inherits. SIM-408's harvest, SIM-253's reconcile, and SIM-254's roll-up all
# trace back to "which company did we decide this deal was". If that were
# mutable, an old memo's citations would silently re-point at a different
# filer. Re-resolving INSERTs a new row; the older rows are the record of how
# the answer changed.

SOURCES = ("sec_edgar",)

# See app/services/entity_resolution/types.py for what separates the last two:
# not_found is a real, expected answer for a private company; unresolved means
# nothing was checked at all.
STATUSES = ("resolved", "not_found", "unresolved")


class EntityResolution(Base):
    """One resolution attempt: which registry was asked, what it answered, and
    the anchor it produced.

    Deal-level, not claim-level. `corroboration_events` is the right home for
    "an outside source agreed/disagreed with THIS claim", but "this deal is
    CIK 0001326801" has no claim behind it -- its `claim_id` is NOT NULL, and
    widening that is SIM-409's decision, not this table's to pre-empt.
    """

    __tablename__ = "entity_resolution"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer -- RLS
    # joins through to organisation.clerk_org_id, same idiom as
    # deals/claims/screening_result.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(Deal.id), nullable=False, index=True
    )

    # Which registry answered. CHECK-constrained to SOURCES in the migration.
    source: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # CHECK-constrained to STATUSES in the migration.
    status: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # The name actually searched, at the time it was searched. Stored because
    # deals.name is mutable: without it a row can only say what the deal is
    # called NOW, not what produced this answer.
    query_name: Mapped[str] = mapped_column(Text, nullable=False)

    # The anchor -- EDGAR's 10-digit zero-padded CIK. NULL unless resolved,
    # enforced both ways by ck_entity_resolution_resolved_requires_registry_id:
    # a resolve with no anchor is indistinguishable from a guess, and an anchor
    # on a non-resolve would be an answer we explicitly declined to give.
    registry_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    legal_name: Mapped[str | None] = mapped_column(Text, nullable=True)

    # [{name, from, to}, ...] -- the filer's previous legal names with their
    # date windows. Load-bearing, not decoration: they are what lets a reader
    # tell "an old document uses the old name" from "this is a different
    # company", and a former name the documents never mention is a candidate
    # undisclosed finding for SIM-409.
    former_names: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # "current_name" | "former_name" | NULL when not resolved.
    matched_on: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Human-readable why, for not_found and unresolved. A reviewer seeing
    # "unresolved" needs to know whether it was ambiguity or a source conflict.
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Which endpoints were called, candidate counts, the matched name, tickers.
    # JSONB rather than columns: written once, read back whole, and nothing
    # queries across its keys -- same call as screening_result.rule_results.
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # No updated_at -- write-once rows have nothing to update.
    #
    # clock_timestamp(), NOT now(): now() is the TRANSACTION timestamp, so two
    # resolutions written in one transaction would tie and `latest_for_deal`
    # would pick between them arbitrarily. Same reasoning, at more length, in
    # screening_result.py.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
