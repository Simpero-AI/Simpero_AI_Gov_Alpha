import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.analysis_run import AnalysisRun
from app.models.deal import Deal
from app.models.organisation import Organisation

# W3 of the consistency effort (see reglens-consistency-plan): the grounded
# field-synthesis for the Company + Summary narrative sections is computed ONCE
# per analysis run and frozen here, instead of re-running the LLM + retrieval on
# every page load. GET /deals/{id}/company-synthesis becomes a pure reader of the
# latest snapshot, so two loads of the same deal are byte-identical and a
# re-analysis replaces (never re-rolls at request time).
#
# Write-once, enforced at the database layer:
#   REVOKE UPDATE, DELETE ON synthesis_snapshot FROM dd_app;
# (see this table's migration). Do NOT add application-level guards -- same
# reasoning as screening_result / human_audit_log: they can be bypassed by
# another code path and give false assurance. Re-analysis INSERTs a new row;
# `latest_for_deal` reads the newest one (supersession). This keeps the history
# and provenance rather than mutating in place, and -- crucially -- avoids a
# destructive delete-then-insert, which the append-only corroboration_events
# table already showed is a trap (see reglens-reingest-idempotency).

# The deal-level outcome of the synthesis pass. Per-section detail
# (no_hits / ungrounded / model_no_answer / ...) stays in the worker logs; this
# is the single sentinel the reader distinguishes "has content" from "explicitly
# empty, and why". Kept verbatim-in-sync with the CHECK in this table's migration
# (house convention).
REASONS = ("ok", "no_api_key", "no_documents", "no_sections_grounded")


class SynthesisSnapshot(Base):
    """One field-synthesis pass over one deal, frozen at analysis time: the
    grounded narrative sections (Company Business Overview / Risks / Commercial /
    Related Parties / Plans, plus the deal-level Executive Summary that feeds the
    Summary tab) and a top-level reason recording why an empty snapshot is empty.

    `sections` stores the INTERNAL provenance shape (per point: text, the
    (document_id, page) citations, and the chunk_ids) -- NOT the resolved
    "file · p.N" strings -- so filename resolution stays a cheap read-time join
    and a document rename is reflected on the next GET for free.
    """

    __tablename__ = "synthesis_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer -- RLS
    # joins through to organisation.clerk_org_id, same idiom as
    # deals/claims/analysis_run/screening_result.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(Deal.id), nullable=False, index=True
    )
    # The run whose generation produced this snapshot. Nullable so a synthesis
    # triggered outside the job chain (a manual regenerate, a backfill) can still
    # be recorded rather than being silently unrepresentable -- mirrors
    # screening_result.analysis_run_id. Read-path keys on deal_id + latest, so
    # this is provenance only.
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(AnalysisRun.id), nullable=True, index=True
    )

    # [{key, title, points: [{text, citations: [{document_id, page}], chunk_ids}]}, ...]
    # JSONB rather than child tables: written once and read back whole, and
    # nothing queries across individual points -- same call as
    # screening_result.rule_results / analysis_run.parse_jobs. Empty list [] when
    # nothing grounded; the reason column says why.
    sections: Mapped[list] = mapped_column(JSONB, nullable=False)

    # CHECK-constrained to REASONS in the migration. "ok" means >=1 section
    # grounded; the rest record an explicitly-empty snapshot so a blank page is a
    # recorded fact ("checked, found nothing, here's why"), never indistinguishable
    # from a not-yet-computed one.
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    # No updated_at -- write-once rows have nothing to update.
    #
    # clock_timestamp(), NOT now(), deliberately breaking this codebase's usual
    # func.now() idiom -- same reason as screening_result: now() is the
    # TRANSACTION timestamp, identical for every row in one transaction, so two
    # snapshots written in one transaction would tie and `latest_for_deal` (what
    # the GET answers with) would pick arbitrarily. clock_timestamp() advances
    # within a transaction, so "latest" is always a real answer.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
