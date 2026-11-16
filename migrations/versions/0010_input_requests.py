"""input requests

Turns that wait for their user's answer (change add-user-input-requests,
design D4). ``queue_items.status`` gains ``waiting`` (parked for an answer)
and ``cancelled`` (ended without one). The new ``input_requests`` table holds
each question, its answer and the parked turn's progress, with at most one
live request (``open``, ``answered`` or ``running``) per queue item.

Downgrading marks ``waiting`` and ``cancelled`` items ``failed`` and drops
the table.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-17 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "status IN ('pending', 'done', 'failed')"
NEW_STATUSES = "status IN ('pending', 'done', 'failed', 'waiting', 'cancelled')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_queue_items_status"), "queue_items", type_="check")
    op.create_check_constraint(op.f("ck_queue_items_status"), "queue_items", NEW_STATUSES)

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
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
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
        "uq_input_requests_live_per_item",
        "input_requests",
        ["queue_item_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'answered', 'running')"),
    )
    op.create_index(
        "ix_input_requests_open",
        "input_requests",
        ["user_name"],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("ix_input_requests_open", table_name="input_requests")
    op.drop_index("uq_input_requests_live_per_item", table_name="input_requests")
    op.drop_table("input_requests")
    op.execute(
        "UPDATE queue_items SET status = 'failed', finished_at = coalesce(finished_at, now()), "
        "failure_reason = coalesce(failure_reason, 'downgraded while waiting for an answer') "
        "WHERE status IN ('waiting', 'cancelled')"
    )
    op.drop_constraint(op.f("ck_queue_items_status"), "queue_items", type_="check")
    op.create_check_constraint(op.f("ck_queue_items_status"), "queue_items", OLD_STATUSES)
