import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.data_source import DataSource
from app.models.deal import Deal
from app.models.organisation import Organisation

# The shape of a claim is defined by contracts/claims.schema.json, NOT here.
# This model is the storage of that shape; the contract is its specification.
# If the two disagree, the contract wins and this file is the bug. Do not add a
# column here that the contract does not describe.

# Mirrors the contract's status enum. A String + CHECK rather than a native
# Postgres enum: `ALTER TYPE ... ADD VALUE` cannot run inside a transaction,
# which makes extending a PG enum genuinely awkward in a migration, and this
# enum has room to grow. A CHECK is a one-line change.
_STATUSES = (
    "proposed",
    "cited",
    "rejected",
    "missing",
    "conflicted",
    "inconclusive",
    "partially_verified",
    "verified",
)

# Statuses at or beyond "trust has been earned" — these require a
# verification_method recording HOW it was earned.
_CHECKED_STATUSES = ("cited", "conflicted", "inconclusive", "partially_verified", "verified")

_VERIFICATION_METHODS = (
    "exact_span",
    "formula_reexecution",
    "direct_read",
    "reranker",
    "human_review",
)

_LOCATION_KINDS = ("pdf", "xlsx", "docx")
# Which extraction contract produced a claim. Absent means quantitative, so
# every row written before this column existed stays valid.
_CLAIM_KINDS = ("quantitative", "qualitative")
# SIM-364 / FinGround: the assertion type. Non-null with `unknown` as the explicit
# fallback (see the claims contract). String + CHECK, same shape as status.
_CLAIM_TYPES = (
    "numerical",
    "temporal",
    "entity_attribute",
    "comparative",
    "regulatory",
    "computational",
    "unknown",
)
_ASSERTION_CLASSES = (
    "related_party",
    "operating_model",
    "market_definition",
    "competitive_position",
    "commercial_terms",
    "risk_or_dependency",
    "plan_or_commitment",
)


