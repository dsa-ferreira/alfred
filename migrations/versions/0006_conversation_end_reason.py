"""conversation end reason

How a conversation ended and whether its idle-close marker was sent (change
close-idle-conversations, design D2): ``end_reason`` is ``'user'`` for
``alfred_end_conversation`` and ``'idle'`` for an idle close; archived rows
from before stay ``NULL``. ``marker_sent_at`` is set when the idle marker is
handed out with the user's next message.

Downgrading drops both columns.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-14 18:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("end_reason", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("marker_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_conversations_end_reason"), "conversations", "end_reason IN ('user', 'idle')"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_conversations_end_reason"), "conversations", type_="check")
    op.drop_column("conversations", "marker_sent_at")
    op.drop_column("conversations", "end_reason")
