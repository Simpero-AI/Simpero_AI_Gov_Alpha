"""intake claim kind

Adds a backend-local `intake` claim kind for the founder/analyst answers minted
from a deal's intake questionnaire (app/services/intake_facts.py). Like `web`,
these are minted directly by the backend, never emitted by the parser, so this
touches only the claims-table CHECK constraints (the DB gate the mint clears) and
is deliberately absent from contracts/claims.schema.json.

For an `intake` claim the locator is the intake data_source (one synthetic row per
deal, carrying intake_link_id); like `xlsx`/`web` it has no positional char span,
so it is exempt from the locator-span and found-requires-span rules by kind alone.

Revision ID: a7d2f4e91c30
Revises: c1e5a9f4b207
Create Date: 2026-09-10 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a7d2f4e91c30"
down_revision: str | Sequence[str] | None = "c1e5a9f4b207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept verbatim in sync with app/models/claim.py so the model and the DB agree.
_LOCATOR_WITH_INTAKE = (
    "(kind = 'pdf' AND page IS NOT NULL)"
    " OR (kind = 'xlsx' AND sheet IS NOT NULL AND cell_ref IS NOT NULL)"
    " OR (kind = 'docx' AND paragraph IS NOT NULL)"
    " OR (kind = 'web')"
    " OR (kind = 'intake')"
)
_LOCATOR_NO_INTAKE = (
    "(kind = 'pdf' AND page IS NOT NULL)"
    " OR (kind = 'xlsx' AND sheet IS NOT NULL AND cell_ref IS NOT NULL)"
    " OR (kind = 'docx' AND paragraph IS NOT NULL)"
    " OR (kind = 'web')"
)
_SPAN_WITH_INTAKE = (
    "status = 'missing' OR kind = 'xlsx' OR kind = 'web' OR kind = 'intake'"
    " OR (char_start IS NOT NULL AND char_end IS NOT NULL)"
)
_SPAN_NO_INTAKE = (
    "status = 'missing' OR kind = 'xlsx' OR kind = 'web'"
    " OR (char_start IS NOT NULL AND char_end IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint("ck_claims_kind", "claims", type_="check")
    op.create_check_constraint(
        "ck_claims_kind", "claims", "kind IN ('pdf', 'xlsx', 'docx', 'web', 'intake')"
    )

    op.drop_constraint("ck_claims_locator_matches_kind", "claims", type_="check")
    op.create_check_constraint("ck_claims_locator_matches_kind", "claims", _LOCATOR_WITH_INTAKE)

    op.drop_constraint("ck_claims_found_requires_span", "claims", type_="check")
    op.create_check_constraint("ck_claims_found_requires_span", "claims", _SPAN_WITH_INTAKE)


def downgrade() -> None:
    op.drop_constraint("ck_claims_found_requires_span", "claims", type_="check")
    op.create_check_constraint("ck_claims_found_requires_span", "claims", _SPAN_NO_INTAKE)

    op.drop_constraint("ck_claims_locator_matches_kind", "claims", type_="check")
    op.create_check_constraint("ck_claims_locator_matches_kind", "claims", _LOCATOR_NO_INTAKE)

    op.drop_constraint("ck_claims_kind", "claims", type_="check")
    op.create_check_constraint("ck_claims_kind", "claims", "kind IN ('pdf', 'xlsx', 'docx', 'web')")
