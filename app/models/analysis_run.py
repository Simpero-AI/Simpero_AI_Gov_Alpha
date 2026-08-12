import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.deal import Deal
from app.models.organisation import Organisation

# queued -> in_progress -> successful|failed. Unlike data_source, this is a
# genuine multi-step walk, not a single pending->terminal edge, so there is
# no one-way trigger on this table -- see this table's migration docstring.
DEFAULT_ANALYSIS_RUN_STATUS = "queued"

# What kind of job this run represents: "parsing" | "extraction" |
# "verification" (CHECK constraint in this table's migration). Only
# "parsing" is actually built today (start_deal_analysis's fan-out to the
# parser service) -- extraction and verification are named here ahead of
# their own implementation, per Vansh, so this table can host those job
# types too without a schema change once they exist.
DEFAULT_ANALYSIS_RUN_JOB_NAME = "parsing"


class AnalysisRun(Base):
    """One run of a job (`job_name`) against a deal. Today that's always
    "Start Analysis" -> parsing: fans the deal's verified documents out to
    the parser service and tracks the outcome per document in `parse_jobs`
    (a JSONB array, not a child table -- a deal has a handful of documents
    and nothing queries across them individually).
    """

    __tablename__ = "analysis_run"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer -- RLS
    # joins through to organisation.clerk_org_id, same idiom as deals/data_source.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(Deal.id), nullable=False, index=True
    )

    # Identity, append-only -- never changes after the run is created.
    job_name: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=DEFAULT_ANALYSIS_RUN_JOB_NAME
    )

    # Persisted verbatim, not interpreted -- nothing reads this yet.
    selected_frameworks: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=DEFAULT_ANALYSIS_RUN_STATUS
    )
    # [{data_source_id, filename, storage_key, job_key, outcome, code,
    #   message, bucket, key}, ...] -- `message` is the parser service's own
    # rejection narrative, verbatim (see start_deal_analysis.py::_apply_outcome).
    parse_jobs: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Nullable, no server_default: stays NULL until the run reaches a
    # terminal status. A frontend-facing summary derived from `parse_jobs`
    # at that point -- one entry per document, camelCase keys (dataSourceId,
    # fileName, status, comment) since this, unlike parse_jobs' internal
    # bookkeeping shape, is meant to be read directly off GET .../status.
    job_comments: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Nullable, no server_default: stays NULL until the run reaches a
    # terminal status (successful/failed) -- set server-side by
    # AnalysisRunRepo.update_progress, once, at that transition.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
