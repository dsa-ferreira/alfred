"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-01 14:27:47.286112

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "uq_conversations_one_current",
        "conversations",
        [sa.literal_column("(ended_at IS NULL)")],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "queue_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('owner', 'ask')", name=op.f("ck_queue_items_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'failed')", name=op.f("ck_queue_items_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queue_items")),
    )
    op.create_index(
        "ix_queue_items_pending",
        "queue_items",
        ["id"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("queue_item_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('owner', 'assistant', 'notice', 'ask_prompt', 'ask_reply')",
            name=op.f("ck_entries_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_entries_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["queue_item_id"],
            ["queue_items.id"],
            name=op.f("fk_entries_queue_item_id_queue_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entries")),
    )
    op.create_index(
        "ix_entries_conversation_id_id", "entries", ["conversation_id", "id"], unique=False
    )
    op.create_index(
        "uq_entries_queue_item_id_kind", "entries", ["queue_item_id", "kind"], unique=True
    )
    op.create_table(
        "turn_tool_calls",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("queue_item_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("result_excerpt", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["queue_item_id"],
            ["queue_items.id"],
            name=op.f("fk_turn_tool_calls_queue_item_id_queue_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turn_tool_calls")),
    )
    op.create_index(
        "ix_turn_tool_calls_queue_item_id",
        "turn_tool_calls",
        ["queue_item_id", "seq"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_turn_tool_calls_queue_item_id", table_name="turn_tool_calls")
    op.drop_table("turn_tool_calls")
    op.drop_index("uq_entries_queue_item_id_kind", table_name="entries")
    op.drop_index("ix_entries_conversation_id_id", table_name="entries")
    op.drop_table("entries")
    op.drop_index(
        "ix_queue_items_pending",
        table_name="queue_items",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("queue_items")
    op.drop_index(
        "uq_conversations_one_current",
        table_name="conversations",
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.drop_table("conversations")
