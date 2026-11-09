"""reminders

Timed reminders (change add-reminders, design D3): the ``reminders`` table,
``reminder_id`` on queue items and entries, and the new queue and entry kinds.

Downgrading fails while reminder queue items or entries exist (the old check
constraints don't allow them); delete them first.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-09 14:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_ENTRY_KINDS = "kind IN ('owner', 'assistant', 'notice', 'ask_prompt', 'ask_reply', 'recall')"
NEW_ENTRY_KINDS = (
    "kind IN ('owner', 'assistant', 'notice', 'ask_prompt', 'ask_reply', 'recall', "
    "'reminder', 'reminder_reply')"
)
OLD_QUEUE_KINDS = "kind IN ('owner', 'ask')"
NEW_QUEUE_KINDS = "kind IN ('owner', 'ask', 'reminder')"


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'fired', 'cancelled')", name=op.f("ck_reminders_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
    )
    op.create_index(
        "ix_reminders_pending_due",
        "reminders",
        ["due_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )

    for table, new in (("queue_items", NEW_QUEUE_KINDS), ("entries", NEW_ENTRY_KINDS)):
        op.drop_constraint(op.f(f"ck_{table}_kind"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_kind"), table, new)
        op.add_column(table, sa.Column("reminder_id", sa.BigInteger(), nullable=True))
        op.create_foreign_key(
            op.f(f"fk_{table}_reminder_id_reminders"), table, "reminders", ["reminder_id"], ["id"]
        )


def downgrade() -> None:
    for table, old in (("entries", OLD_ENTRY_KINDS), ("queue_items", OLD_QUEUE_KINDS)):
        op.drop_constraint(op.f(f"fk_{table}_reminder_id_reminders"), table, type_="foreignkey")
        op.drop_column(table, "reminder_id")
        op.drop_constraint(op.f(f"ck_{table}_kind"), table, type_="check")
        op.create_check_constraint(op.f(f"ck_{table}_kind"), table, old)

    op.drop_index("ix_reminders_pending_due", table_name="reminders")
    op.drop_table("reminders")
