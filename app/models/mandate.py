import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.organisation import Organisation, Users

# Global reference/taxonomy tables (no org_id, no RLS) -- shared across every
# tenant. Read-only on the product portal; full CRUD on the admin portal,
# platform admins only (see app/api/admin/mandates.py).


class MandateCategory(Base):
    __tablename__ = "mandate_categories"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    category: Mapped[str] = mapped_column(String(150), nullable=False, unique=True, index=True)


class MandateOptions(Base):
    __tablename__ = "mandate_options"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    category_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(MandateCategory.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # NULL = top-level option. Non-NULL = sub-option of that row; depth is
    # unbounded by the schema (the Builder UI only renders one level).
    # Sub-options carry their parent's category_id -- denormalized on purpose
    # so the category cascade and every category-scoped query keep working
    # unchanged. Set server-side from the parent row, never from the request
    # body (see app/api/admin/mandates.py), so it cannot diverge.
    #
    # No ORM relationship() for this self-FK, deliberately: MandateOptionsRepo
    # .delete uses session.delete(obj) directly, and a relationship here would
    # make SQLAlchemy try to NULL out children's parent_option_id instead of
    # letting Postgres cascade the delete -- silently promoting sub-options to
    # top-level rows. The DB-level ondelete="CASCADE" below is what actually
    # deletes the subtree.
    parent_option_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mandate_options.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    option: Mapped[str] = mapped_column(String(255), nullable=False)

    # Two partial indexes, not one (category_id, parent_option_id, option)
    # index: NULLs are never equal to each other in a plain unique index,
    # which would silently drop the top-level uniqueness rule. Top-level rows
    # stay unique per category; child rows are unique per parent (so two
    # different parents can each have a child named e.g. "All").
    __table_args__ = (
        Index(
            "uq_mandate_options_category_option",
            "category_id",
            "option",
            unique=True,
            postgresql_where=text("parent_option_id IS NULL"),
        ),
        Index(
            "uq_mandate_options_parent_option",
            "parent_option_id",
            "option",
            unique=True,
            postgresql_where=text("parent_option_id IS NOT NULL"),
        ),
    )


class Mandate(Base):
    """The org's own investment mandate -- one row per org, set/updated from
    the product portal. `mandate` is a free-form JSONB list of the org's
    selected category/option picks; not FK-validated against
    mandate_categories/mandate_options row-by-row (same "contract-shaped
    JSONB" idiom as investment_profiles.mandate)."""

    __tablename__ = "mandates"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # One mandate per org. Integer FK + UNIQUE because organisation.id is a
    # serial Integer -- RLS joins through to organisation.clerk_org_id, same
    # idiom as investment_profiles/deals/funds/claims.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, unique=True, index=True
    )
    # Who last saved this org's mandate -- informational only, not part of
    # the org_isolation RLS scoping.
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey(Users.id), nullable=False, index=True)

    mandate: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
