"""merge resolved_entity and intake-questions heads

No DDL: this exists purely to rejoin two independent migration branches so
there is one head again. SIM-420's resolved_entity (b7e41d92c5a8) grew off
the SIM-262 line while staging's deal_intake_questions (67e5302afcfe) grew
off its own; the two touch different tables and neither depends on the
other, so joining them is all that is needed.

Revision ID: 965c3096f497
Revises: 67e5302afcfe, b7e41d92c5a8
Create Date: 2026-08-28 00:34:57.122277

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "965c3096f497"
# str | Sequence[str] rather than just str: a merge migration's down_revision
# is a tuple of the heads it joins, not a single revision id.
down_revision: str | Sequence[str] | None = ("67e5302afcfe", "b7e41d92c5a8")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
