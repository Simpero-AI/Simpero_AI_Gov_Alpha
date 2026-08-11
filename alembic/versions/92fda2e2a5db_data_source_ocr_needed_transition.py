"""data source ocr_needed transition

SIM-350's parser signal (no_extractable_text) needs to land on
data_source.status as verified -> ocr_needed. The one-way trigger added in
d6d2fe8f27ae rejects any UPDATE where OLD.status <> 'pending', so that write
currently raises. This migration is a deliberate, narrow relaxation, decided
by Vansh (Option A of docs/plans/start-analysis-flow-alpha.md's "Blocking
prerequisite" section) rather than a loosening of the guarantee: the
lifecycle stays a one-way DAG (ocr_needed is still terminal, every other
transition is still rejected), it's just no longer collapsed to a single
edge out of 'pending'. Still enforced against every role, including the
table owner -- this replaces the trigger function body, not the trigger
itself.

No grant changes needed: GRANT UPDATE (status, fingerprint, status_updated_at)
ON data_source TO dd_app already exists from d6d2fe8f27ae.

Revision ID: 92fda2e2a5db
Revises: 7b837e251134
Create Date: 2026-08-10 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "92fda2e2a5db"
down_revision: str | Sequence[str] | None = "7b837e251134"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_FUNCTION = """
    CREATE OR REPLACE FUNCTION data_source_enforce_one_way_status() RETURNS trigger AS $$
    BEGIN
        IF OLD.status <> 'pending' THEN
            RAISE EXCEPTION 'data_source % status is final once left pending (was %)',
                OLD.id, OLD.status;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
"""

_NEW_FUNCTION = """
    CREATE OR REPLACE FUNCTION data_source_enforce_one_way_status() RETURNS trigger AS $$
    BEGIN
        IF OLD.status = 'pending' THEN
            RETURN NEW;
        ELSIF OLD.status = 'verified' AND NEW.status = 'ocr_needed' THEN
            RETURN NEW;
        ELSE
            RAISE EXCEPTION 'data_source % status is final once left pending (was %, tried %)',
                OLD.id, OLD.status, NEW.status;
        END IF;
    END;
    $$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(_NEW_FUNCTION)


def downgrade() -> None:
    op.execute(_OLD_FUNCTION)
