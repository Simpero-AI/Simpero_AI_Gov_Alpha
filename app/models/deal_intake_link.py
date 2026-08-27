import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.deal import Deal
from app.models.organisation import Organisation, Users

# Lifecycle states are one-way pending -> terminal, enforced by the
# trg_deal_intake_link_one_way_status trigger added in this table's
# migration -- not by application code (same idiom as DataSource's
# _STATUSES / trg_data_source_one_way_status).
_STATUSES = ("pending", "submitted", "revoked", "expired")


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class DealIntakeLink(Base):
    """The external deal-intake link (SIM P1-01). The raw token is never
    stored -- only its SHA-256 (`token_hash`); see this table's migration for
    the RLS/grant/trigger posture (data_source's column-grant + one-way
    trigger idiom).
    """

    __tablename__ = "deal_intake_link"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer -- RLS
    # joins through to organisation.clerk_org_id, same idiom as data_source/
    # deals/human_audit_log.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )
    # Denormalized copy of organisation.clerk_org_id, written once at insert.
    # Never used in org_isolation's own predicate -- exists only so the
    # public-session GUC handshake (P1-04) can read the tenant id off this
    # row without a second, un-guarded join into organisation (itself
    # RLS'd). Never updated after insert.
    clerk_org_id: Mapped[str] = mapped_column(String(64), nullable=False)

    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(Deal.id), nullable=False, index=True
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    recipient_email: Mapped[str] = mapped_column(String(255), nullable=False)
    questions_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending", index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_by_user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Users.id), nullable=False, index=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_list(_STATUSES)})", name="ck_deal_intake_link_status"),
    )
