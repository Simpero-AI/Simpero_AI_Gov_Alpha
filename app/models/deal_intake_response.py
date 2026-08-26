import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.deal import Deal
from app.models.deal_intake_link import DealIntakeLink
from app.models.organisation import Organisation

# Append-only: no application-level immutability guard here, nor should
# there ever be one. Enforcement is a blanket REVOKE UPDATE, DELETE ON
# deal_intake_response FROM dd_app in this table's migration -- the
# human_audit_log idiom (a submitted external answer is a historical fact,
# not editable state).


class DealIntakeResponse(Base):
    """The external party's submitted intake answers (SIM P1-02). See this
    table's migration for the RLS/grant posture -- blanket-immutable, same
    idiom as human_audit_log, unlike deal_intake_link's narrow lifecycle-
    column grant-back.
    """

    __tablename__ = "deal_intake_response"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer -- RLS
    # joins through to organisation.clerk_org_id, same idiom as
    # deal_intake_link/data_source/human_audit_log.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )

    # Denormalized alongside link_id so the org-side read is one indexed
    # lookup, rather than a join through deal_intake_link.
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(Deal.id), nullable=False, index=True
    )
    link_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(DealIntakeLink.id), nullable=False, index=True
    )

    respondent_email: Mapped[str] = mapped_column(String(255), nullable=False)
    answers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
