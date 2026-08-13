import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, Numeric, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.organisation import Funds, Organisation

# Lifecycle states mirror the frontend's dealsLifecycle.STATE_ORDER
# (src/shared/dealsLifecycle.ts in Simpero_AI_Gov_Web): sourcing -> draft ->
# submitted -> (approved | declined). Kept as a plain string, not a DB enum,
# so the two repos' lifecycle contracts don't require a migration to stay
# in sync — the frontend module is the source of truth for the sequence.
DEFAULT_DEAL_STATUS = "sourcing"


class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer — RLS joins
    # through to organisation.clerk_org_id, same idiom as funds/claims/human_audit_log.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )
    # Nullable: the frontend's deals.create contract has no fund field yet.
    fund_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(Funds.id), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(Text, nullable=False)
    gp_source: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Integer cents, per repo convention (no floats for money).
    deal_size_min_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deal_size_max_usd: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    sector_tags: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # Screening #3: structured, single-value fields db_04/gs_07/gs_08 key off
    # -- distinct from sector_tags above (free-text, unstructured, display
    # only). Plain Text, deliberately no SAEnum and no CheckConstraint: a
    # CHECK can't reference another table's workspace config (gs_08's
    # approved_sectors lives in investment_profiles.mandate, SIM-<screening>)
    # or the rulebook's own prohibited list (db_04, app/services/screening/
    # rulebooks/track_b.yaml) -- and a CHECK enforcing either would block
    # *creating* a deal in a prohibited sector, which defeats db_04's whole
    # point: the row must exist so it can be evaluated and flagged as a
    # deal-breaker, not silently rejected at INSERT. Do not "fix" this into
    # a CheckConstraint later -- sector/geography policy lives in config and
    # the rulebook, not the schema.
    sector: Mapped[str | None] = mapped_column(Text, nullable=True)
    hq_geography: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Post-close founder equity as a fraction (0.0-1.0), not a percent int --
    # gs_06's threshold (founder_equity_gte: 0.10) compares directly against
    # this. Nullable: typically decided during deal structuring, after
    # intake, not at creation (see the PATCH /deals/{id} endpoint) --
    # unset means gs_06 stays `unknown`, never a guess. Numeric(5,4), not
    # Float: this feeds an auto-decision threshold, so exact decimal storage
    # (no binary-float write-time rounding) is worth it, unlike a plain
    # display value. asdecimal=False keeps it a plain Python float at the
    # ORM boundary -- consistent with how claim values are handled -- while
    # Postgres still stores it as exact NUMERIC.
    founder_equity_post_close_pct: Mapped[float | None] = mapped_column(
        Numeric(5, 4, asdecimal=False), nullable=True
    )

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=DEFAULT_DEAL_STATUS)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
