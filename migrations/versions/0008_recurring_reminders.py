"""recurring reminders

Repeat rules and quiet reminders, the due time of each delivered occurrence,
and conversations closed as ``unattended`` (change add-recurring-reminders,
design D1).

``reminders`` gains ``repeat``, ``repeat_days`` (a weekday bitmask, Monday =
bit 0), ``repeat_time`` (local ``HH:MM``) and ``quiet_ok``. ``queue_items``
and ``entries`` gain ``reminder_due_at``, backfilled from
``reminders.due_at``: exact, since every existing reminder is one-shot.

Downgrading cancels pending repeating reminders, drops the new columns, and
turns ``unattended`` conversations into ``idle`` ones whose marker counts as
sent, so none appears.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-15 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("reminders", sa.Column("repeat", sa.Text(), nullable=True))
    op.add_column("reminders", sa.Column("repeat_days", sa.SmallInteger(), nullable=True))
    op.add_column("reminders", sa.Column("repeat_time", sa.Text(), nullable=True))
    op.add_column(
        "reminders",
        sa.Column("quiet_ok", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_reminders_repeat"),
        "reminders",
        "repeat IN ('daily', 'weekdays', 'weekends', 'weekly')",
    )
    op.create_check_constraint(
        op.f("ck_reminders_repeat_days"), "reminders", "repeat_days BETWEEN 1 AND 127"
    )
    op.create_check_constraint(
        op.f("ck_reminders_repeat_rule"),
        "reminders",
        "(repeat IS NULL) = (repeat_days IS NULL) AND (repeat IS NULL) = (repeat_time IS NULL)",
    )

    for table in ("queue_items", "entries"):
        op.add_column(
            table, sa.Column("reminder_due_at", sa.DateTime(timezone=True), nullable=True)
        )
        op.execute(
            f"UPDATE {table} SET reminder_due_at = reminders.due_at "
            f"FROM reminders WHERE reminders.id = {table}.reminder_id"
        )

    op.drop_constraint(op.f("ck_conversations_end_reason"), "conversations", type_="check")
    op.create_check_constraint(
        op.f("ck_conversations_end_reason"),
        "conversations",
        "end_reason IN ('user', 'idle', 'unattended')",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE conversations SET end_reason = 'idle', "
        "marker_sent_at = coalesce(marker_sent_at, now()) WHERE end_reason = 'unattended'"
    )
    op.drop_constraint(op.f("ck_conversations_end_reason"), "conversations", type_="check")
    op.create_check_constraint(
        op.f("ck_conversations_end_reason"), "conversations", "end_reason IN ('user', 'idle')"
    )

    for table in ("queue_items", "entries"):
        op.drop_column(table, "reminder_due_at")

    op.execute(
        "UPDATE reminders SET status = 'cancelled', cancelled_at = now() "
        "WHERE status = 'pending' AND repeat IS NOT NULL"
    )
    op.drop_constraint(op.f("ck_reminders_repeat_rule"), "reminders", type_="check")
    op.drop_constraint(op.f("ck_reminders_repeat_days"), "reminders", type_="check")
    op.drop_constraint(op.f("ck_reminders_repeat"), "reminders", type_="check")
    op.drop_column("reminders", "quiet_ok")
    op.drop_column("reminders", "repeat_time")
    op.drop_column("reminders", "repeat_days")
    op.drop_column("reminders", "repeat")
