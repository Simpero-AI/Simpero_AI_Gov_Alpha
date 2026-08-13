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

# Screening #4 (SIM-404). Write-once, enforced at the database layer:
#   REVOKE UPDATE, DELETE ON screening_result FROM dd_app;
# (see this table's migration). Do NOT add application-level guards -- same
# reasoning as human_audit_log.py: they can be bypassed by another code path
# and give false assurance.
#
# Write-once matters more here than for an ordinary result cache. This row is
# the record of WHY a deal was auto-declined, stamped with the rulebook
# version that decided it. If it were mutable, "what did the screener
# actually say at LOI, under which rules" would be unanswerable after any
# later re-run -- which is the question the whole provenance chain exists to
# answer. Re-screening a deal INSERTS a new row; it never updates this one.

RECOMMENDATIONS = ("auto_decline", "green", "human_review")


class ScreeningResult(Base):
    """One screening pass over one deal: the recommendation, the rulebook
    version that produced it, and the per-rule verdicts with their evidence.

    A recommendation, deliberately not a decision -- `human_review` is a real
    and common outcome, and even `auto_decline` is a cited recommendation a
    human acts on.
    """

    __tablename__ = "screening_result"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer -- RLS
    # joins through to organisation.clerk_org_id, same idiom as
    # deals/claims/analysis_run.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(Deal.id), nullable=False, index=True
    )
    # The screening run that produced this. Nullable so a screening triggered
    # outside the job chain (a manual re-screen, a backfill) can still be
    # recorded rather than being silently unrepresentable.
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(AnalysisRun.id), nullable=True, index=True
    )

    # e.g. "track_b.v1". Stamped from the loaded rulebook, never hardcoded --
    # this is what makes an old result re-readable after the rules change.
    rulebook_version: Mapped[str] = mapped_column(Text, nullable=False)
    # CHECK-constrained to RECOMMENDATIONS in the migration.
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # [{rule_id, verdict, evaluator, evidence_ref, confidence, reason}, ...]
    # JSONB rather than a child table: these rows are written once and read
    # back whole, and nothing queries across individual rule verdicts -- same
    # call as analysis_run.parse_jobs. On an auto_decline this is deliberately
    # a PARTIAL list ending at the breaker that fired, because the engine
    # short-circuits; that truncation is a fact about the run worth
    # preserving, not a gap to backfill.
    rule_results: Mapped[list] = mapped_column(JSONB, nullable=False)

    # No updated_at -- write-once rows have nothing to update.
    #
    # clock_timestamp(), NOT now(), deliberately breaking this codebase's
    # usual func.now() idiom. now() is the TRANSACTION timestamp: it is
    # identical for every row written in one transaction, so two screenings
    # in the same transaction tie and `latest_for_deal` -- which is what
    # GET /deals/{id}/screening answers with -- picks arbitrarily between
    # them. Everywhere else in the schema created_at is informational and a
    # tie is harmless; here the ordering is load-bearing. clock_timestamp()
    # advances within a transaction, so "latest" is always a real answer.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )
