import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.deal import Deal
from app.models.deal_intake_link import DealIntakeLink
from app.models.organisation import Organisation

# Lifecycle states for a data_source row. One-way pending -> terminal,
# enforced by the trg_data_source_one_way_status trigger added in this
# table's migration -- not by application code (see that migration's
# docstring for why: triggers fire for every role, including the table
# owner, closing a gap column-level GRANT/REVOKE alone leaves open).
_STATUSES = ("pending", "verified", "quarantined", "ocr_needed", "mismatch")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class DataSource(Base):
    """One uploaded document, registered once its presigned PUT to Spaces is
    confirmed (SIM-216). Identity/history columns (everything but
    status/fingerprint/status_updated_at) are append-only: REVOKE UPDATE,
    DELETE on this table is granted back narrowly, only on the three
    lifecycle columns the async ingest job legitimately owns -- see this
    table's migration for the full column-grant + one-way-trigger pattern.
    """

    __tablename__ = "data_source"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer -- RLS
    # joins through to organisation.clerk_org_id, same idiom as deals/claims/chunks.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )
    # Unlike claims/chunks (authored before `deals` existed in the migration
    # chain), `deals` precedes this table today -- a real FK, not a bare UUID.
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(Deal.id), nullable=False, index=True
    )

    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)

    # The external URL a web-collected source came from (the web-search deep
    # search pass). Null for ordinary uploaded documents; set for the synthetic
    # per-source rows the collect pass creates, so a web claim's citation is a
    # real external link rather than a filename. Identity/append-only like the
    # other non-lifecycle columns.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Client-computed hash at presign time -- the pre-flight dedupe key.
    # Never treated as proof of the uploaded bytes.
    declared_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # The async ingest job's verified hash. NULL until ingest completes.
    fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending", index=True
    )
    # Nullable, no server_default: stays NULL until the ingest job moves
    # status off 'pending' -- a still-pending row has never had a status
    # *change*, only an initial value. Set alongside `status` in the same
    # UPDATE, server-side (see DataSourceRepo.update_status).
    status_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # NULL for org-side uploads (app/api/uploads.py). Set only on rows created
    # via the public intake flow (app/api/public_uploads.py), to the link that
    # authorized the upload (P3-10).
    intake_link_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(DealIntakeLink.id), nullable=True, index=True
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_list(_STATUSES)})", name="ck_data_source_status"),
    )
