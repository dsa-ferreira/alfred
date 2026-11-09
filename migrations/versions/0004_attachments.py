"""attachments

The owner's photos, image files and PDFs (change add-attachments, design D1):
``attachments`` (the bytes are in the object store) and ``attachment_pages``
(each PDF page's text). Only new tables, so downgrading drops them and the
files they pointed at stay in the bucket.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-09 20:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("queue_item_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_id", sa.BigInteger(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["queue_item_id"],
            ["queue_items.id"],
            name=op.f("fk_attachments_queue_item_id_queue_items"),
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"], ["entries.id"], name=op.f("fk_attachments_entry_id_entries")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachments")),
        sa.UniqueConstraint("object_key", name=op.f("uq_attachments_object_key")),
    )
    op.create_index(
        "ix_attachments_queue_item_id", "attachments", ["queue_item_id", "position"], unique=False
    )
    op.create_index("ix_attachments_entry_id", "attachments", ["entry_id"], unique=False)
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


def downgrade() -> None:
    op.drop_table("attachment_pages")
    op.drop_index("ix_attachments_entry_id", table_name="attachments")
    op.drop_index("ix_attachments_queue_item_id", table_name="attachments")
    op.drop_table("attachments")
