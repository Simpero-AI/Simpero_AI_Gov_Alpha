"""company firmographic assertion classes

Adds co_investor, funding_history, key_customer and geographic_presence to
ck_claims_assertion_class so the Company tab's four previously permanent-empty
sections (Co-Investors / Funding History / Key Customers / Geographic Presence)
can be populated from qualitative claims instead of showing a hardcoded
"No evidence found". Constraint-only: the column already exists.

Revision ID: c9d4e1f6a3b2
Revises: b3f9c7d21a08
Create Date: 2026-09-17 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "c9d4e1f6a3b2"
down_revision: str | None = "b3f9c7d21a08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Keep in lockstep with app/models/claim.py:_ASSERTION_CLASSES and
# contracts/claims.schema.json's assertion_class enum.
_NEW = (
    "related_party",
    "operating_model",
    "market_definition",
    "competitive_position",
    "commercial_terms",
    "risk_or_dependency",
    "plan_or_commitment",
    "co_investor",
    "funding_history",
    "key_customer",
    "geographic_presence",
)

_OLD = (
    "related_party",
    "operating_model",
    "market_definition",
    "competitive_position",
    "commercial_terms",
    "risk_or_dependency",
    "plan_or_commitment",
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.drop_constraint("ck_claims_assertion_class", "claims", type_="check")
    op.create_check_constraint(
        "ck_claims_assertion_class",
        "claims",
        f"assertion_class IS NULL OR assertion_class IN ({_in(_NEW)})",
    )


def downgrade() -> None:
    # A pre-existing row using a new class would violate the narrowed constraint;
    # none exist until the parser emits them (a later deploy), so a straight
    # recreate is safe at this revision.
    op.drop_constraint("ck_claims_assertion_class", "claims", type_="check")
    op.create_check_constraint(
        "ck_claims_assertion_class",
        "claims",
        f"assertion_class IS NULL OR assertion_class IN ({_in(_OLD)})",
    )
