"""conversation memory

Summaries, embeddings and full-text search for archived conversations, and
recall entries (memory design D1, D2, D8). Needs the ``vector`` extension,
which only a superuser can create, so it is checked here, never created.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-02 22:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alfred.db.migrate import MigrationError

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = "kind IN ('owner', 'assistant', 'notice', 'ask_prompt', 'ask_reply')"
NEW_KINDS = "kind IN ('owner', 'assistant', 'notice', 'ask_prompt', 'ask_reply', 'recall')"
SEARCH_TSV = "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, ''))"

MISSING_EXTENSION = (
    "the database has no 'vector' extension (pgvector). alfred's role can't create it; "
    "as a superuser, run: CREATE EXTENSION IF NOT EXISTS vector; in alfred's database, "
    "then migrate again"
)


def upgrade() -> None:
    present = op.get_bind().scalar(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    if not present:
        raise MigrationError(MISSING_EXTENSION)

    op.add_column("conversations", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("conversations", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "conversations", sa.Column("summarised_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("conversations", sa.Column("embedding", Vector(), nullable=True))
    op.add_column("conversations", sa.Column("embedding_model", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("summary_attempts", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "conversations", sa.Column("summary_next_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "conversations",
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed(SEARCH_TSV, persisted=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_conversations_search_tsv",
        "conversations",
        ["search_tsv"],
        unique=False,
        postgresql_using="gin",
    )

    op.drop_constraint(op.f("ck_entries_kind"), "entries", type_="check")
    op.create_check_constraint(op.f("ck_entries_kind"), "entries", NEW_KINDS)
    op.add_column("entries", sa.Column("recalled_conversation_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        op.f("fk_entries_recalled_conversation_id_conversations"),
        "entries",
        "conversations",
        ["recalled_conversation_id"],
        ["id"],
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


def downgrade() -> None:
    # Recall entries can't exist under 0001's kinds.
    op.execute("DELETE FROM entries WHERE kind = 'recall'")
    op.drop_index("uq_entries_recall", table_name="entries")
    op.drop_index("ix_entries_text_tsv", table_name="entries")
    op.drop_constraint(
        op.f("fk_entries_recalled_conversation_id_conversations"), "entries", type_="foreignkey"
    )
    op.drop_column("entries", "recalled_conversation_id")
    op.drop_constraint(op.f("ck_entries_kind"), "entries", type_="check")
    op.create_check_constraint(op.f("ck_entries_kind"), "entries", OLD_KINDS)

    op.drop_index("ix_conversations_search_tsv", table_name="conversations")
    for column in (
        "search_tsv",
        "summary_next_at",
        "summary_attempts",
        "embedding_model",
        "embedding",
        "summarised_at",
        "summary",
        "title",
    ):
        op.drop_column("conversations", column)
