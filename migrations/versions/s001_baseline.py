"""baseline: every slice's tables, as of alfred 0.17.0

Revision ID: s001
Revises:
Create Date: 2026-11-16 13:41:04.820595

Replaces revisions 0001-0010: databases created by alfred 0.16 or older
can't be upgraded (``alfred.db.migrate`` refuses them). Generated from
``alfred.schema.metadata`` and edited by hand: the ``vector`` check, the
generated ``search_tsv`` column, and the partial and expression indexes.
Needs the ``vector`` extension, which only a superuser can create.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alfred.db.migrate import MigrationError

# revision identifiers, used by Alembic.
revision: str = "s001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MISSING_EXTENSION = (
    "the database has no 'vector' extension (pgvector). alfred's role can't create it; "
    "as a superuser, run: CREATE EXTENSION IF NOT EXISTS vector; in alfred's database, "
    "then migrate again"
)
_ENTRY_KINDS = (
    "kind IN ('user', 'assistant', 'notice', 'ask_prompt', 'ask_reply', 'recall', "
    "'reminder', 'reminder_reply')"
)
# Tables, dependents after what they refer to; the downgrade drops them in reverse.
_TABLES = (
    "attachments",
    "conversations",
    "queue_items",
    "reminders",
    "attachment_pages",
    "conversation_summaries",
    "entries",
    "fact_extractions",
    "facts",
    "input_requests",
    "reminder_recipients",
    "turn_tool_calls",
    "entry_attachments",
)


def upgrade() -> None:
    present = op.get_bind().scalar(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    if not present:
        raise MigrationError(_MISSING_EXTENSION)

    op.create_table(
        "attachments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("mime", sa.String(length=128), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("kind IN ('image', 'pdf')", name=op.f("ck_attachments_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachments")),
        sa.UniqueConstraint("object_key", name=op.f("uq_attachments_object_key")),
    )
    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_name", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.Text(), nullable=True),
        sa.Column("marker_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "end_reason IN ('user', 'idle', 'unattended')", name=op.f("ck_conversations_end_reason")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "uq_conversations_one_current_per_user",
        "conversations",
        ["user_name"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.create_table(
        "queue_items",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("user_name", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.CheckConstraint("kind IN ('chat', 'ask', 'reminder')", name=op.f("ck_queue_items_kind")),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'failed', 'waiting', 'cancelled')",
            name=op.f("ck_queue_items_status"),
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
        "reminders",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("repeat", sa.Text(), nullable=True),
        sa.Column("repeat_days", sa.SmallInteger(), nullable=True),
        sa.Column("repeat_time", sa.Text(), nullable=True),
        sa.Column("quiet_ok", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "repeat IN ('daily', 'weekdays', 'weekends', 'weekly')",
            name=op.f("ck_reminders_repeat"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'fired', 'cancelled')", name=op.f("ck_reminders_status")
        ),
        sa.CheckConstraint(
            "(repeat IS NULL) = (repeat_days IS NULL) AND (repeat IS NULL) = (repeat_time IS NULL)",
            name=op.f("ck_reminders_repeat_rule"),
        ),
        sa.CheckConstraint("repeat_days BETWEEN 1 AND 127", name=op.f("ck_reminders_repeat_days")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
    )
    op.create_index(
        "ix_reminders_pending_due",
        "reminders",
        ["due_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "attachment_pages",
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["attachments.id"],
            name=op.f("fk_attachment_pages_attachment_id_attachments"),
        ),
        sa.PrimaryKeyConstraint("attachment_id", "page", name=op.f("pk_attachment_pages")),
    )
    op.create_table(
        "conversation_summaries",
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summarised_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedding", Vector(), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(
                "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, ''))",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_summaries_conversation_id_conversations"),
        ),
        sa.PrimaryKeyConstraint("conversation_id", name=op.f("pk_conversation_summaries")),
    )
    op.create_index(
        "ix_conversation_summaries_search_tsv",
        "conversation_summaries",
        ["search_tsv"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_table(
        "entries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("turn_id", sa.BigInteger(), nullable=True),
        sa.Column("recalled_conversation_id", sa.BigInteger(), nullable=True),
        sa.Column("origin", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _ENTRY_KINDS,
            name=op.f("ck_entries_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_entries_conversation_id_conversations"),
        ),
        sa.ForeignKeyConstraint(
            ["recalled_conversation_id"],
            ["conversations.id"],
            name=op.f("fk_entries_recalled_conversation_id_conversations"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entries")),
    )
    op.create_index(
        "ix_entries_conversation_id_id", "entries", ["conversation_id", "id"], unique=False
    )
    op.create_index(
        "ix_entries_text_tsv",
        "entries",
        [sa.literal_column("to_tsvector('english', text)")],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "uq_entries_recall",
        "entries",
        ["conversation_id", "recalled_conversation_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'recall'"),
    )
    op.create_index("uq_entries_turn_id_kind", "entries", ["turn_id", "kind"], unique=True)
    op.create_table(
        "fact_extractions",
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('done', 'gave_up')", name=op.f("ck_fact_extractions_outcome")
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_fact_extractions_conversation_id_conversations"),
        ),
        sa.PrimaryKeyConstraint("conversation_id", name=op.f("pk_fact_extractions")),
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
        sa.Column("duplicate_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duplicate_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duplicate_next_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "ix_facts_duplicate_pending",
        "facts",
        ["id"],
        unique=False,
        postgresql_where=sa.text("removed_at IS NULL AND duplicate_checked_at IS NULL"),
    )
    op.create_table(
        "input_requests",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("queue_item_id", sa.BigInteger(), nullable=False),
        sa.Column("user_name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("tool", sa.String(length=128), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("question", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("answer", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("turn", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'answered', 'running', 'done', 'cancelled', 'expired')",
            name=op.f("ck_input_requests_status"),
        ),
        sa.ForeignKeyConstraint(
            ["queue_item_id"],
            ["queue_items.id"],
            name=op.f("fk_input_requests_queue_item_id_queue_items"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_input_requests")),
    )
    op.create_index(
        "ix_input_requests_open",
        "input_requests",
        ["user_name"],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "uq_input_requests_live_per_item",
        "input_requests",
        ["queue_item_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'answered', 'running')"),
    )
    op.create_table(
        "reminder_recipients",
        sa.Column("reminder_id", sa.BigInteger(), nullable=False),
        sa.Column("user_name", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["reminder_id"],
            ["reminders.id"],
            name=op.f("fk_reminder_recipients_reminder_id_reminders"),
        ),
        sa.PrimaryKeyConstraint("reminder_id", "user_name", name=op.f("pk_reminder_recipients")),
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
    op.create_table(
        "entry_attachments",
        sa.Column("entry_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["attachment_id"],
            ["attachments.id"],
            name=op.f("fk_entry_attachments_attachment_id_attachments"),
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["entries.id"], name=op.f("fk_entry_attachments_entry_id_entries")
        ),
        sa.PrimaryKeyConstraint("entry_id", "attachment_id", name=op.f("pk_entry_attachments")),
    )
    op.create_index(
        "ix_entry_attachments_attachment_id", "entry_attachments", ["attachment_id"], unique=False
    )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_table(table)
