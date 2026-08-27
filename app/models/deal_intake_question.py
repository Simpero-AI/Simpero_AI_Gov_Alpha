import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import Index
from sqlalchemy.types import DateTime

from app.core.database import Base

# Global reference table (no org_id, no RLS) -- shared across every tenant,
# same idiom as mandate_categories/mandate_options. Read-only on the product
# portal; full CRUD on the admin portal, platform admins only (see
# app/api/admin/intake_questions.py, P2-02).

# Currently supported input_type values -- CHECK-constrained (same idiom as
# DataSource._STATUSES) because a link's questions_snapshot freezes
# input_type at generation time: a bad value here doesn't just fail on
# read, it gets baked into every link issued while the row is active.
_INPUT_TYPES = ("text", "textarea")


class DealIntakeQuestion(Base):
    __tablename__ = "deal_intake_questions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Immutable identity a link's questions_snapshot references by -- never
    # reassigned once a link has snapshotted it. Uniqueness is scoped to
    # active rows (partial index below), not global: deactivating a question
    # frees its key for reuse without deleting the row a past snapshot still
    # points at (see P2-02's activate/deactivate, never a hard delete).
    question_key: Mapped[str] = mapped_column(String(100), nullable=False)

    prompt: Mapped[str] = mapped_column(String(500), nullable=False)
    help_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_type: Mapped[str] = mapped_column(String(50), nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

    # Flat ordered list (Q7) -- the whole active set applies, no categories.
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        Index(
            "uq_deal_intake_questions_active_key",
            "question_key",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(
            "input_type IN ('" + "', '".join(_INPUT_TYPES) + "')",
            name="ck_deal_intake_questions_input_type",
        ),
    )
