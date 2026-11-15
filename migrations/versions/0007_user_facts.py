"""user facts

Facts about each user and the extraction state of archived conversations
(change add-user-facts, design D1). Every existing archived conversation
starts with ``facts_outcome`` ``NULL`` (pending), which is the backfill.

Downgrading drops ``facts`` and the three conversation columns.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-15 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("facts_outcome", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("facts_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "conversations", sa.Column("facts_next_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        op.f("ck_conversations_facts_outcome"),
        "conversations",
        "facts_outcome IN ('done', 'gave_up')",
    )
    op.create_index(
        "ix_conversations_facts_pending",
        "conversations",
        ["user_name", "id"],
        unique=False,
        postgresql_where=sa.text("ended_at IS NOT NULL AND facts_outcome IS NULL"),
    )

    op.create_table(
        "facts",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_name", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("embedding_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("embedding_next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_reason", sa.Text(), nullable=True),
        sa.Column("replaced_by", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("origin IN ('told', 'learned')", name=op.f("ck_facts_origin")),
        sa.CheckConstraint(
            "removed_reason IN ('forgotten', 'replaced', 'duplicate')",
            name=op.f("ck_facts_removed_reason"),
        ),
        sa.CheckConstraint(
            "(removed_at IS NULL) = (removed_reason IS NULL)", name=op.f("ck_facts_removed")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_facts_conversation_id_conversations"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by"], ["facts.id"], name=op.f("fk_facts_replaced_by_facts")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_facts")),
    )
    op.create_index(
        "ix_facts_active",
        "facts",
        ["user_name", "id"],
        unique=False,
        postgresql_where=sa.text("removed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_facts_active", table_name="facts")
    op.drop_table("facts")
    op.drop_index("ix_conversations_facts_pending", table_name="conversations")
    op.drop_constraint(op.f("ck_conversations_facts_outcome"), "conversations", type_="check")
    op.drop_column("conversations", "facts_next_at")
    op.drop_column("conversations", "facts_attempts")
    op.drop_column("conversations", "facts_outcome")
