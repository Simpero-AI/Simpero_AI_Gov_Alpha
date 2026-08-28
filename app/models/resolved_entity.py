import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import DateTime

from app.core.database import Base
from app.models.deal import Deal
from app.models.organisation import Organisation

# AE-A-CORR (SIM-420). The deal's ONE consolidated identity, folded from the
# per-registry `entity_resolution` attempts (SIM-262) into the single artifact
# every corroboration adapter keys on instead of raw `claim.entity`.
#
# Why a second table rather than reading entity_resolution directly: that table
# is one row per (deal, registry) ATTEMPT -- it answers "what did EDGAR say",
# and by design it has to keep saying it even when the answer was `not_found`.
# An adapter does not want an attempt log; it wants "who is this company, under
# every name, with every registry id we hold". Folding that per (claim x source)
# would re-derive the same answer dozens of times per deal and give each adapter
# its own chance to fold it differently. One row, folded once, read by all.
#
# Write-once, enforced at the database layer (REVOKE UPDATE, DELETE ON
# resolved_entity FROM dd_app -- see this table's migration). Same reasoning as
# entity_resolution and screening_result: this row is the anchor every
# downstream corroboration verdict inherits, so a mutable one would silently
# re-point old events at a different company. Re-resolving INSERTs a new row;
# the older rows are the record of how the answer changed.

# Registry cross-reference keys carried in `registry_ids`. Named constants, not
# bare strings, because an adapter looking up the wrong key gets None -- which
# reads as "no signal" and would silently disable the adapter rather than fail.
REGISTRY_CIK = "cik"  # SEC EDGAR central index key
REGISTRY_ISED_CORPORATION_ID = "ised_corporation_id"  # Corporations Canada (federal)
REGISTRY_BC_REGISTRATION_NUMBER = "bc_registration_number"  # OrgBook BC (provincial)

REGISTRY_KEYS = (
    REGISTRY_CIK,
    REGISTRY_ISED_CORPORATION_ID,
    REGISTRY_BC_REGISTRATION_NUMBER,
)


class ResolvedEntity(Base):
    """The deal-scoped resolved-entity artifact: canonical legal name, the
    alias/former-name list, and the registry cross-refs, in one row.

    Deal-level, not claim-level -- "this deal is CIK 0001326801 / ISED
    corporationId 1234567" has no claim behind it, which is exactly why it
    cannot live in `corroboration_events` (whose claim_id is NOT NULL).
    """

    __tablename__ = "resolved_entity"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )

    # Tenant. Integer FK because organisation.id is a serial Integer -- RLS
    # joins through to organisation.clerk_org_id, same idiom as
    # deals/claims/entity_resolution.
    org_id: Mapped[int] = mapped_column(
        Integer, ForeignKey(Organisation.id), nullable=False, index=True
    )
    deal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(Deal.id), nullable=False, index=True
    )

    # The name to present and to match against, chosen from the contributing
    # registries by a fixed precedence (see _CANONICAL_SOURCE_PRECEDENCE in
    # app/services/entity_resolution/resolved.py) so two folds of the same
    # inputs never disagree.
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)

    # ["Old Name Ltd", ...] -- former and alternate legal names, canonical
    # excluded. Load-bearing, not decoration: an older deck legitimately names
    # the company as it was then, and an adapter that only knew the current
    # name would read that as a different company (a false conflict) rather
    # than as the same one.
    aliases: Mapped[list] = mapped_column(JSONB, nullable=False)

    # {"cik": "0000320193", "ised_corporation_id": "1234567", ...} -- only the
    # registries that actually answered. Keys are constrained to REGISTRY_KEYS
    # by the folding service, not by a DB CHECK: a CHECK over JSONB keys would
    # have to be rewritten every time a registry is added, and the service is
    # the only writer.
    registry_ids: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # Which entity_resolution rows fed this fold, and what each contributed.
    # JSONB rather than columns: written once, read back whole, nothing queries
    # across its keys -- same call as entity_resolution.evidence.
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # No updated_at -- write-once rows have nothing to update.
    #
    # clock_timestamp(), NOT now(): now() is the TRANSACTION timestamp, so two
    # folds written in one transaction would tie and `latest_for_deal` would
    # pick between them arbitrarily. Same reasoning as entity_resolution.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("length(btrim(canonical_name)) > 0", name="ck_resolved_entity_has_name"),
        CheckConstraint("jsonb_typeof(aliases) = 'array'", name="ck_resolved_entity_aliases_array"),
        CheckConstraint(
            "jsonb_typeof(registry_ids) = 'object'", name="ck_resolved_entity_registry_ids_object"
        ),
        # The invariant that keeps "absence != conflict" true at the storage
        # layer: a fold with no registry id at all resolved nothing, and must
        # leave NO row rather than a name-only one an adapter could mistake for
        # a real anchor. Enforced here, not only in the service, for the same
        # reason as entity_resolution's resolved-requires-registry_id CHECK --
        # an anchor-less "resolution" is indistinguishable from a guess.
        CheckConstraint("registry_ids <> '{}'::jsonb", name="ck_resolved_entity_has_registry_id"),
    )
