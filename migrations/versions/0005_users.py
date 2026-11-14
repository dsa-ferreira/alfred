"""users

Several users (change add-multiple-users, design D1, D5, D7, D9):
``user_name`` on conversations, queue items and attachments, ``created_by``
on reminders, the ``reminder_recipients`` table, the stored kind ``owner``
renamed to ``user`` in entries and queue items, and one current
conversation per user instead of one in total.

Existing rows are assigned to the admin named by ``ALFRED_MIGRATE_ADMIN``
(``alfred migrate`` reads no config), who also becomes the recipient of
every existing reminder. The variable is needed only when there are rows to
assign; without it, or with a value that isn't a user name, the migration
stops before changing anything.

Downgrading renames ``user`` back to ``owner`` and drops the user columns
and the recipients table. The old index allows one current conversation in
total, so it fails while several users have one: end the others first.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-14 15:00:00

"""

import os
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from alfred.db.migrate import MigrationError

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ADMIN_ENV = "ALFRED_MIGRATE_ADMIN"
# As config's user names: lowercase letters, digits and hyphens.
_NAME = re.compile(r"[a-z0-9][a-z0-9-]*")

# The tables whose rows get a user; entries belong to one through their conversation.
USER_TABLES = ("conversations", "queue_items", "attachments")
OWNED_TABLES = (*USER_TABLES, "reminders")

_ENTRY_KINDS = (
    "'assistant', 'notice', 'ask_prompt', 'ask_reply', 'recall', 'reminder', 'reminder_reply'"
)
KINDS = {
    "entries": (f"kind IN ('owner', {_ENTRY_KINDS})", f"kind IN ('user', {_ENTRY_KINDS})"),
    "queue_items": ("kind IN ('owner', 'ask', 'reminder')", "kind IN ('user', 'ask', 'reminder')"),
}
CURRENT = sa.text("ended_at IS NULL")


def _admin() -> str | None:
    """The admin's name from the environment, or ``None`` when no row needs one."""
    bind = op.get_bind()
    if not any(bind.scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {t})")) for t in OWNED_TABLES):
        return None
    name = os.environ.get(ADMIN_ENV, "").strip()
    if _NAME.fullmatch(name):
        return name
    problem = f"{ADMIN_ENV}={name!r} is not a user name" if name else f"{ADMIN_ENV} is not set"
    raise MigrationError(
        f"{problem}. The database has conversations, messages or reminders from before "
        f"alfred had users; set {ADMIN_ENV} to the admin's [[users]] name (lowercase "
        "letters, digits and hyphens) so they are assigned to the admin, then migrate again"
    )


def upgrade() -> None:
    admin = _admin()

    for table in USER_TABLES:
        op.add_column(table, sa.Column("user_name", sa.Text(), nullable=True))
    op.add_column("reminders", sa.Column("created_by", sa.Text(), nullable=True))
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

    if admin is not None:
        for table in USER_TABLES:
            op.execute(sa.text(f"UPDATE {table} SET user_name = :admin").bindparams(admin=admin))
        op.execute(sa.text("UPDATE reminders SET created_by = :admin").bindparams(admin=admin))
        op.execute(
            sa.text(
                "INSERT INTO reminder_recipients (reminder_id, user_name) "
                "SELECT id, :admin FROM reminders"
            ).bindparams(admin=admin)
        )
    for table in USER_TABLES:
        op.alter_column(table, "user_name", nullable=False)
    op.alter_column("reminders", "created_by", nullable=False)

    for table, (_old, new) in KINDS.items():
        op.drop_constraint(op.f(f"ck_{table}_kind"), table, type_="check")
        op.execute(f"UPDATE {table} SET kind = 'user' WHERE kind = 'owner'")
        op.create_check_constraint(op.f(f"ck_{table}_kind"), table, new)

    op.drop_index(
        "uq_conversations_one_current", table_name="conversations", postgresql_where=CURRENT
    )
    op.create_index(
        "uq_conversations_one_current_per_user",
        "conversations",
        ["user_name"],
        unique=True,
        postgresql_where=CURRENT,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_conversations_one_current_per_user",
        table_name="conversations",
        postgresql_where=CURRENT,
    )
    op.create_index(
        "uq_conversations_one_current",
        "conversations",
        [sa.literal_column("(ended_at IS NULL)")],
        unique=True,
        postgresql_where=CURRENT,
    )

    for table, (old, _new) in KINDS.items():
        op.drop_constraint(op.f(f"ck_{table}_kind"), table, type_="check")
        op.execute(f"UPDATE {table} SET kind = 'owner' WHERE kind = 'user'")
        op.create_check_constraint(op.f(f"ck_{table}_kind"), table, old)

    op.drop_table("reminder_recipients")
    op.drop_column("reminders", "created_by")
    for table in reversed(USER_TABLES):
        op.drop_column(table, "user_name")