def _sql_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class Claim(Base):
    """One row of the deterministic claims spine.

    Every downstream pass (Verify, Compose, Score, AskMe, audit) reads from
    here and nothing re-parses the source, so this table's correctness is the
    product's correctness.

    One spine for all three formats, not a table per format: the formats differ
    only in the SHAPE of their location, while entity/attribute/value/status and
    scoping are identical. The format-specific location columns are null per
    row. A deal mixes formats and gets compared across them ("the CIM says X,
    the model says Y") — that is `WHERE deal_id = ...` on one table, versus a
    three-way UNION on every read if it were split.
    """

    __tablename__ = "claims"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer — RLS then
    # joins through to organisation.clerk_org_id, exactly as funds does. (The
    # Database Reference says "uuid FK" here; it is describing an intent the
    # existing schema never took, and following it would produce a column that
    # cannot reference organisation at all.)
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )

    # deal_id -> deals.id. `claims` predates `deals` in the migration chain
    # (claims_spine came before deals), so this started as a bare UUID; the FK
    # was added once `deals` existed, gated on a one-time orphaned-row check
    # against the real DB (see 77be2ddc60a0_data_source_fks.py) -- confirmed
    # zero, so no backfill was needed.
    # session_id/chunk_id: sessions/chunks tables exist, but no FK added yet
    # (out of scope for this pass). Typed UUID on the expectation that new
    # tables follow audit_log's intended UUID pattern rather than
    # organisation's serial Integer. The contract requires none of them (only
    # entity/attribute/value/status/location), so they stay nullable until
    # there is something to point at.
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(Deal.id), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    # data_source_id -> data_source.id. Column name stays data_source_id per
    # contracts/claims.schema.json (cross-repo contract on name/shape only;
    # a DB-level FK doesn't touch that contract).
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey(DataSource.id), nullable=True
    )
    chunk_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    entity: Mapped[str] = mapped_column(Text, nullable=False)
    attribute: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    period_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A=Actual, E=Estimate, P=Projected.
    period_kind: Mapped[str | None] = mapped_column(String(1), nullable=True)

    # raw / normalized / unit / scale_multiplier / scale_source / value_type.
    # JSONB rather than flat columns because the contract treats value as one
    # object: the raw printed form and the normalized number must travel
    # together or an audit cannot answer "why does the memo say 15.3M when the
    # deck says 15,295?".
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # --- Location: typed union by kind; the other formats' columns are null. --
    # No `file` column, deliberately: the contract makes `file` debug-only and
    # data_source_id the document identity.
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # List of [x0, top, x1, bottom] in Docling's native coordinate system.
    bbox: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    sheet: Mapped[str | None] = mapped_column(Text, nullable=True)
    cell_ref: Mapped[str | None] = mapped_column(String(16), nullable=True)
    paragraph: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    # How citation-level trust was earned. Null until something has been
    # checked; required once status is cited or later (see the CHECK below).
    verification_method: Mapped[str | None] = mapped_column(String(24), nullable=True)

    section: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Which extraction contract produced this claim. Absent (NULL) means
    # quantitative -- the numeric path predates this column. A qualitative claim
    # carries no magnitude by design (value_type text, normalized null) and
    # names its assertion_class; the CHECK below makes that non-bypassable.
    claim_kind: Mapped[str | None] = mapped_column(String(12), nullable=True)
    assertion_class: Mapped[str | None] = mapped_column(String(24), nullable=True)

    # Groups fragments of one logical table, including one split across pages.
    # This is what carries element structure in alpha — there is no elements
    # table and no element_id FK (decision: Option C, 2026-07-17).
    table_group_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    fragment_order: Mapped[int | None] = mapped_column(Integer, nullable=True)

    flags: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    # SIM-365: the parser's stable, positional claim identity ({page}:{cs}-{ce}[#n],
    # {sheet}!{cell}[#n], or {page}:none[#n]). Nullable -- rows written before this
    # column existed have none. Unique per (org_id, data_source_id, claim_ref) so
    # re-ingesting one document is idempotent (see the index in __table_args__).
    claim_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SIM-364: the FinGround assertion type; verification routes on it. Non-null with an
    # `unknown` server-default so existing rows and un-typeable claims are visibly untyped.
    claim_type: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="unknown", index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({_sql_list(_STATUSES)})", name="ck_claims_status"),
        CheckConstraint(f"claim_type IN ({_sql_list(_CLAIM_TYPES)})", name="ck_claims_claim_type"),
        # SIM-365: idempotent re-ingest of one document. NULLs are distinct in a unique
        # index, so this only binds once both data_source_id and claim_ref are present
        # (the real upload flow); the demo direct-ingest path leaves data_source_id NULL.
        Index(
            "uq_claims_org_data_source_claim_ref",
            "org_id",
            "data_source_id",
            "claim_ref",
            unique=True,
        ),
        CheckConstraint(
            f"verification_method IS NULL OR verification_method IN "
            f"({_sql_list(_VERIFICATION_METHODS)})",
            name="ck_claims_verification_method",
        ),
        CheckConstraint(f"kind IN ({_sql_list(_LOCATION_KINDS)})", name="ck_claims_kind"),
        CheckConstraint(
            f"claim_kind IS NULL OR claim_kind IN ({_sql_list(_CLAIM_KINDS)})",
            name="ck_claims_claim_kind",
        ),
        CheckConstraint(
            f"assertion_class IS NULL OR assertion_class IN ({_sql_list(_ASSERTION_CLASSES)})",
            name="ck_claims_assertion_class",
        ),
        # A qualitative claim is a text assertion by construction: it must name
        # its assertion_class. Mirrors the contract's qualitative allOf.
        CheckConstraint(
            "claim_kind IS DISTINCT FROM 'qualitative' OR assertion_class IS NOT NULL",
            name="ck_claims_qualitative_has_class",
        ),
        CheckConstraint(
            "period_kind IS NULL OR period_kind IN ('A', 'E', 'P')",
            name="ck_claims_period_kind",
        ),
        # The locator is always required: a claim always knows WHERE it looked,
        # even when it found nothing.
        CheckConstraint(
            "(kind = 'pdf' AND page IS NOT NULL)"
            " OR (kind = 'xlsx' AND sheet IS NOT NULL AND cell_ref IS NOT NULL)"
            " OR (kind = 'docx' AND paragraph IS NOT NULL)",
            name="ck_claims_locator_matches_kind",
        ),
        # All-or-nothing provenance, first half: a claim that is not `missing`
        # asserts it FOUND something, so it must carry the span proving where.
        # XLSX is exempt — a cell reference IS the whole citation, there is no
        # sub-cell span to give.
        CheckConstraint(
            "status = 'missing'"
            " OR kind = 'xlsx'"
            " OR (char_start IS NOT NULL AND char_end IS NOT NULL)",
            name="ck_claims_found_requires_span",
        ),
        # Second half, and the sharper one: a `missing` claim found nothing, so
        # it must carry NO span. char_start=0/char_end=0 is not an absence — it
        # is a citation to the first character of the page, and a highlight UI
        # would draw it over unrelated text while the claim says nothing was
        # found. Without this, "no span" is unenforceable and a zero span looks
        # valid.
        CheckConstraint(
            "status <> 'missing' OR (char_start IS NULL AND char_end IS NULL)",
            name="ck_claims_missing_has_no_span",
        ),
        # Trust must record how it was earned; trust with no recorded basis is
        # indistinguishable from a guess.
        CheckConstraint(
            f"status NOT IN ({_sql_list(_CHECKED_STATUSES)}) OR verification_method IS NOT NULL",
            name="ck_claims_checked_requires_method",
        ),
    )
