"""data source quarantined transition

The parser's pdf_too_large rejection (page count over its configured max)
needs to land on data_source.status as verified -> quarantined; otherwise
an over-length document stays "verified" forever and is indistinguishable
from a real success in GET /deals/{id}/documents and document_count. Second
deliberate, narrow carve-out to the one-way trigger (the first being
verified -> ocr_needed in 92fda2e2a5db), approved by Vansh: quarantined is
still terminal and every other post-pending transition is still rejected.

Revision ID: e3a7c5b19d42
Revises: c9d4e1f6a3b2
Create Date: 2026-10-01 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3a7c5b19d42"
down_revision: str | Sequence[str] | None = "c9d4e1f6a3b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FUNCTION = """
    CREATE OR REPLACE FUNCTION data_source_enforce_one_way_status() RETURNS trigger AS $$
    BEGIN
        IF OLD.status = 'pending' THEN
            RETURN NEW;
        ELSIF OLD.status = 'verified' AND NEW.status IN ({targets}) THEN
            RETURN NEW;
        ELSE
            RAISE EXCEPTION 'data_source % status is final once left pending (was %, tried %)',
                OLD.id, OLD.status, NEW.status;
        END IF;
    END;
    $$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(_FUNCTION.format(targets="'ocr_needed', 'quarantined'"))


def downgrade() -> None:
    op.execute(_FUNCTION.format(targets="'ocr_needed'"))
