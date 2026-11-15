"""fact duplicate check

The model's duplicate check of each fact (change fix-fact-removals, design
D1): ``duplicate_checked_at`` is ``NULL`` while it is pending, with its own
retry pair. Every fact that exists at upgrade time is marked checked, so the
upgrade starts no backfill and a fact restored by hand later isn't judged
again.

Downgrading drops the three columns and the index.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-16 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "facts", sa.Column("duplicate_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "facts",
        sa.Column("duplicate_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "facts", sa.Column("duplicate_next_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE facts SET duplicate_checked_at = now()")
    op.create_index(
        "ix_facts_duplicate_pending",
        "facts",
        ["id"],
        unique=False,
        postgresql_where=sa.text("removed_at IS NULL AND duplicate_checked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_facts_duplicate_pending", table_name="facts")
    op.drop_column("facts", "duplicate_next_at")
    op.drop_column("facts", "duplicate_attempts")
    op.drop_column("facts", "duplicate_checked_at")
