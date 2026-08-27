import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.organisation import Funds, Organisation, Users

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
    # Creator, for display/audit only — deals stay org-scoped, not user-scoped
    # (see DealRowResponse's docstring); no query filters on this column.
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey(Users.id), nullable=True, index=True
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
    # approved sectors are derived from the org's `mandates` row -- see
    # app/services/screening/workspace_config.py, SIM-414)
    # or the rulebook's own prohibited list (db_04, app/services/screening/
    # rulebooks/track_b.yaml) -- and a CHECK enforcing either would block
    # *creating* a deal in a prohibited sector, which defeats db_04's whole
    # point: the row must exist so it can be evaluated and flagged as a
    # deal-breaker, not silently rejected at INSERT. Do not "fix" this into
    # a CheckConstraint later -- sector/geography policy lives in config and
    # the rulebook, not the schema.
    sector: Mapped[str | None] = mapped_column(Text, nullable=True)
    hq_geography: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Path B "search just in case": the parser's grounded verdict per qualitative
    # (llm) screening rule that has no deterministic evaluator -- shaped
    # {rule_id: {"verdict": "Y"|"N"|"unknown", "evidence": "<quote>"}}. Written at
    # verification ingest from the documents' findings; read by the document
    # evaluators (app/services/screening/evaluators/document.py). Nullable: a deal
    # whose mandate selected no such rule (or that predates this pass) simply has
    # none, and every document rule then reads as unknown.
    qualitative_findings: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Pipeline Inspector: how to organize THIS deal's facts into a dashboard --
    # {"subjects": [{"name", "kind", "entities": [...]}], "metric_order": [...]}.
    # The parser's grounded organizing pass produces it (it only arranges cited
    # facts, never invents a value); the Inspector renders subjects/metric order
    # from it and falls back to deterministic frequency grouping when it is null
    # (a deal that predates the pass, or whose organization was skipped).
    dashboard_structure: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=DEFAULT_DEAL_STATUS)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
