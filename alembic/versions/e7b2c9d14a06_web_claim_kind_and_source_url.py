"""web claim kind + data_source.source_url

Adds a backend-local `web` claim kind for facts collected by the web-search
deep-search pass (app/services/web_search_collect.py), plus a `source_url`
column on data_source so a web-collected claim's citation is a real external
link, not a document filename masquerade.

`web` is deliberately NOT added to contracts/claims.schema.json: that schema
governs the parser->backend seam, and the parser never emits web claims -- they
are minted directly by the backend collect pass. So this touches only the
claims-table CHECK constraints (the DB gate the mint path actually clears).

For a `web` claim the URL (on data_source.source_url) is the locator: like
`xlsx`, it carries no positional char span, so it is exempt from the
found-requires-span rule and its locator rule is satisfied by the kind alone.

Revision ID: e7b2c9d14a06
Revises: d3f7a1c2b9e4
Create Date: 2026-09-04 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7b2c9d14a06"
down_revision: str | Sequence[str] | None = "d3f7a1c2b9e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept verbatim in sync with app/models/claim.py so the model and the DB agree.
_LOCATOR_WITH_WEB = (
    "(kind = 'pdf' AND page IS NOT NULL)"
    " OR (kind = 'xlsx' AND sheet IS NOT NULL AND cell_ref IS NOT NULL)"
    " OR (kind = 'docx' AND paragraph IS NOT NULL)"
    " OR (kind = 'web')"
)
_LOCATOR_NO_WEB = (
    "(kind = 'pdf' AND page IS NOT NULL)"
    " OR (kind = 'xlsx' AND sheet IS NOT NULL AND cell_ref IS NOT NULL)"
    " OR (kind = 'docx' AND paragraph IS NOT NULL)"
)
_SPAN_WITH_WEB = (
    "status = 'missing' OR kind = 'xlsx' OR kind = 'web'"
    " OR (char_start IS NOT NULL AND char_end IS NOT NULL)"
)
_SPAN_NO_WEB = (
    "status = 'missing' OR kind = 'xlsx' OR (char_start IS NOT NULL AND char_end IS NOT NULL)"
)


def upgrade() -> None:
    op.add_column("data_source", sa.Column("source_url", sa.Text(), nullable=True))

    op.drop_constraint("ck_claims_kind", "claims", type_="check")
    op.create_check_constraint("ck_claims_kind", "claims", "kind IN ('pdf', 'xlsx', 'docx', 'web')")

    op.drop_constraint("ck_claims_locator_matches_kind", "claims", type_="check")
    op.create_check_constraint("ck_claims_locator_matches_kind", "claims", _LOCATOR_WITH_WEB)

    op.drop_constraint("ck_claims_found_requires_span", "claims", type_="check")
    op.create_check_constraint("ck_claims_found_requires_span", "claims", _SPAN_WITH_WEB)


def downgrade() -> None:
    op.drop_constraint("ck_claims_found_requires_span", "claims", type_="check")
    op.create_check_constraint("ck_claims_found_requires_span", "claims", _SPAN_NO_WEB)

    op.drop_constraint("ck_claims_locator_matches_kind", "claims", type_="check")
    op.create_check_constraint("ck_claims_locator_matches_kind", "claims", _LOCATOR_NO_WEB)

    op.drop_constraint("ck_claims_kind", "claims", type_="check")
    op.create_check_constraint("ck_claims_kind", "claims", "kind IN ('pdf', 'xlsx', 'docx')")

    op.drop_column("data_source", "source_url")
