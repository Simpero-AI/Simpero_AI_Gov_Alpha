"""data source intake link id

P3-10: adds data_source.intake_link_id, an FK back to the intake link that
uploaded a document via the public intake flow. NULL for org-side uploads
(app/api/uploads.py) -- only rows created via app/api/public_uploads.py set
it. Also tightens intake_deal_documents_insert's WITH CHECK to require the
inserted row's intake_link_id to match the session's own app.intake_link_id
GUC, closing the gap where a public-session insert could otherwise write an
arbitrary intake_link_id for any still-pending link on the same deal.

Revision ID: 9a48cce5ecac
Revises: 76a165315331
Create Date: 2026-08-28 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9a48cce5ecac"
down_revision: str | Sequence[str] | None = "76a165315331"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_source",
        sa.Column("intake_link_id", sa.UUID(), nullable=True),
    )
    op.create_index(
        op.f("ix_data_source_intake_link_id"), "data_source", ["intake_link_id"], unique=False
    )
    op.create_foreign_key(
        "fk_data_source_intake_link_id_deal_intake_link",
        "data_source",
        "deal_intake_link",
        ["intake_link_id"],
        ["id"],
    )
    op.execute("""
        ALTER POLICY intake_deal_documents_insert ON data_source
            WITH CHECK (
                org_id = (SELECT id FROM organisation WHERE clerk_org_id = current_setting('app.org_id', true))
                AND deal_id = current_setting('app.intake_deal_id', true)::uuid
                AND (intake_link_id = current_setting('app.intake_link_id', true)::uuid)
                AND EXISTS (
                    SELECT 1 FROM deal_intake_link l
                    WHERE l.deal_id = data_source.deal_id AND l.status = 'pending'
                )
            )
    """)


def downgrade() -> None:
    op.execute("""
        ALTER POLICY intake_deal_documents_insert ON data_source
            WITH CHECK (
                org_id = (SELECT id FROM organisation WHERE clerk_org_id = current_setting('app.org_id', true))
                AND deal_id = current_setting('app.intake_deal_id', true)::uuid
                AND EXISTS (
                    SELECT 1 FROM deal_intake_link l
                    WHERE l.deal_id = data_source.deal_id AND l.status = 'pending'
                )
            )
    """)
    op.drop_constraint(
        "fk_data_source_intake_link_id_deal_intake_link", "data_source", type_="foreignkey"
    )
    op.drop_index(op.f("ix_data_source_intake_link_id"), table_name="data_source")
    op.drop_column("data_source", "intake_link_id")
