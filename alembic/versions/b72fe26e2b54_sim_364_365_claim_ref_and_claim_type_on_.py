"""SIM-364/365 claim_ref and claim_type on claims

Revision ID: b72fe26e2b54
Revises: 77be2ddc60a0
Create Date: 2026-08-02 19:17:30.508725

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b72fe26e2b54"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = "77be2ddc60a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CLAIM_TYPES = (
    "numerical",
    "temporal",
    "entity_attribute",
    "comparative",
    "regulatory",
    "computational",
    "unknown",
)


def upgrade() -> None:
    # SIM-365: stable parser claim identity, unique per (org, data_source) for
    # idempotent re-ingest. Nullable -- existing rows predate it.
    op.add_column("claims", sa.Column("claim_ref", sa.Text(), nullable=True))
    # SIM-364: FinGround assertion type; verification routes on it. Non-null with an
    # `unknown` server-default so existing rows and un-typeable claims are visibly untyped.
    op.add_column(
        "claims",
        sa.Column("claim_type", sa.String(length=16), nullable=False, server_default="unknown"),
    )
    op.create_check_constraint(
        "ck_claims_claim_type",
        "claims",
        "claim_type IN (" + ", ".join(f"'{t}'" for t in _CLAIM_TYPES) + ")",
    )
    op.create_index("ix_claims_claim_type", "claims", ["claim_type"])
    op.create_index(
        "uq_claims_org_data_source_claim_ref",
        "claims",
        ["org_id", "data_source_id", "claim_ref"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_claims_org_data_source_claim_ref", table_name="claims")
    op.drop_index("ix_claims_claim_type", table_name="claims")
    op.drop_constraint("ck_claims_claim_type", "claims", type_="check")
    op.drop_column("claims", "claim_type")
    op.drop_column("claims", "claim_ref")
